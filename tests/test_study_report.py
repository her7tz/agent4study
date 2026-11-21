"""FR-16: paginated read-only reports with real local artifacts and fake records."""
import copy
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from artifacts import ArtifactStore
from runtime_control import CancellationToken, RunCancelled
from study_report import _create_report, _period, _read_all, _statistics, StudyReportError
from study_log import READ_SCOPES, StudyLog, StudyLogError


class StudyFixture:
    base_url = 'https://study.invalid'

    def __init__(self, count=101):
        self.calls = []
        self.projects = {'projects': [{'id': 1, 'name': '学习项目', 'importance': 3, 'status': 'active'}]}
        self.records = [{'id': identifier, 'subject': '递归', 'note': '递归需要终止条件。',
            'record_type': 'learning', 'intensity_score': 2, 'project_id': 1,
            'study_date': '2026-10-29', 'created_at': '2026-10-29T00:00:00',
            'updated_at': '2026-10-29T00:00:00'} for identifier in range(count, 0, -1)]

    def read(self, resource, cancellation=None, **query):
        self.calls.append((resource, query))
        if cancellation:
            cancellation.check()
        if resource == 'projects':
            return {'data': copy.deepcopy(self.projects)}
        if resource != 'records':
            raise AssertionError('报告不应访问其他资源')
        offset, limit = query.get('offset', 0), query['limit']
        rows = self.records[offset:offset + limit]
        end = offset + len(rows)
        return {'data': {'records': copy.deepcopy(rows), 'total': len(self.records),
            'total_intensity': len(self.records) * 2, 'total_points': len(self.records) * 6,
            'next_offset': end if end < len(self.records) else None}}

    def bind_reader(self):
        return self.read, lambda cancellation: cancellation.check()


class StudyReportContracts(unittest.TestCase):
    def setUp(self):
        self.arguments = {'period': 'range', 'start_date': '2026-10-01', 'end_date': '2026-10-30'}
        self.period = _period(self.arguments)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.settings = SimpleNamespace(data_home=root, output_dir=root / 'reports',
            database_path=root / 'tasks.sqlite3', api_key='fixture-key', model='fixture')
        self.artifacts = ArtifactStore(self.settings.output_dir, self.settings.database_path, root)

    def read(self, adapter):
        read, _ = adapter.bind_reader()
        return _read_all(read, self.period, CancellationToken(), lambda _: None)

    def test_full_pagination_and_statistics_include_second_page(self):
        adapter = StudyFixture()
        records, projects, aggregates = self.read(adapter)
        self.assertEqual(len(records), 101)
        self.assertEqual(records[-1]['id'], 1)
        self.assertEqual([q['offset'] for r, q in adapter.calls if r == 'records'], [0, 100, 0, 100])
        stats = _statistics(records, projects, aggregates)
        self.assertEqual((stats['records'], stats['active_days'], stats['total_intensity'], stats['total_points']), (101, 1, 202, 606))

    def test_missing_page_is_an_error_not_partial_success(self):
        adapter = StudyFixture()
        original = adapter.read
        def truncated(resource, **kwargs):
            result = original(resource, **kwargs)
            if resource == 'records' and kwargs['offset'] == 0:
                result['data']['next_offset'] = None
            return result
        adapter.read = truncated
        with self.assertRaisesRegex(StudyReportError, '分页'):
            self.read(adapter)

    def test_duplicate_and_out_of_range_records_fail(self):
        for field, value in [('id', 101), ('study_date', '2026-09-01')]:
            adapter = StudyFixture()
            adapter.records[-1][field] = value
            with self.subTest(field=field), self.assertRaises(StudyReportError):
                self.read(adapter)

    def test_changed_boundary_page_rejects_mixed_snapshot(self):
        adapter = StudyFixture(2)
        original = adapter.read
        counts = 0
        def changing(resource, **kwargs):
            nonlocal counts
            if resource == 'records':
                counts += 1
                if counts == 2:
                    adapter.records[0]['note'] = '读取时发生编辑'
            return original(resource, **kwargs)
        adapter.read = changing
        with self.assertRaisesRegex(StudyReportError, '发生变化'):
            self.read(adapter)

    def test_middle_page_edit_aborts_before_model_or_report_save(self):
        adapter = StudyFixture(201)
        original = adapter.read

        def changing(resource, **kwargs):
            if resource == 'records' and kwargs['offset'] == 200:
                adapter.records[125]['note'] = '中间页记录在读取中被修改，数量和汇总保持原值。'
            return original(resource, **kwargs)

        adapter.read = changing
        client = Mock()
        with patch('study_report.StudyLog', return_value=adapter), \
                self.assertRaisesRegex(StudyReportError, '发生变化'):
            _create_report(self.arguments, self.settings, self.artifacts, CancellationToken(), None, client=client)
        client.json_chat.assert_not_called()
        self.assertEqual(list(self.settings.output_dir.rglob('*.md')), [])

    def test_reconnected_authorization_during_pagination_stops_report(self):
        adapter = StudyLog(self.settings)
        data = StudyFixture(101)
        grants = iter(['a' * 32, 'b' * 32])
        sent_tokens = []

        def request(path, method='GET', body=None, *, token=None, cancellation=None):
            if path == '/api/agent/connect':
                return {'id': next(grants), 'status': 'pending', 'scopes': sorted(READ_SCOPES),
                        'request_expires_at': 9999999999, 'expires_at': None}
            if method == 'DELETE':
                return {'status': 'revoked'}
            sent_tokens.append(token)
            if path.endswith('/projects'):
                return data.read('projects')['data']
            return data.read('records', offset=0, limit=100)['data']

        def progress(stage):
            if '已读取 Study Log 记录 100/101 条' == stage:
                adapter.disconnect()
                adapter.connect()

        client = Mock()
        with patch.object(adapter, '_request', side_effect=request), patch('study_report.StudyLog', return_value=adapter):
            adapter.connect()
            with self.assertRaisesRegex(StudyLogError, '授权已变更'):
                _create_report(self.arguments, self.settings, self.artifacts, CancellationToken(), progress, client=client)
        self.assertEqual(len(sent_tokens), 2)
        self.assertEqual(len(set(sent_tokens)), 1)
        client.json_chat.assert_not_called()
        self.assertEqual(list(self.settings.output_dir.rglob('*.md')), [])

    def test_remote_revocation_stops_model_requests_and_report_save(self):
        adapter = StudyLog(self.settings)
        data = StudyFixture(1)
        grant = self.settings.data_home / 'connections' / 'study-log.json'
        client = Mock()
        for revoke_at, model_calls in [('after_pages', 0), ('after_first_model', 1), ('before_save', 4)]:
            with self.subTest(revoke_at=revoke_at):
                expires = int(datetime.now().timestamp()) + 600
                state = {'status':'connected', 'id':'a' * 32, 'scopes':sorted(READ_SCOPES),
                         'request_expires_at':expires, 'expires_at':expires}
                adapter._save({**state, 'site_url':adapter.base_url, 'token':'slr_' + 'b' * 64})
                project_calls = 0
                local_before = grant.read_bytes()
                client.reset_mock()

                def request(path, method='GET', *, token=None, cancellation=None):
                    nonlocal project_calls
                    self.assertEqual(method, 'GET')
                    self.assertEqual(token, 'slr_' + 'b' * 64)
                    if path == '/api/agent/session':
                        return state.copy()
                    if path.endswith('/projects'):
                        project_calls += 1
                        if project_calls == 2 and revoke_at == 'after_pages':
                            state['status'] = 'revoked'
                        return data.read('projects')['data']
                    return data.read('records', offset=0, limit=100)['data']

                def reply(system, payload, **kwargs):
                    inputs = json.loads(payload)
                    unit = {'title':'递归', 'content':'递归需要终止条件。', 'source_ids':['study-record-1']}
                    if inputs.get('source_review'):
                        return {'overview':inputs['overview'], 'units':inputs['units'], 'unsupported_unit_ids':[]}
                    if 'summary' in inputs:
                        return {'suggestions':[]}
                    if 'notes' in inputs:
                        unit['merged_from'] = [note['id'] for note in inputs['notes']]
                    elif revoke_at == 'after_first_model':
                        state['status'] = 'revoked'
                    return {'overview':'递归学习记录', 'units':[unit]}

                def progress(message):
                    if revoke_at == 'before_save' and message == '保存学习报告到本地内容库…':
                        state['status'] = 'revoked'

                client.json_chat.side_effect = reply
                with patch.object(adapter, '_request', side_effect=request), patch('study_report.StudyLog', return_value=adapter):
                    with self.assertRaisesRegex(StudyLogError, '已撤销'):
                        _create_report(self.arguments, self.settings, self.artifacts, CancellationToken(), progress, client=client)
                self.assertEqual(client.json_chat.call_count, model_calls)
                self.assertEqual(list(self.settings.output_dir.rglob('*.md')), [])
                self.assertEqual(grant.read_bytes(), local_before)

    def test_unchanged_three_page_snapshot_rechecks_all_offsets(self):
        adapter = StudyFixture(201)
        records, _, _ = self.read(adapter)
        self.assertEqual(len(records), 201)
        self.assertEqual([q['offset'] for r, q in adapter.calls if r == 'records'], [0, 100, 200, 0, 100, 200])

    def test_aggregate_disagreement_rejects_report(self):
        records, projects, aggregates = self.read(StudyFixture(1))
        with self.assertRaisesRegex(StudyReportError, '汇总不一致'):
            _statistics(records, projects, (aggregates[0], 999))

    def test_empty_period_saves_one_true_zero_record_document_without_model(self):
        client = Mock()
        with patch('study_report.StudyLog', return_value=StudyFixture(0)):
            result = _create_report(self.arguments, self.settings, self.artifacts,
                                    CancellationToken(), None, client=client)
        files = list(self.settings.output_dir.rglob('*.md'))
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].parent.name, 'study_report')
        self.assertIn('没有学习或实验记录', files[0].read_text())
        self.assertEqual(result['statistics']['records'], 0)
        client.json_chat.assert_not_called()

    def test_model_report_uses_actual_sources_and_saves_complete_document(self):
        client = SimpleNamespace(settings=self.settings, timeout=180)
        responses = iter([
            {'overview': '递归', 'units': [{'title': '终止条件', 'content': '递归需要终止条件。', 'source_ids': ['study-record-1']}]},
            {'overview': '递归', 'units': [{'title': '终止条件', 'content': '递归需要终止条件。', 'source_ids': ['study-record-1'], 'merged_from': ['n1_1']}]},
            {'markdown': '## 学习内容\n\n即使没有终止条件也能结束递归。本周已完成整个项目。（记录 #1）',
             'unit_ids': ['k1'], 'suggestions': [{'action': '练习给递归编写终止条件。', 'unit_ids': ['k1']}]},
        ])
        def respond(system, payload, **kwargs):
            data = json.loads(payload)
            if data.get('source_review'):
                return {'overview': '复核后的递归概述。', 'units': [
                    {**unit, 'content': '递归需要终止条件。当前记录只记下了这个概念。'} for unit in data['units']],
                        'unsupported_unit_ids': []}
            return next(responses)
        client.json_chat = Mock(side_effect=respond)
        with patch('study_report.StudyLog', return_value=StudyFixture(1)):
            result = _create_report(self.arguments, self.settings, self.artifacts,
                                    CancellationToken(), None, client=client)
        document = self.artifacts.resolve_document(result['note']['id'])['path'].read_text()
        self.assertIn('复核后的递归概述。', document)
        self.assertIn('递归需要终止条件。当前记录只记下了这个概念。', document)
        self.assertNotIn('即使没有终止条件', document)
        self.assertNotIn('本周已完成整个项目', document)
        self.assertIn('## 后续建议\n\n- 练习给递归编写终止条件。', document)
        self.assertIn('记录 #1', document)
        self.assertIn('| 记录数 | 1 |', document)
        self.assertIn('| 总积分 | 6 |', document)
        self.assertIn('知识依据索引', document)
        self.assertEqual(client.json_chat.call_count, 4)
        calls = [json.loads(call.args[1]) for call in client.json_chat.call_args_list]
        self.assertIn('blocks', calls[0])
        self.assertFalse(calls[0].get('source_review'))
        self.assertIn('notes', calls[1])
        self.assertTrue(calls[2]['source_review'])
        self.assertEqual(calls[2]['blocks'], calls[0]['blocks'])
        self.assertIn('summary', calls[3])
        self.assertEqual(len(list(self.settings.output_dir.rglob('*.md'))), 1)

    def test_suggestions_can_be_empty_and_cannot_cite_unknown_knowledge(self):
        client = Mock()
        bundle = {'overview': '', 'processing': {},
                  'units': [{'id': 'k1', 'title': '终止条件', 'content': '递归需要终止条件。',
                             'source_ids': ['study-record-1']}],
                  'sources': {'study-record-1': {'record_id': 1, 'date': '2026-10-29', 'project_name': '学习项目'}}}
        with patch('study_report.StudyLog', return_value=StudyFixture(1)), patch('study_report.SummaryEngine') as engine:
            engine.return_value.summarize.return_value.as_dict.return_value = bundle
            client.json_chat.return_value = {'suggestions': [{'action': '练习递归。', 'unit_ids': ['invented']}]}
            with self.assertRaisesRegex(StudyReportError, '实际知识依据'):
                _create_report(self.arguments, self.settings, self.artifacts, CancellationToken(), None, client=client)
            self.assertEqual(list(self.settings.output_dir.rglob('*.md')), [])
            client.json_chat.return_value = {'suggestions': []}
            result = _create_report(self.arguments, self.settings, self.artifacts, CancellationToken(), None, client=client)
        document = self.artifacts.resolve_document(result['note']['id'])['path'].read_text()
        self.assertIn('递归需要终止条件。', document)
        self.assertNotIn('## 后续建议', document)

    def test_relative_weeks_and_explicit_range_limits(self):
        with patch('study_report.datetime') as clock:
            clock.now.return_value = datetime(2026, 10, 30, 12)
            this, last = _period({'period': 'this_week'}), _period({'period': 'last_week'})
        self.assertEqual((this['start_date'], this['end_date']), ('2026-10-26', '2026-10-30'))
        self.assertEqual((last['start_date'], last['end_date']), ('2026-10-19', '2026-10-25'))
        for start, end in [('2026-01-01', '2026-04-01'), ('2026-10-30', '2026-10-01'), ('2026-02-30', '2026-03-01')]:
            with self.subTest(start=start, end=end), self.assertRaises(StudyReportError):
                _period({'period': 'range', 'start_date': start, 'end_date': end})


if __name__ == '__main__':
    unittest.main()
