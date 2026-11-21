"""Local HTTP integration checks; opt in with AGENT_HTTP_TESTS=1.

Starts a loopback server on an unused port with disposable data. No external
requests, account grants, model API calls or installed-app launches are made.
"""
import hashlib
import http.client
import io
import json
import logging
import os
import re
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path
from urllib.parse import quote, urljoin
from unittest.mock import Mock, patch

from harness import Settings
from web_server import LocalHTTPServer, WebRuntime


@unittest.skipUnless(os.environ.get('AGENT_HTTP_TESTS') == '1', '需要显式启用本机 HTTP 验收')
class HTTPSurface(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        with patch.dict(os.environ, {'MINIMAL_AGENT_HOME': temporary.name,
                'MINIMAL_AGENT_OUTPUT': str(Path(temporary.name) / 'summaries'),
                'DEEPSEEK_API_KEY': '', 'MINIMAL_AGENT_MAX_PARALLEL': '2'}):
            self.settings = Settings()
        self.runtime = WebRuntime(self.settings, logging.getLogger('http-acceptance'))
        self.addCleanup(self.runtime.close)
        self.server = LocalHTTPServer(('127.0.0.1', 0), self.runtime)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)

    def request(self, path, method='GET', body=None, headers=None, token=True):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        data = body if isinstance(body, bytes) else json.dumps(body).encode() if body is not None else None
        request_headers = {'Content-Type': 'application/json'}
        if token:
            request_headers['X-Agent-Token'] = self.runtime.token
        request_headers.update(headers or {})
        try:
            connection.request(method, path, body=data, headers=request_headers)
            response = connection.getresponse()
            raw = response.read()
            value = json.loads(raw) if response.getheader('Content-Type', '').startswith('application/json') else raw
            return response.status, value, dict(response.getheaders())
        finally:
            connection.close()

    def test_missing_token_external_origin_and_invalid_host_are_rejected(self):
        for headers, token in [({}, False), ({'Origin': 'https://untrusted.invalid'}, True),
                               ({'Host': 'untrusted.invalid'}, True), ({'Sec-Fetch-Site': 'cross-site'}, True)]:
            with self.subTest(headers=headers):
                code, _, _ = self.request('/api/conversations/new', 'POST', {}, headers=headers, token=token)
                self.assertEqual(code, 403)
        self.assertEqual(self.runtime.chat.list(), [])

    def test_simultaneous_first_read_and_send_share_one_default_conversation(self):
        from concurrent.futures import ThreadPoolExecutor

        self.assertEqual(self.runtime.chat.list(), [])
        self.assertEqual(self.request('/api/config', 'POST', {'api_key': 'synthetic-first-chat'})[0], 200)
        start = threading.Barrier(2)
        legacy_creation = threading.Barrier(2)
        create = self.runtime.chat.new

        def synchronized_creation():
            # On the broken path, both requests reach explicit creation before either inserts.
            legacy_creation.wait(timeout=3)
            return create()

        def request(*args):
            start.wait(timeout=3)
            return self.request(*args)

        message = '空工作区发送第一条消息。'
        model = Mock()
        model.stream_chat.return_value = {'role': 'assistant', 'content': '第一条回复。'}
        with patch.object(self.runtime.chat, 'new', side_effect=synchronized_creation), \
             patch('conversation.create_client', return_value=model), ThreadPoolExecutor(2) as worker:
            original = worker.submit(request, '/api/conversations/current')
            sending = worker.submit(request, '/api/chat/start', 'POST', {'message': message, 'web_enabled': False})
            code, current, _ = original.result(timeout=5)
            created_code, submitted, _ = sending.result(timeout=5)
            run_id = submitted['run_id']
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                _, run, _ = self.request('/api/chat/runs/' + run_id)
                if run.get('conversation'):
                    break
                time.sleep(.02)
            self.assertEqual(run['run']['status'], 'completed')
        self.assertEqual((code, created_code), (200, 202))
        self.assertEqual(current['conversation_id'], submitted['conversation']['conversation_id'])
        model.stream_chat.assert_called_once()
        self.assertEqual(len(self.runtime.chat.list()), 1)
        self.assertEqual([item['content'] for item in self.runtime.chat.current()['messages']], [message, '第一条回复。'])

    def test_saturated_delegation_delivers_older_results_to_busy_original_conversation(self):
        release_workers, release_reply, replying = threading.Event(), threading.Event(), threading.Event()
        started = []
        model = type('HoldingReply', (), {})()

        def stream(*args, **kwargs):
            replying.set()
            if not release_reply.wait(12):
                raise AssertionError('held conversation was not released')
            return {'role': 'assistant', 'content': '原会话的回答已结束。'}

        model.stream_chat = stream

        def execute(capability, parameters, settings, **kwargs):
            title = parameters['title']
            started.append(title)
            if not release_workers.wait(12):
                raise AssertionError('held workers were not released')
            if title == '任务 2':
                raise RuntimeError('合成独立失败')
            path = settings.output_dir / 'paper_summary' / (title + '.md')
            path.parent.mkdir(exist_ok=True)
            path.write_text('# ' + title)
            return {'title': title, 'path': str(path), 'markdown': '# ' + title}

        def wait_for(read, predicate):
            deadline = time.monotonic() + 4
            while time.monotonic() < deadline:
                value = read()
                if predicate(value):
                    return value
                time.sleep(.02)
            self.fail('未观察到预期的并行或回传状态')

        origin = self.runtime.chat.new()['conversation_id']
        other = self.runtime.chat.new()['conversation_id']
        self.assertEqual(self.request('/api/config', 'POST', {'api_key': 'synthetic-parallel-key'})[0], 200)
        batch = [{'agent_id': 'paper_reading', 'capability': 'paper_summary',
                  'parameters': {'title': f'任务 {i}', 'paper_text': '用于并行验收的合成论文材料。' * 30}}
                 for i in range(1, 9)]

        def status(task):
            return self.request('/api/tasks/' + task['id'])[1]['task']['status']

        def deliveries():
            with self.runtime.chat._connect() as db:
                rows = db.execute("SELECT actions FROM conversation_messages WHERE conversation_id=? AND origin='task_delivery'", (origin,)).fetchall()
            return [json.loads(row[0])[0]['task_id'] for row in rows]

        with patch('conversation.create_client', return_value=model), patch('web_server.execute_capability', side_effect=execute), patch('web_server.notify_local'):
            try:
                self.assertEqual(self.request('/api/chat/start', 'POST', {'conversation_id': origin,
                    'message': '保持原会话忙碌', 'web_enabled': False})[0], 202)
                self.assertTrue(replying.wait(3))
                code, result, _ = self.request('/api/subagents/delegate', 'POST', {'conversation_id': origin, 'tasks': batch})
                self.assertEqual(code, 202, result)
                tasks = result['tasks']
                wait_for(self.runtime.executor.status, lambda state: state == {'limit': 2, 'active': 2, 'queued': 6})
                self.assertCountEqual(wait_for(lambda: list(started), lambda items: len(items) == 2),
                                      ['任务 1', '任务 2'])
                self.assertEqual(self.request('/api/tasks/' + tasks[2]['id'] + '/cancel', 'POST', {})[1]['status'], 'cancelled')
                # Newer records from another conversation evict this entire batch from the global recent list.
                with self.runtime.tasks._connect() as db:
                    db.executemany("""INSERT INTO ui_tasks(id,url,title,status,stage,created_at,include_frames,model,conversation_id)
                        VALUES(?,'','其他会话','completed','完成',?,0,'fixture',?)""",
                        [(f'fixture-{i}', time.time() + 1, other) for i in range(101)])
                self.assertFalse({task['id'] for task in tasks} & {task['id'] for task in self.runtime.tasks.list()})
                release_workers.set()
                terminal = wait_for(lambda: [status(task) for task in tasks],
                                    lambda states: not {'queued', 'running'}.intersection(states))
                self.assertEqual(terminal, ['completed', 'failed', 'cancelled'] + ['completed'] * 5)
                self.assertNotIn('任务 3', started)
                self.assertEqual(deliveries(), [])
                release_reply.set()

                delivered = wait_for(deliveries, lambda items: len(items) == len(tasks))
                self.assertCountEqual(delivered, [task['id'] for task in tasks])
                conversation = self.request('/api/conversations/' + origin)[1]
                actual = {action['task_id']: action['task_status'] for message in conversation['messages']
                          for action in message['actions'] if action.get('task_status')}
                self.assertEqual(actual, dict(zip((task['id'] for task in tasks), terminal)))
                self.assertCountEqual([path.stem for path in self.settings.output_dir.rglob('*.md')],
                                      ['任务 1', '任务 4', '任务 5', '任务 6', '任务 7', '任务 8'])
                self.assertCountEqual(deliveries(), delivered)
            finally:
                release_workers.set()
                release_reply.set()

    def test_delegation_write_failure_does_not_enqueue_any_member(self):
        self.assertEqual(self.request('/api/config', 'POST', {'api_key': 'synthetic-batch-key'})[0], 200)
        tasks = [{'agent_id': 'paper_reading', 'capability': 'paper_summary', 'parameters': {
            'title': title, 'paper_text': '批量委派验收材料。' * 40}} for title in ('第一项', '拒绝写入')]
        wrong_owner = [tasks[0], {**tasks[1], 'agent_id': 'video_learning'}]
        self.assertEqual(self.request('/api/subagents/delegate', 'POST', {'tasks': wrong_owner})[0], 400)
        incomplete = [tasks[0], {key: value for key, value in tasks[1].items() if key != 'agent_id'}]
        self.assertEqual(self.request('/api/subagents/delegate', 'POST', {'tasks': incomplete})[0], 400)
        self.assertEqual(self.runtime.tasks.list(), [])
        with self.runtime.tasks._connect() as db:
            db.execute("""CREATE TRIGGER reject_second_task BEFORE INSERT ON ui_tasks
                WHEN json_extract(NEW.parameters, '$.title') = '拒绝写入'
                BEGIN SELECT RAISE(ABORT, 'synthetic batch write failure'); END""")
        with patch.object(self.runtime.executor, 'submit') as submit, \
             patch('web_server.execute_capability') as execute:
            code, payload, _ = self.request('/api/subagents/delegate', 'POST', {'tasks': tasks})
        self.assertEqual(code, 503)
        self.assertIn('整批未开始', payload['error'])
        submit.assert_not_called()
        execute.assert_not_called()
        self.assertEqual(self.runtime.tasks.list(), [])

    def test_delegation_queue_rejection_records_every_member_as_not_started(self):
        self.assertEqual(self.request('/api/config', 'POST', {'api_key': 'synthetic-batch-key'})[0], 200)
        self.runtime.executor.shutdown(wait=True, cancel_futures=True)
        tasks = [{'agent_id': 'paper_reading', 'capability': 'paper_summary', 'parameters': {
            'title': title, 'paper_text': '批量委派验收材料。' * 40}} for title in ('第一项', '第二项')]
        with patch('web_server.execute_capability') as execute:
            code, payload, _ = self.request('/api/subagents/delegate', 'POST', {'tasks': tasks})
        self.assertEqual(code, 503)
        self.assertIn('整批未开始', payload['error'])
        execute.assert_not_called()
        persisted = self.runtime.tasks.list()
        self.assertCountEqual([task['parameters']['title'] for task in persisted], ['第一项', '第二项'])
        self.assertEqual({task['status'] for task in persisted}, {'failed'})
        self.assertEqual(len({task['group_id'] for task in persisted}), 1)

    def test_instant_failure_redacts_progress_and_preserves_failure_reason(self):
        credential = 'synthetic-instant-task-secret'
        self.assertEqual(self.request('/api/config', 'POST', {'api_key': credential})[0], 200)
        observed = []
        notified = threading.Event()

        def execute(capability, parameters, settings, *, progress, **kwargs):
            progress('连接模型 ' + credential)
            observed.append(self.runtime.tasks.list()[0]['stage'])
            raise RuntimeError('模型握手失败：' + credential)

        with patch('web_server.execute_capability', side_effect=execute), \
             patch('web_server.notify_local', side_effect=lambda *_: notified.set()):
            code, data, _ = self.request('/api/tasks', 'POST', {'capability': 'paper_summary',
                'parameters': {'paper_text': '用于验收的合成研究材料。' * 25}})
            self.assertEqual(code, 202, data)
            self.assertTrue(notified.wait(3))
            _, payload, _ = self.request('/api/tasks/' + data['task']['id'])
        self.assertEqual(observed, ['连接模型 [已隐藏]'])
        task = payload['task']
        self.assertEqual((task['status'], task['stage'], task['error']),
                         ('failed', '失败于：连接模型 [已隐藏]', '模型握手失败：[已隐藏]'))
        self.assertNotIn(credential, json.dumps(payload))
        self.assertIsNone(task['output_file'])

    def test_report_evidence_readable_without_exposing_other_json(self):
        assets = self.settings.output_dir / 'paper_summary' / 'sample_assets'
        assets.mkdir(parents=True)
        (assets / 'source-evidence.json').write_text('{"version":1}')
        (assets / 'private.json').write_text('{"private":true}')
        self.assertEqual(self.request('/outputs/paper_summary/sample_assets/source-evidence.json')[:2],
                         (200, {'version': 1}))
        self.assertEqual(self.request('/outputs/paper_summary/sample_assets/private.json')[0], 404)

    def test_chat_task_delivery_source_resolves_attachments_over_http(self):
        from tests.test_content_lifecycle import PNG
        name = 'paper_summary/chat_report.md'
        report = self.settings.output_dir / name
        assets = report.with_name('chat_report_assets')
        assets.mkdir(parents=True)
        evidence = b'{"version":1,"source":"synthetic chat delivery"}\n'
        plan = b'{"slides":[]}\n'
        deck = b'synthetic pptx for HTTP transport'
        (assets / 'source-evidence.json').write_bytes(evidence)
        (assets / 'frame.png').write_bytes(PNG)
        (assets / 'presentation-plan.json').write_bytes(plan)
        (assets / 'presentation.pptx').write_bytes(deck)
        (assets / 'private.pptx').write_bytes(b'not a report attachment')
        markdown = ('# 合成报告\n\n[原文依据](chat_report_assets/source-evidence.json)\n\n'
                    '![合成配图](chat_report_assets/frame.png)\n\n'
                    '[PPT 编排](chat_report_assets/presentation-plan.json)\n\n'
                    '[下载 PPT](chat_report_assets/presentation.pptx)\n')
        report.write_text(markdown, encoding='utf-8')
        code, conversation, _ = self.request('/api/conversations/new', 'POST', {})
        self.assertEqual(code, 201)
        task = self.runtime.tasks.create('', False, 'fixture', capability='paper_summary',
            parameters={'text': '合成材料'}, title='对话附件验收')
        self.runtime.tasks.update(task['id'], conversation_id=conversation['conversation_id'],
            status='completed', stage='完成', markdown=markdown, output_file=name)

        code, delivered, _ = self.request('/api/conversations/' + conversation['conversation_id'])
        self.assertEqual(code, 200)
        message = next(item for item in delivered['messages']
                       if any(action.get('task_id') == task['id'] for action in item['actions']))
        self.assertEqual(message['source_file'], name)
        self.assertIn(markdown, message['content'])
        references = re.findall(r'\]\(([^)]+)\)', message['content'])
        self.assertEqual(len(references), 4)
        # Resolve the delivered Markdown with the public source-file contract,
        # then inspect actual responses rather than reproducing the UI helper.
        base = '/outputs/' + quote(message['source_file'], safe='/')
        presentation_mime = 'application/vnd.openxmlformats-officedocument.presentationml.presentation'
        for reference, expected, mime in zip(references, (evidence, PNG, plan, deck),
                                             ('application/json', 'image/png', 'application/json', presentation_mime)):
            with self.subTest(reference=reference):
                connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
                try:
                    connection.request('GET', urljoin(base, reference),
                                       headers={'X-Agent-Token': self.runtime.token})
                    response = connection.getresponse()
                    self.assertEqual(response.status, 200)
                    self.assertTrue(response.getheader('Content-Type', '').startswith(mime))
                    if mime == presentation_mime:
                        self.assertEqual(response.getheader('Content-Disposition'), 'attachment; filename=presentation.pptx')
                    self.assertEqual(response.read(), expected)
                finally:
                    connection.close()
        deck_url = urljoin(base, references[-1])
        self.assertEqual(self.request(urljoin(base, 'chat_report_assets/private.pptx'))[0], 404)
        code, archive, _ = self.request('/api/export?file=' + quote(name, safe=''))
        self.assertEqual(code, 200)
        with zipfile.ZipFile(io.BytesIO(archive)) as exported:
            self.assertEqual(exported.read(str(Path(name).parent / references[-1])), deck)
            self.assertEqual(exported.read(str(Path(name).parent / references[-2])), plan)

        note = next(item for item in self.runtime.artifacts().list() if item['name'] == name)
        code, removed, _ = self.request('/api/artifacts/delete', 'POST', {'artifact_ids': [note['id']]})
        self.assertEqual(code, 200)
        endpoint = '/api/conversations/' + conversation['conversation_id']

        def saved_delivery():
            payload = self.request(endpoint)[1]
            return next(item for item in payload['messages'] if item['id'] == message['id'])

        hidden = saved_delivery()
        self.assertIsNone(hidden['source_file'])
        self.assertNotIn(markdown, hidden['content'])
        self.assertIn('回收站', hidden['content'])
        self.assertEqual(hidden['actions'][0]['task_status'], 'deleted')
        self.assertEqual(self.request('/outputs/' + name)[0], 404)
        self.assertEqual(self.request(deck_url)[0], 404)

        self.assertEqual(self.request('/api/config', 'POST', {'api_key': 'synthetic-trash-key'})[0], 200)
        client = Mock()
        client.stream_chat.return_value = {'role': 'assistant', 'content': '这是一条独立的回答。'}
        with patch('conversation.create_client', return_value=client):
            code, _, _ = self.request('/api/chat', 'POST', {'conversation_id': conversation['conversation_id'],
                                                            'message': '请核对目前的笔记状态。'})
        self.assertEqual(code, 200)
        model_history = client.stream_chat.call_args.args[0]
        self.assertFalse(any(markdown in item.get('content', '') for item in model_history))
        self.assertTrue(any('回收站' in item.get('content', '') for item in model_history if item['role'] == 'assistant'))

        self.assertEqual(self.request('/api/artifacts/restore', 'POST', {'trash_ids': [removed['deleted'][0]['trash_id']]})[0], 200)
        self.assertEqual(saved_delivery(), message)
        self.assertEqual(self.request('/outputs/' + name)[0], 200)
        self.assertEqual(self.request('/outputs/paper_summary/chat_report_assets/source-evidence.json')[1], json.loads(evidence))
        self.assertEqual(self.request('/outputs/paper_summary/chat_report_assets/frame.png')[1], PNG)
        self.assertEqual(self.request(deck_url)[:2], (200, deck))

    def test_personal_qq_loopback_bridge_and_confirmed_reply(self):
        from concurrent.futures import ThreadPoolExecutor
        from http.server import BaseHTTPRequestHandler, HTTPServer
        sent = []
        held_page, release_page = threading.Event(), threading.Event()
        bridge_calls = []
        history_calls = []
        class Fixture(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_POST(self):
                if self.headers.get('Authorization') != 'Bearer fixture':
                    self.send_error(401); return
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                bridge_calls.append(self.path)
                if self.path == '/send_private_msg':
                    sent.append(body)
                if self.path == '/get_friend_msg_history':
                    history_calls.append(body)
                result = {'/get_login_info': {'user_id':123}, '/get_group_list': [],
                          '/get_friend_list':[{'user_id':55,'nickname':'合成联系人'}],
                          '/get_friend_msg_history':{'messages':[{'message_type':'private','user_id':55,
                              'time':10,'message_id':7,'real_seq':'97','message':[{'type':'text','data':{'text':'合成消息'}}]}]},
                          '/send_private_msg':{'message_id':88}}[self.path]
                if self.path == '/get_friend_msg_history':
                    original = result['messages'][0]
                    if body.get('message_seq') == '7':
                        result = {'messages': [original, {**original, 'time': 8, 'message_id': 6, 'real_seq': '96'}]}
                    elif body.get('message_seq') == '6':
                        result = {'messages': []}
                    elif len(history_calls) == 4:
                        held_page.set()
                        if not release_page.wait(5):
                            self.send_error(504)
                            return
                        result = {'messages': [{**original, 'message_id': i} for i in range(100)]}
                    else:
                        result = {'messages': [
                            {**original, 'message_id': 8, 'real_seq': 98, 'target_id': 123, 'sub_type': 'friend'},
                            original,
                            {**original, 'message_id': 5, 'time': 9, 'sub_type': 'group',
                             'message': '群临时私聊'},
                            {**original, 'message_id': 4, 'time': 3, 'target_id': 777,
                             'message': '其他收件人'},
                            {**original, 'message_id': 3, 'time': 1, 'target_id': 777},
                        ]}
                data = json.dumps({'status':'ok','retcode':0,'data':result}).encode()
                # The second send drops the connection after a valid JSON prefix,
                # before delivering the advertised length.
                truncated = self.path == '/send_private_msg' and len(sent) == 2
                self.send_response(200);self.send_header('Content-Length',str(len(data) + int(truncated)))
                self.end_headers();self.wfile.write(data)
        bridge=HTTPServer(('127.0.0.1',0),Fixture)
        thread=threading.Thread(target=bridge.serve_forever,daemon=True);thread.start()
        try:
            for path in ('/qq.html','/qq.js'):
                self.assertEqual(self.request(path)[0],200)
            self.assertEqual(self.request('/api/qq/connect','POST',{},token=False)[0],403)
            bridge_settings = {'token':'fixture', 'port':bridge.server_port}
            invalid = self.request('/api/qq/connect','POST',{**bridge_settings, 'expected_account':123})
            self.assertEqual(invalid[0],400)
            self.assertEqual(bridge_calls, [])
            rejected = self.request('/api/qq/connect','POST',{**bridge_settings, 'expected_account':'999'})
            self.assertEqual(rejected[0],409)
            self.assertIn('当前登录 QQ 123', rejected[1]['error'])
            self.assertNotIn('session', rejected[1])
            self.assertEqual(bridge_calls, ['/get_login_info'])
            code, connection, _ = self.request('/api/qq/connect','POST',{**bridge_settings, 'expected_account':'123'})
            self.assertEqual(code,200);self.assertEqual(connection['account'],'123')
            args={'peer':'friend:55','session':connection['session']}
            def call(action,**fields):return self.request('/api/qq/'+action,'POST',{**args,**fields})
            self.assertEqual(call('select')[0],200)
            read = call('read', **{'from': 2, 'to':20})[1]
            self.assertEqual([row['id'] for row in read['messages']], ['6', '7', '8'])
            self.assertIn('已读取 3 页', read['scope'])
            for unrelated in ('3', '4', '5'):
                self.assertEqual(call('preview', message_id=unrelated, text='合成答复')[0], 400)
            model = Mock()
            model.json_chat.return_value = {'summary': [{'text': '合成事项', 'ids': ['7']}], 'todos': []}
            with patch('agent_profiles.create_client', return_value=model), patch('agent_profiles.resolve_agent'):
                self.assertEqual(call('summarize')[0], 200)
            model_rows = json.loads(model.json_chat.call_args.args[1])
            self.assertEqual([row['id'] for row in model_rows], ['6', '7', '8'])
            ticket=call('preview',message_id='7',text='合成答复')[1]
            self.assertEqual(sent,[])
            self.assertEqual(call('confirm',ticket=ticket['id'])[1]['status'],'confirmed')
            duplicate=call('confirm',ticket=ticket['id'])
            self.assertEqual(duplicate[0],409)
            self.assertNotIn('code',duplicate[1])
            self.assertEqual(sent,[{'message':[{'type':'text','data':{'text':'合成答复'}}],'user_id':55}])
            second = call('preview', message_id='7', text='回执将中断')[1]
            self.assertEqual(call('confirm', ticket=second['id'])[1]['status'], 'unknown')
            self.assertEqual(call('confirm', ticket=second['id'])[0], 409)
            self.assertEqual(len(sent), 2)
            with ThreadPoolExecutor(2) as worker:
                pending = worker.submit(call, 'read', **{'from':0,'to':20})
                try:
                    self.assertTrue(held_page.wait(2))
                    stop = worker.submit(call, 'disconnect')
                    self.assertEqual(stop.result(timeout=2)[0], 200)
                    self.assertFalse(pending.done())
                finally:
                    release_page.set()
                self.assertEqual(pending.result(timeout=3)[0], 409)
            self.assertEqual([request.get('message_seq') for request in history_calls], [None, '7', '6', None])
            self.assertTrue(all(request.get('disable_get_url') is True and request.get('parse_mult_msg') is False
                                for request in history_calls))
            stale=call('read',**{'from':0,'to':20})
            self.assertEqual(stale[0],409)
            self.assertEqual(stale[1]['code'],'qq_session_changed')
        finally:
            bridge.shutdown();bridge.server_close();thread.join(timeout=2)

    def test_personal_qq_icqq_history_and_string_receipt(self):
        from http.server import BaseHTTPRequestHandler, HTTPServer

        calls = []
        status_responses = []
        supported_actions = ['get_friend_msg_history', 'get_group_msg_history']
        group_latest = {'group_id': 91, 'user_id': 42, 'time': 20, 'message_id': 'Z3JvdXBfbGF0ZXN0',
                        'message_seq': 120, 'message': [{'type': 'at', 'data': {'qq': '123'}}]}
        group_older = {**group_latest, 'time': 10, 'message_id': 'Z3JvdXBfb2xkZXI', 'message_seq': 110,
                       'message': [{'type': 'text', 'data': {'text': '群历史'}}]}
        friend = {'user_id': 55, 'time': 15, 'message_id': 'ZnJpZW5kX2hpc3Q', 'message_seq': 500,
                  'message': [{'type': 'text', 'data': {'text': '私聊历史'}}]}
        other = {**friend, 'user_id': 77, 'message_id': 'dW5zZWxlY3RlZA', 'message_seq': 3}
        wrong_target = {**friend, 'target_id': 77, 'message_id': 'd3JvbmdfdGFyZ2V0', 'message_seq': 2}
        temporary = {**friend, 'sub_type': 'group', 'message_id': 'dGVtcG9yYXJ5', 'message_seq': 1}
        receipt = 'Y29uZmlybWVkX3JlcGx5'

        class Fixture(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                calls.append((self.path, body, self.headers.get('Authorization')))
                sequence = body.get('message_seq')
                result = {
                    '/get_supported_actions': supported_actions,
                    '/get_login_info': {'user_id': 123},
                    '/get_group_list': [{'group_id': 91, 'group_name': '合成群'}],
                    '/get_friend_list': [{'user_id': 55, 'nickname': '合成好友'}],
                    '/get_group_msg_history': {'messages': {None: [group_latest], 120: [group_latest, group_older],
                                                           110: []}.get(sequence, [])},
                    '/get_friend_msg_history': {
                        'messages': {None: [friend, other, wrong_target, temporary], 500: []}.get(sequence, [])},
                    '/send_group_msg': {'message_id': receipt},
                }
                if self.path == '/get_status':
                    response = {'online': status_responses.pop(0) if status_responses else True}
                else:
                    response = result[self.path]
                raw = json.dumps({'status': 'ok', 'retcode': 0, 'data': response}).encode()
                self.send_response(200)
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        bridge = HTTPServer(('127.0.0.1', 0), Fixture)
        thread = threading.Thread(target=bridge.serve_forever, daemon=True)
        thread.start()
        try:
            settings = {'port': bridge.server_port, 'token': 'fixture'}
            invalid = self.request('/api/qq/connect', 'POST', {**settings, 'bridge_kind': 'unknown'})
            self.assertEqual(invalid[0], 400)
            self.assertEqual(calls, [])
            icqq = {**settings, 'bridge_kind': 'icqq'}
            for states, expected_paths in [
                ([False], ['/get_status']),
                ([True, False], ['/get_status', '/get_login_info', '/get_friend_list',
                                 '/get_group_list', '/get_supported_actions', '/get_status']),
            ]:
                status_responses.extend(states)
                code, offline, _ = self.request('/api/qq/connect', 'POST', icqq)
                self.assertEqual(code, 409)
                self.assertIn('QQ 尚未在线', offline['error'])
                self.assertNotIn('session', offline)
                self.assertEqual([path for path, _, _ in calls], expected_paths)
                calls.clear()
            status_responses.extend([True, True])
            status, connection, _ = self.request('/api/qq/connect', 'POST', icqq)
            self.assertEqual(status, 200)
            self.assertEqual(connection['bridge_kind'], 'icqq')
            self.assertEqual(connection['history_support'], {'friend': True, 'group': True})

            def call(action, peer, **fields):
                status, body, _ = self.request('/api/qq/' + action, 'POST', {
                    'peer': peer, 'session': connection['session'], **fields,
                })
                return status, body

            bounds = {'from': 1, 'to': 30}
            self.assertEqual(call('select', 'friend:55')[0], 200)
            status, friends = call('read', 'friend:55', **bounds)
            self.assertEqual(status, 200)
            self.assertEqual([record['id'] for record in friends['messages']], [friend['message_id']])
            self.assertEqual(call('select', 'group:91')[0], 200)
            status, group = call('read', 'group:91', **bounds)
            self.assertEqual(status, 200)
            self.assertEqual([record['id'] for record in group['messages']],
                             [group_older['message_id'], group_latest['message_id']])
            self.assertTrue(group['messages'][-1]['mentions_me'])
            status, ticket = call('preview', 'group:91', message_id=group_latest['message_id'], text='合成回复')
            self.assertEqual(status, 200)
            self.assertFalse(any(path.startswith('/send_') for path, _, _ in calls))
            self.assertEqual(call('confirm', 'group:91', ticket=ticket['id'])[1],
                             {'status': 'confirmed', 'message_id': receipt})
            self.assertEqual(call('confirm', 'group:91', ticket=ticket['id'])[0], 409)

            history = [body for path, body, _ in calls if path.endswith('_history')]
            self.assertEqual(history, [
                {'user_id': 55, 'count': 20}, {'user_id': 55, 'count': 20, 'message_seq': 500},
                {'group_id': 91, 'count': 20}, {'group_id': 91, 'count': 20, 'message_seq': 120},
                {'group_id': 91, 'count': 20, 'message_seq': 110},
            ])
            self.assertEqual([body for path, body, _ in calls if path == '/send_group_msg'],
                             [{'group_id': 91, 'message': [{'type': 'text', 'data': {'text': '合成回复'}}]}])
            self.assertTrue(all(auth == 'Bearer fixture' for _, _, auth in calls))

            def assert_session_ended(response):
                code, error = response
                self.assertEqual(code, 409)
                self.assertEqual(error['code'], 'qq_session_changed')
                self.assertIn('QQ 尚未在线', error['error'])
                self.assertIsNone(self.runtime.qq.session)
                self.assertEqual(self.runtime.qq.messages, {})
                self.assertIsNone(self.runtime.qq.ticket)

            status, ticket = call('preview', 'group:91', message_id=group_latest['message_id'], text='不应提交')
            self.assertEqual(status, 200)
            calls.clear()
            status_responses.append(False)
            assert_session_ended(call('confirm', 'group:91', ticket=ticket['id']))
            self.assertEqual([path for path, _, _ in calls], ['/get_status'])

            _, connection, _ = self.request('/api/qq/connect', 'POST', icqq)
            self.assertEqual(call('select', 'group:91')[0], 200)
            calls.clear()
            status_responses.extend([True, False])
            assert_session_ended(call('read', 'group:91', **bounds))
            self.assertEqual([path for path, _, _ in calls],
                             ['/get_status', '/get_login_info', '/get_group_msg_history', '/get_status'])

            _, connection, _ = self.request('/api/qq/connect', 'POST', icqq)
            self.assertEqual(call('select', 'group:91')[0], 200)
            self.assertEqual(call('read', 'group:91', **bounds)[0], 200)
            with patch.object(self.runtime.qq, '_model_reply', return_value={'text': '旧摘要'}) as reply:
                status_responses.extend([True, False])
                assert_session_ended(call('summarize', 'group:91'))
                reply.assert_called_once()

            supported_actions.remove('get_friend_msg_history')
            code, connection, _ = self.request('/api/qq/connect', 'POST', icqq)
            self.assertEqual(code, 200)
            self.assertEqual(connection['history_support'], {'friend': False, 'group': True})
            self.assertIn('friend:55', connection['peers'])
            self.assertEqual(call('select', 'friend:55')[0], 200)
            calls.clear()
            code, unavailable = call('read', 'friend:55', **bounds)
            self.assertEqual(code, 409)
            self.assertIn('未提供此类会话的历史查询接口', unavailable['error'])
            self.assertEqual(calls, [])
            self.assertEqual(call('select', 'group:91')[0], 200)
            self.assertEqual(call('read', 'group:91', **bounds)[0], 200)

            supported_actions[:] = []
            code, connection, _ = self.request('/api/qq/connect', 'POST', icqq)
            self.assertEqual(code, 200)
            self.assertEqual(connection['history_support'], {})
            self.assertEqual(call('select', 'friend:55')[0], 200)
            self.assertEqual(call('read', 'friend:55', **bounds)[0], 200)
        finally:
            bridge.shutdown()
            bridge.server_close()
            thread.join(timeout=2)

    def test_personal_qq_read_only_and_platform_unread_snapshot(self):
        from http.server import BaseHTTPRequestHandler, HTTPServer
        from qq_unread import UNREAD_ACTION

        calls = []
        scenario = 'valid'
        def message(mid, seq, content):
            return {'message_id': mid, 'message_seq': seq, 'time': 55, 'self_id': '123',
                    'user_id': 22, 'group_id': '9', 'message_type': 'group', 'message': content}

        class Fixture(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                calls.append((self.path, body, self.headers.get('Authorization')))
                rejection = None
                if self.path == '/get_login_info':
                    data = {'user_id': 123}
                elif self.path == '/get_group_info':
                    data = {'group_id': body['group_id'], 'group_name': '合成测试群'}
                elif self.path == '/get_group_msg_history':
                    data = {'messages': [] if 'message_seq' in body else [message('history', '105', '按日期读取')]}
                elif self.path == '/' + UNREAD_ACTION and scenario in ('napcat-unsupported', 'napcat-error'):
                    rejection = '不支持的Api ' + UNREAD_ACTION if scenario == 'napcat-unsupported' else '记录不存在'
                    data = None
                elif self.path == '/' + UNREAD_ACTION and scenario != 'unsupported':
                    later = 'cursor' in body
                    rows = [message('c', '102', '[CQ:at,qq=123]')] if later else [
                        message('a', '97', '核对实验结果'), message('b', '100', '[CQ:image,file=sample]'),
                    ]
                    data = {'version': 1, 'source': 'qq_platform', 'self_id': '123', 'group_id': '9',
                            'snapshot_id': 'snapshot-test', 'unread_count': 3, 'first_unread_seq': '97',
                            'last_unread_seq': '102', 'messages': rows, 'next_cursor': None if later else 'page-2'}
                    if scenario == 'empty':
                        data.update(unread_count=0, first_unread_seq=None, last_unread_seq=None,
                                    messages=[], next_cursor=None)
                    elif scenario == 'other-group' and later:
                        rows[0]['group_id'] = '10'
                    elif scenario == 'other-account' and later:
                        rows[0]['self_id'] = '124'
                    elif scenario == 'count-mismatch':
                        data['unread_count'] = 4
                    elif scenario == 'moved' and later:
                        data['snapshot_id'] = 'different'
                    elif scenario == 'wrong-end':
                        data['last_unread_seq'] = '103'
                    elif scenario == 'repeat-cursor':
                        data.update(unread_count=5, last_unread_seq='104', next_cursor='page-2')
                else:
                    self.send_error(404)
                    return
                raw = json.dumps({'status': 'failed', 'retcode': 200, 'wording': rejection, 'data': None}
                                 if rejection else {'status': 'ok', 'retcode': 0, 'data': data}).encode()
                self.send_response(200)
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        bridge = HTTPServer(('127.0.0.1', 0), Fixture)
        thread = threading.Thread(target=bridge.serve_forever, daemon=True)
        thread.start()
        try:
            settings = {'port': bridge.server_port, 'token': 'fixture', 'test_peer': 'group:9'}
            self.assertEqual(self.request('/api/qq/connect', 'POST', settings)[0], 400)
            self.assertEqual(calls, [])
            self.assertEqual(self.request('/api/qq/connect', 'POST', {**settings, 'expected_account': '124'})[0], 409)
            self.assertEqual([path for path, _, _ in calls], ['/get_login_info'])
            calls.clear()
            code, connected, _ = self.request('/api/qq/connect', 'POST', {**settings, 'expected_account': '123'})
            self.assertEqual(code, 200)
            self.assertTrue(connected['read_only'])
            self.assertEqual(connected['peers'], {'group:9': '合成测试群'})
            self.assertEqual([path for path, _, _ in calls],
                             ['/get_login_info', '/get_group_info', '/get_login_info'])

            def call(action, **body):
                return self.request('/api/qq/' + action, 'POST', {
                    'session': connected['session'], 'peer': 'group:9', **body,
                })[:2]

            self.assertEqual(call('select', peer='group:10')[0], 400)
            self.assertEqual(call('select')[0], 200)
            code, result = call('read-unread')
            self.assertEqual(code, 200)
            self.assertEqual([row['id'] for row in result['messages']], ['a', 'c'])
            self.assertEqual([row['sequence'] for row in result['messages']], ['97', '102'])
            self.assertTrue(result['messages'][1]['mentions_me'])
            self.assertEqual(result['coverage']['source'], 'qq_platform')
            self.assertEqual(result['coverage']['unread_count'], 3)
            self.assertEqual(result['coverage']['first_unread_seq'], '97')
            self.assertEqual(result['coverage']['last_unread_seq'], '102')
            self.assertEqual([body for path, body, _ in calls if path == '/' + UNREAD_ACTION], [
                {'self_id': '123', 'group_id': '9', 'count': 100},
                {'self_id': '123', 'group_id': '9', 'count': 100, 'snapshot_id': 'snapshot-test', 'cursor': 'page-2'},
            ])
            model = Mock()
            model.json_chat.return_value = {'summary': [{'text': '需核对实验结果', 'ids': ['a']}], 'todos': []}
            with patch('agent_profiles.create_client', return_value=model), patch('agent_profiles.resolve_agent'):
                code, summary = call('summarize')
            self.assertEqual(code, 200)
            self.assertEqual(summary['references'], ['a', 'c'])
            self.assertEqual(summary['coverage'], result['coverage'])
            self.assertEqual(call('preview', message_id='a', text='不得外发')[0], 409)
            self.assertEqual(call('confirm', ticket='forged')[0], 409)
            scenario = 'napcat-unsupported'
            code, failure = call('read-unread')
            self.assertEqual(code, 501)
            self.assertIn('当前 QQ 桥接未提供平台未读接口', failure['error'])
            self.assertNotIn('messages', failure)
            self.assertNotIn('coverage', failure)
            self.assertEqual(self.runtime.qq.messages, {})
            self.assertIsNone(self.runtime.qq.scope)
            self.assertIsNone(self.runtime.qq.coverage)
            self.assertIsNone(self.runtime.qq.ticket)
            self.assertEqual(call('summarize')[0], 400)
            code, history = call('read', **{'from': 0, 'to': 100})
            self.assertEqual(code, 200)
            self.assertEqual([item['id'] for item in history['messages']], ['history'])
            for scenario in ('other-group', 'other-account', 'count-mismatch', 'moved', 'wrong-end',
                             'repeat-cursor', 'unsupported', 'napcat-error'):
                with self.subTest(scenario=scenario):
                    code, failure = call('read-unread')
                    self.assertEqual(code, 502)
                    self.assertNotIn('messages', failure)
                    self.assertEqual(call('summarize')[0], 400)
            scenario = 'empty'
            code, empty = call('read-unread')
            self.assertEqual(code, 200)
            self.assertEqual(empty['messages'], [])
            self.assertEqual(empty['coverage']['unread_count'], 0)
            self.assertTrue(all(auth == 'Bearer fixture' for _, _, auth in calls))
            self.assertFalse(any(path.startswith(('/send_', '/mark_')) for path, _, _ in calls))
        finally:
            bridge.shutdown()
            bridge.server_close()
            thread.join(timeout=2)

    def test_status_registry_and_static_dependencies_are_available(self):
        code, status, _ = self.request('/api/status')
        self.assertEqual(code, 200)
        self.assertEqual(status['process_id'], os.getpid())
        self.assertEqual(status['data_dir'], str(self.settings.data_home.resolve()))
        self.assertEqual(status['output_dir'], str(self.settings.output_dir.resolve()))
        self.assertEqual(status['timezone'], 'system-local')
        self.assertIn('timezone_label', status)
        self.assertNotIn('api_key', status)
        code, data, _ = self.request('/api/capabilities')
        self.assertEqual({item['id'] for item in data['capabilities']}, {'video_summary', 'paper_summary', 'study_report'})
        for path in ['/', '/app.js', '/agent_console.js', '/conversation_controls.js', '/study_log.js',
                     '/vendor/marked.umd.js', '/vendor/purify.min.js', '/vendor/mermaid.min.js']:
            with self.subTest(path=path):
                code, content, _ = self.request(path)
                self.assertEqual(code, 200)
                self.assertGreater(len(content), 0)

    def test_model_discovery_uses_only_the_selected_roles_saved_credentials(self):
        from model_transport import ModelTransportError

        def transport(request, timeout):
            self.assertEqual(request.full_url, 'https://api.deepseek.com/models')
            self.assertEqual(request.get_method(), 'GET')
            self.assertIsNone(request.data)
            self.assertEqual(timeout, 15)
            key = request.get_header('Authorization').removeprefix('Bearer ')
            if key == 'synthetic-main-key':
                ids = ['main-flash', 'main-pro', 'main-flash', 'not a model', key, 'x' * 101]
            else:
                self.assertEqual(key, 'synthetic-global-key')
                ids = ['global-flash']
            return io.BytesIO(json.dumps({'data': [{'id': model} for model in ids]}).encode())

        main = '/api/agent-profiles/conversation'
        inherited = '/api/agent-profiles/video_learning'
        global_role = '/api/agent-profiles/global'
        with patch('harness.open_model_response', side_effect=transport) as upstream:
            self.assertEqual(self.request(main + '/models', 'POST', {})[0], 400)
            self.assertEqual(self.request('/api/config', 'POST', {'api_key': 'synthetic-global-key'})[0], 200)
            self.assertEqual(self.request(main, 'POST', {'api_key': 'synthetic-main-key', 'model': 'original-model'})[0], 200)
            self.assertEqual(self.request('/api/agent-profiles')[0], 200)
            self.assertEqual(self.request(main + '/models', 'POST', {}, token=False)[0], 403)
            self.assertEqual(self.request(main + '/models', 'POST', {'api_key': 'unsaved'})[0], 400)
            upstream.assert_not_called()

            for path, models in [(main, ['main-flash', 'main-pro']),
                                 (inherited, ['global-flash']), (global_role, ['global-flash'])]:
                with self.subTest(path=path):
                    code, result, _ = self.request(path + '/models', 'POST', {})
                    self.assertEqual((code, result), (200, {'models': models}))
            self.assertEqual(upstream.call_count, 3)
            _, profiles, _ = self.request('/api/agent-profiles')
            self.assertEqual(next(role['model'] for role in profiles['agents']
                                  if role['role'] == 'conversation'), 'original-model')

            upstream.side_effect = ModelTransportError('拒绝 synthetic-main-key', 'response_body', True)
            code, result, _ = self.request(main + '/models', 'POST', {})
            self.assertEqual((code, result), (502, {'error': '拒绝 [已隐藏凭证]'}))
            upstream.side_effect = None
            upstream.return_value = io.BytesIO(b'{"data":{}}')
            self.assertEqual(self.request(main + '/models', 'POST', {})[:2],
                             (502, {'error': 'DeepSeek 返回了无法识别的模型列表。'}))

    def test_saved_role_profiles_reach_actual_clients_and_preserve_running_snapshots(self):
        from agent_profiles import PROVIDERS
        clients, guard = [], threading.Lock()
        started = {role: threading.Event() for role in ('paper_reading', 'video_learning')}
        release = threading.Event()

        class RecordingClient:
            def __init__(self, settings, timeout=180):
                self.settings, self.timeout, self.calls = settings, timeout, []
                with guard:
                    self.block = settings._profile_role in started and not any(
                        item.settings._profile_role == settings._profile_role for item in clients)
                    clients.append(self)

            def chat(self, messages, **kwargs):
                self.calls.append(messages[0]['content'])
                if self.block:
                    started[self.settings._profile_role].set()
                    if not release.wait(timeout=10):
                        raise RuntimeError('HTTP 验收未释放模型调用。')
                    self.block = False
                return {'role': 'assistant', 'content': '', 'tool_calls': [
                    {'id': str(index), 'type': 'function', 'function': {'name': name, 'arguments': '{}'}}
                    for index, name in enumerate(('produce_result', 'inspect_result', 'finish_task'))]}

            def stream_chat(self, messages, **kwargs):
                self.calls.append(messages[0]['content'])
                return {'role': 'assistant', 'content': '有限图使用已访问集合避免重复遍历。', 'reasoning_content': ''}

            def json_chat(self, system, user, **kwargs):
                self.calls.append(system)
                value = json.loads(user)
                if value.get('source_review'):
                    return {'overview': value['overview'], 'units': value['units'],
                            'unsupported_unit_ids': []}
                if 'blocks' in value or 'notes' in value:
                    sources = ([block['id'] for block in value['blocks']] if 'blocks' in value
                               else list(dict.fromkeys(source for note in value['notes'] for source in note['source_ids'])))
                    unit = {'title': '有限图遍历', 'content': '已访问集合避免重复遍历；图必须有限。',
                            'tags': ['图'], 'source_ids': sources}
                    if 'notes' in value:
                        unit['merged_from'] = [note['id'] for note in value['notes']]
                    return {'overview': '有限图与已访问集合。', 'units': [unit]}
                knowledge = value['knowledge']
                return {'include_overview': False, 'overview_diagram_type': 'none', 'overview_diagram_mermaid': '',
                        'sections': [{'title': '有限图遍历',
                            'unit_ids': [unit['id'] for unit in knowledge['units']],
                            'source_ids': list(knowledge['sources']), 'diagram_type': 'none',
                            'diagram_mermaid': '', 'frame_timestamps': []}]}

        def post(path, body, expected=200):
            code, result, _ = self.request(path, 'POST', body)
            self.assertEqual(code, expected, result)
            return result

        def wait_for(path, key):
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                code, result, _ = self.request(path)
                self.assertEqual(code, 200, result)
                value = result[key]
                if value['status'] not in {'queued', 'running'}:
                    self.assertEqual(value['status'], 'completed', value)
                    return value
                time.sleep(.02)
            self.fail('HTTP 任务在验收时限内未结束。')

        def profile(role, **values):
            return post('/api/agent-profiles/' + role, values)['agent']

        def rule(role, name, marker):
            post('/api/skills/' + name, {'role': role, 'content': marker})

        def task(capability, parameters):
            return post('/api/tasks', {'capability': capability, 'parameters': parameters}, 202)['task']

        def assert_client(client, model, key, skills, marker=None):
            self.assertEqual((client.settings.model, client.settings.api_key, client.settings.skill_names),
                             (model, key, tuple(skills)))
            self.assertTrue(client.calls)
            for system in client.calls:
                if marker:
                    self.assertIn(marker, system)
                else:
                    self.assertNotIn('<skill name=', system)

        url = 'https://www.bilibili.com/video/BV1it411g7CX/'
        material = {'info': {'id': 'profile-fixture', 'title': '有限图遍历', 'webpage_url': url, 'duration': 20},
                    'transcript': [{'start': 0, 'end': 20, 'text': '已访问集合避免重复遍历；图必须有限。'}],
                    'source': '合成字幕', 'language': 'zh'}
        paper = {'paper_text': '有限图使用已访问集合避免重复遍历。研究假设顶点和边均为有限集合，不包含性能测量。' * 8}
        keys = {name: 'fixture-' + name + '-credential' for name in ('global', 'main', 'paper', 'video', 'archive')}
        with patch.dict(PROVIDERS, {'deepseek': RecordingClient}), \
             patch('web_server.resolve_tool', return_value='/fixture/yt-dlp'), \
             patch('video_summary._get_video_material', return_value=material), \
             patch('web_server.notify_local'):
            profile('conversation', model='main-model', api_key=keys['main'], skills=['summary-core'])
            state = self.request('/api/status')[1]
            self.assertFalse(state['api_key_configured'])
            self.assertEqual(state['conversation'], {'provider': 'deepseek', 'model': 'main-model', 'api_key_configured': True})
            profile('global', model='default-model', api_key=keys['global'], skills=['summary-core'])
            rule('global', 'summary-core', 'GLOBAL_PROFILE_RULE')
            rule('conversation', 'summary-core', 'MAIN_PROFILE_RULE')
            profile('paper_reading', model='paper-old', api_key=keys['paper'], skills=['paper-summary'])
            rule('paper_reading', 'paper-summary', 'PAPER_RULE_OLD')
            profile('video_learning', model='video-model', api_key=keys['video'], skills=[])
            profile('knowledge_archivist', model='archive-model', api_key=keys['archive'], skills=['knowledge-extraction'])
            rule('knowledge_archivist', 'knowledge-extraction', 'ARCHIVE_PROFILE_RULE')
            conversation = post('/api/conversations/new', {}, 201)['conversation_id']
            run = post('/api/chat/start', {'message': '说明有限图遍历。', 'conversation_id': conversation,
                       'web_enabled': False}, 202)
            wait_for('/api/chat/runs/' + run['run_id'], 'run')
            try:
                first = task('paper_summary', paper)
                self.assertTrue(started['paper_reading'].wait(timeout=5))
                video = task('video_summary', {'url': url, 'include_frames': False})
                self.assertTrue(started['video_learning'].wait(timeout=5))
                queued = task('paper_summary', paper)
                queued_task = self.request('/api/tasks/' + queued['id'])[1]['task']
                self.assertEqual(queued_task['status'], 'queued')
                self.assertEqual(queued_task['execution_profiles'], [])
                running_profiles = self.request('/api/tasks/' + first['id'])[1]['task']['execution_profiles']
                self.assertEqual(len(running_profiles), 1)
                profile('paper_reading', model='paper-new')
                rule('paper_reading', 'paper-summary', 'PAPER_RULE_NEW')
            finally:
                release.set()
            for item in (first, video, queued):
                wait_for('/api/tasks/' + item['id'], 'task')
            from web_server import UITaskStore
            restarted_store = UITaskStore(self.settings.database_path)
            for task_id, model, marker in [(first['id'], 'paper-old', 'PAPER_RULE_OLD'),
                                           (queued['id'], 'paper-new', 'PAPER_RULE_NEW')]:
                completed = self.request('/api/tasks/' + task_id)[1]['task']
                self.assertEqual(completed['model'], model)
                actual, = completed['execution_profiles']
                loaded_rules = next(client.settings.skill_instructions for client in clients if client.settings.model == model)
                self.assertIn(marker, loaded_rules)
                self.assertEqual(actual, {
                    'role': 'paper_reading', 'provider': 'deepseek', 'model': model, 'skills': ['paper-summary'],
                    'skills_sha256': hashlib.sha256(loaded_rules.encode('utf-8')).hexdigest(),
                    'started_at': actual['started_at'], 'attempt': 1,
                })
                self.assertGreaterEqual(actual['started_at'], completed['created_at'])
                self.assertEqual(restarted_store.get(task_id, with_markdown=True)['execution_profiles'], [actual])
            self.assertEqual(restarted_store.get(first['id'], with_markdown=True)['execution_profiles'], running_profiles)
            self.assertEqual(restarted_store.get(video['id'], with_markdown=True)['execution_profiles'][0]['skills'], [])
            inherited = profile('video_learning', inherit=['model', 'api_key', 'skills'])
            self.assertEqual(inherited['model'], 'default-model')
            self.assertEqual(inherited['sources']['model'], 'default')
            self.assertEqual(inherited['skill_source'], 'global')
            inherited_video = task('video_summary', {'url': url, 'include_frames': False})
            wait_for('/api/tasks/' + inherited_video['id'], 'task')
            archived = post('/api/conversations/' + conversation + '/archive', {'extract_knowledge': True})
            self.assertIsNone(archived['extraction_error'])
            jobs = self.request('/api/knowledge/ingestions')[1]['jobs']
            self.assertEqual(len(jobs), 1)
            wait_for('/api/knowledge/ingestions/' + jobs[0]['id'], 'job')
            main = profile('conversation', inherit=['model', 'api_key', 'skills'])
            self.assertEqual((main['model'], main['sources']['model']), ('default-model', 'default'))
            next_conversation = post('/api/conversations/new', {}, 201)['conversation_id']
            next_run = post('/api/chat/start', {'message': '继续解释。', 'conversation_id': next_conversation,
                            'web_enabled': False}, 202)
            wait_for('/api/chat/runs/' + next_run['run_id'], 'run')
            code, public, _ = self.request('/api/agent-profiles')
            self.assertEqual(code, 200)
            for key in keys.values():
                self.assertNotIn(key, json.dumps(public))
            self.assertEqual(self.request('/api/agent-profiles/paper_reading', 'POST', {'provider': 'unknown'})[0], 400)

        by_role = {role: [client for client in clients if client.settings._profile_role == role]
                   for role in ('conversation', 'paper_reading', 'video_learning', 'knowledge_archivist')}
        self.assertEqual({role: len(items) for role, items in by_role.items()},
                         {'conversation': 2, 'paper_reading': 2, 'video_learning': 2, 'knowledge_archivist': 1})
        assert_client(by_role['conversation'][0], 'main-model', keys['main'], ['summary-core'], 'MAIN_PROFILE_RULE')
        assert_client(by_role['conversation'][1], 'default-model', keys['global'], ['summary-core'], 'MAIN_PROFILE_RULE')
        assert_client(by_role['paper_reading'][0], 'paper-old', keys['paper'], ['paper-summary'], 'PAPER_RULE_OLD')
        self.assertTrue(all('PAPER_RULE_NEW' not in call for call in by_role['paper_reading'][0].calls))
        assert_client(by_role['paper_reading'][1], 'paper-new', keys['paper'], ['paper-summary'], 'PAPER_RULE_NEW')
        assert_client(by_role['video_learning'][0], 'video-model', keys['video'], [])
        assert_client(by_role['video_learning'][1], 'default-model', keys['global'], ['summary-core'], 'GLOBAL_PROFILE_RULE')
        assert_client(by_role['knowledge_archivist'][0], 'archive-model', keys['archive'], ['knowledge-extraction'], 'ARCHIVE_PROFILE_RULE')

    def test_pdf_upload_preserves_bytes_without_starting_a_task(self):
        from tests.test_paper_workflow import sample_pdf
        from paper_summary import PaperUploads
        data = sample_pdf()
        with patch('web_server.execute_capability') as execute:
            code, result, _ = self.request('/api/papers/upload', 'POST', data,
                headers={'Content-Type': 'application/pdf', 'X-File-Name': 'fixture.pdf'})
        self.assertEqual(code, 201)
        path, _ = PaperUploads(self.settings.data_home).get(result['paper']['upload_id'])
        self.assertEqual(path.read_bytes(), data)
        self.assertEqual(self.runtime.tasks.list(), [])
        execute.assert_not_called()
        for data, mime, expected in [(b'not a PDF', 'application/pdf', 400),
                                     (b'{}', 'application/json', 415)]:
            with self.subTest(mime=mime):
                self.assertEqual(self.request('/api/papers/upload', 'POST', data,
                    headers={'Content-Type': mime})[0], expected)

    def test_session_import_queues_original_task_and_clear_blocks_future_import(self):
        from tests.test_paper_workflow import sample_pdf
        url = 'https://ieeexplore.ieee.org/document/11029963'
        task = self.runtime.tasks.create(url, False, 'fixture', capability='paper_summary',
            parameters={'url': url}, title='论文总结')
        with patch('site_sessions._addresses', return_value=['93.184.216.34']):
            session = self.runtime.site_sessions.create(url, task['id'])
        self.runtime.tasks.update(task['id'], status='waiting_user', session_id=session['id'])
        session_path = '/api/site-sessions/' + session['id']
        endpoint = session_path + '/import'
        headers = {'Content-Type': 'application/pdf', 'X-File-Name': 'fixture.pdf'}
        with patch.object(self.runtime.executor, 'submit') as submit:
            code, result, _ = self.request(endpoint, 'POST', sample_pdf(), headers=headers)
        self.assertEqual(code, 202)
        submit.assert_called_once()
        self.assertEqual(result['task']['id'], task['id'])
        self.assertEqual(result['task']['status'], 'queued')
        self.assertEqual(result['task']['attempt'], 2)
        self.assertEqual(len(self.runtime.tasks.list()), 1)
        self.assertEqual(self.runtime.site_sessions.get(session['id'])['status'], 'material_imported')
        for status in ('session_saved', 'access_failed'):
            code, response, _ = self.request(session_path + '/state', 'POST', {'status': status})
            self.assertEqual((code, response['session']['status']), (200, 'material_imported'))
        self.assertEqual(self.request(endpoint, 'POST', sample_pdf(), headers=headers)[0], 409)
        self.assertEqual(len(list(self.settings.data_home.glob('uploads/papers/*/source.pdf'))), 1)
        code, cleared, _ = self.request(session_path + '/state', 'POST', {'status': 'cleared'})
        self.assertEqual(code, 200)
        self.assertEqual(cleared['session']['status'], 'cleared')
        # The desktop reports clearing and completion asynchronously.
        for stale_state in ('clear_pending', 'session_saved', 'cleared'):
            with self.subTest(stale_state=stale_state):
                self.assertEqual(self.request(session_path + '/state', 'POST', {'status': stale_state})[:2],
                                 (200, cleared))
        self.assertEqual(self.request(session_path)[:2], (200, cleared))
        self.assertEqual(self.request(endpoint, 'POST', sample_pdf(), headers=headers)[0], 409)

    def test_cancelled_paper_does_not_accept_a_late_pdf_or_claim_import(self):
        from tests.test_paper_workflow import sample_pdf
        url = 'https://ieeexplore.ieee.org/document/11029963'
        due = time.time() + 3600
        self.runtime.store.add('cancel-with-window', url, 'once', None, due,
                               capability='paper_summary', parameters={'url': url})
        self.runtime.store.record_run('cancel-with-window', due, 'waiting_user', '需要 PDF')
        run, = self.runtime.store.recent_runs()
        occurrence = '/api/schedule-runs/' + str(run['id'])
        with patch('site_sessions._addresses', return_value=['93.184.216.34']):
            code, prepared, _ = self.request(occurrence + '/material', 'POST', {})
        self.assertEqual(code, 200)
        task = prepared['task']
        self.assertEqual(self.request(occurrence + '/cancel', 'POST', {})[1]['status'], 'cancelled')
        path = '/api/site-sessions/' + task['session_id']
        self.assertEqual(self.request(path + '/state', 'POST', {'status': 'material_imported'})[0], 400)
        code, _, _ = self.request(path + '/import', 'POST', sample_pdf(), {'Content-Type': 'application/pdf'})
        self.assertEqual(code, 409)
        self.assertEqual(self.request(path)[1]['session']['status'], 'needs_login')
        self.assertEqual(self.runtime.tasks.get(task['id'])['attempt'], 1)
        self.assertEqual(self.runtime.store.recent_runs()[0]['status'], 'cancelled')
        self.assertFalse(list(self.settings.data_home.glob('uploads/papers/*/source.pdf')))

    def test_cleared_paper_window_can_be_reopened_for_the_same_task(self):
        from concurrent.futures import ThreadPoolExecutor
        from tests.test_paper_workflow import sample_pdf

        url = 'https://ieeexplore.ieee.org/document/11029963'
        task = self.runtime.tasks.create(url, False, 'fixture', capability='paper_summary', parameters={'url': url})
        endpoint = '/api/tasks/' + task['id'] + '/account-session'
        with patch('site_sessions._addresses', return_value=['93.184.216.34']):
            original = self.runtime.site_sessions.create(url, task['id'])
            self.runtime.tasks.update(task['id'], status='waiting_user', session_id=original['id'])
            old_path = '/api/site-sessions/' + original['id']
            self.assertEqual(self.request(old_path + '/state', 'POST', {'status': 'clear_pending'})[0], 200)
            self.assertEqual(self.request(endpoint, 'POST', {})[0], 409)
            self.assertEqual(self.request(old_path + '/state', 'POST', {'status': 'cleared'})[0], 200)
            with ThreadPoolExecutor(2) as executor:
                replacements = list(executor.map(lambda _: self.request(endpoint, 'POST', {}), range(2)))

        self.assertEqual([reply[0] for reply in replacements], [200, 200])
        session = replacements[0][1]['session']
        self.assertEqual(session['id'], replacements[1][1]['session']['id'])
        self.assertNotEqual(session['id'], original['id'])
        self.assertEqual((session['task_id'], session['status']), (task['id'], 'needs_login'))
        self.assertEqual(self.runtime.tasks.get(task['id'])['session_id'], session['id'])

        headers = {'Content-Type': 'application/pdf'}
        self.assertEqual(self.request(old_path + '/import', 'POST', sample_pdf(), headers)[0], 409)
        with patch.object(self.runtime.executor, 'submit') as submit:
            code, imported, _ = self.request('/api/site-sessions/' + session['id'] + '/import', 'POST', sample_pdf(), headers)
        self.assertEqual(code, 202)
        submit.assert_called_once()
        self.assertEqual((imported['task']['id'], imported['task']['attempt']), (task['id'], 2))
        self.assertEqual(self.request(endpoint, 'POST', {})[0], 409)
        self.assertEqual(len(self.runtime.tasks.list()), 1)

    def test_cancel_between_pdf_save_and_task_attachment_discards_unattached_pdf(self):
        from paper_summary import PaperUploads
        from tests.test_paper_workflow import sample_pdf

        url = 'https://ieeexplore.ieee.org/document/11029963'
        task = self.runtime.tasks.create(url, False, 'fixture', capability='paper_summary', parameters={'url': url})
        with patch('site_sessions._addresses', return_value=['93.184.216.34']):
            session = self.runtime.site_sessions.create(url, task['id'])
        self.runtime.tasks.update(task['id'], status='waiting_user', session_id=session['id'])
        save = PaperUploads.save

        def save_then_cancel(uploads, data, filename):
            metadata = save(uploads, data, filename)
            self.runtime.cancel_task(task['id'])
            return metadata

        path = '/api/site-sessions/' + session['id']
        with patch.object(PaperUploads, 'save', save_then_cancel):
            self.assertEqual(self.request(path + '/import', 'POST', sample_pdf(), {'Content-Type': 'application/pdf'})[0], 409)
        self.assertEqual(self.request(path)[1]['session']['status'], 'needs_login')
        self.assertEqual(self.runtime.tasks.get(task['id'])['status'], 'cancelled')
        self.assertFalse(list(self.settings.data_home.glob('uploads/papers/*/source.pdf')))

    def test_two_import_requests_from_new_window_create_only_one_task(self):
        from concurrent.futures import ThreadPoolExecutor
        from tests.test_paper_workflow import sample_pdf

        self.assertEqual(self.request('/api/config', 'POST', {'api_key': 'fixture-paper-credential'})[0], 200)
        url = 'https://ieeexplore.ieee.org/document/11029963'
        with patch('site_sessions._addresses', return_value=['93.184.216.34']):
            code, response, _ = self.request('/api/site-sessions', 'POST', {'url': url})
        self.assertEqual(code, 201)
        path = '/api/site-sessions/' + response['session']['id']

        def upload():
            return self.request(path + '/import', 'POST', sample_pdf(), {'Content-Type': 'application/pdf'})

        with patch.object(self.runtime.executor, 'submit') as submit, ThreadPoolExecutor(2) as requests:
            results = [requests.submit(upload) for _ in range(2)]
            replies = sorted(request.result(timeout=5) for request in results)
        self.assertEqual([reply[0] for reply in replies], [202, 409])
        submit.assert_called_once()
        task, = self.runtime.tasks.list()
        session = self.request(path)[1]['session']
        self.assertEqual((session['task_id'], session['status']), (task['id'], 'material_imported'))
        self.assertEqual(task['session_id'], session['id'])
        self.assertEqual(len(list(self.settings.data_home.glob('uploads/papers/*/source.pdf'))), 1)

    def test_standalone_scheduled_paper_waits_before_model_and_continues_from_original_run(self):
        from agent_profiles import AgentProfiles
        from capabilities import execute_capability
        from paper_summary import read_paper
        from scheduler import Scheduler
        from tests.test_paper_workflow import sample_pdf
        from tests.test_schedule_occurrences import InlineExecutor

        url = 'https://ieeexplore.ieee.org/document/11029963'
        due = time.time() + 3600
        store = self.runtime.store
        profiles = AgentProfiles(self.settings)
        profiles.update('paper_reading', {'model': 'scheduled-original'})
        for job_id in ('waiting-paper', 'cancel-paper'):
            store.add(job_id, url, 'once', None, due, capability='paper_summary', parameters={'url': url})
        conversation_id = self.runtime.chat.current()['conversation_id']
        with self.runtime.chat._connect() as db:
            db.execute("INSERT INTO schedule_conversations VALUES(?,?)", ('waiting-paper', conversation_id))

        def run(capability, parameters, cancellation, progress, settings, logger):
            return execute_capability(capability, parameters, settings, cancellation=cancellation, progress=progress, logger=logger)

        scheduler = Scheduler(store, self.settings, None, self.runtime.logger, executor=InlineExecutor(),
                              controlled_runner=run, settings_provider=self.runtime.snapshot)
        with patch('subagents.run_business_subagent') as subagent, patch('scheduler.notify_local'):
            scheduler.tick(now=due)
        subagent.assert_not_called()
        waiting = {item['job_id']: item for item in store.recent_runs()}
        run_id = waiting['waiting-paper']['id']
        cancel_id = waiting['cancel-paper']['id']
        self.assertEqual(waiting['waiting-paper']['status'], 'waiting_user')
        original_profile = waiting['waiting-paper']['execution_profile']
        self.assertEqual((original_profile['attempt'], original_profile['model']), (1, 'scheduled-original'))
        self.assertEqual(self.runtime.tasks.list(), [])
        self.assertEqual(self.request(f'/api/schedule-runs/{cancel_id}/cancel', 'POST', {})[1]['status'], 'cancelled')
        self.assertEqual(self.request(f'/api/schedule-runs/{cancel_id}/material', 'POST', {})[0], 409)

        for i in range(30):
            store.record_run('history-fixture', float(i), 'completed', '近期历史')
        self.assertNotIn(run_id, [item['id'] for item in store.recent_runs()])
        self.assertIn(run_id, [item['id'] for item in self.request('/api/schedules')[1]['runs']])

        endpoint = f'/api/schedule-runs/{run_id}/material'
        with patch('site_sessions._addresses', return_value=['93.184.216.34']):
            code, prepared, _ = self.request(endpoint, 'POST', {})
            again = self.request(endpoint, 'POST', {})[1]['task']
        self.assertEqual(code, 200)
        task = prepared['task']
        self.assertEqual((again['id'], again['session_id']), (task['id'], task['session_id']))
        self.assertEqual(task['conversation_id'], conversation_id)
        self.assertEqual(len(self.runtime.tasks.list()), 1)

        initial_session = task['session_id']
        self.assertEqual(self.request('/api/site-sessions/' + initial_session + '/state', 'POST', {'status':'cleared'})[0], 200)
        with patch('site_sessions._addresses', return_value=['93.184.216.34']):
            reopened = self.request(endpoint, 'POST', {})[1]['task']
        self.assertEqual(reopened['id'], task['id'])
        self.assertNotEqual(reopened['session_id'], initial_session)
        task = reopened

        def finish(capability, parameters, settings, **kwargs):
            paper = read_paper(parameters, settings, kwargs['cancellation'], kwargs['progress'])
            self.assertEqual((paper['pages'], paper['original_url']), (1, url))
            output = settings.output_dir / 'paper_summary' / 'fixture.md'
            output.parent.mkdir(exist_ok=True)
            output.write_text('# Synthetic PDF report\n')
            return {'path': str(output), 'markdown': output.read_text()}

        headers = {'Content-Type': 'application/pdf', 'X-File-Name': 'fixture.pdf'}
        profiles.update('paper_reading', {'model': 'scheduled-after-import', 'skills': []})
        with patch('web_server.execute_capability', side_effect=finish) as execute, patch('web_server.notify_local'):
            code, imported, _ = self.request('/api/site-sessions/' + task['session_id'] + '/import', 'POST', sample_pdf(), headers)
            self.assertEqual(code, 202)
            self.assertEqual((imported['task']['id'], imported['task']['attempt']), (task['id'], 2))
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                result = next(item for item in self.request('/api/schedules')[1]['runs'] if item['id'] == run_id)
                if result['status'] in {'completed', 'failed'}:
                    break
                time.sleep(.02)
            self.assertEqual(result['status'], 'completed', result['detail'])
            execute.assert_called_once()
        self.assertEqual(result['execution_profile'], original_profile)
        after_import, = self.request('/api/tasks/' + task['id'])[1]['task']['execution_profiles']
        self.assertEqual((after_import['attempt'], after_import['model'], after_import['skills']),
                         (2, 'scheduled-after-import', []))
        self.assertEqual(Path(result['output_path']).read_text(), '# Synthetic PDF report\n')
        self.assertEqual(self.request(endpoint, 'POST', {})[0], 409)

    def test_workbench_can_cancel_a_run_owned_by_a_standalone_scheduler(self):
        from scheduler import Scheduler

        due = time.time() + 3600
        store = self.runtime.store
        store.add('external-run', '', 'once', None, due, capability='study_report', parameters={'period': 'last_week'})
        started = threading.Event()
        stopped = threading.Event()

        def execute(capability, parameters, token, progress):
            token.register(stopped.set)
            started.set()
            stopped.wait(5)
            token.check()
            self.fail('Background execution did not observe cancellation')

        external = Scheduler(store, self.settings, None, self.runtime.logger, controlled_runner=execute)
        try:
            external.tick(now=due)
            self.assertTrue(started.wait(3))
            run, = store.recent_runs()
            endpoint = f'/api/schedule-runs/{run["id"]}/cancel'
            with patch.object(self.runtime, 'scheduler', None):
                code, result, _ = self.request(endpoint, 'POST', {})
                self.assertEqual((code, result['status']), (200, 'cancelled'))
                self.assertFalse(stopped.is_set())
                external._signal_cancelled_runs()
                self.assertTrue(stopped.wait(2))
                self.assertEqual(self.request(endpoint, 'POST', {})[1]['status'], 'cancelled')
            self.assertEqual(store.recent_runs()[0]['status'], 'cancelled')
            self.assertFalse(store.record_run('external-run', due, 'completed', '迟到的完成'))
        finally:
            external.close()
            external.join(timeout=3)

    def test_conversation_archive_and_restore_keep_original_messages(self):
        code, first, _ = self.request('/api/conversations/new', 'POST', {})
        self.assertEqual(code, 201)
        identifier = first['conversation_id']
        self.runtime.chat._save(identifier, {'role': 'user', 'content': '需要保留的测试内容'})
        code, _, _ = self.request('/api/conversations/' + identifier + '/rename', 'POST', {'title': '验收对话'})
        self.assertEqual(code, 200)
        with patch.object(self.runtime.chat.knowledge, 'request') as extract:
            code, archived, _ = self.request('/api/conversations/' + identifier + '/archive', 'POST', {'extract_knowledge': False})
        self.assertEqual(code, 200)
        extract.assert_not_called()
        self.assertTrue(archived['conversation']['archived_at'])
        code, restored, _ = self.request('/api/conversations/' + identifier + '/unarchive', 'POST', {})
        self.assertEqual(code, 200)
        self.assertIsNone(restored['archived_at'])
        self.assertEqual(restored['messages'][0]['content'], '需要保留的测试内容')
        code, second, _ = self.request('/api/conversations/new', 'POST', {})
        self.assertNotEqual(second['conversation_id'], identifier)
        self.assertEqual(second['messages'], [])

    def test_failed_archive_extraction_retries_once_and_preserves_original_chat(self):
        from agent_profiles import PROVIDERS
        clients = []

        class RecordingClient:
            def __init__(self, settings, timeout=180):
                self.settings, self.timeout, self.calls = settings, timeout, []
                self.fail = settings._profile_role == 'knowledge_archivist' and not any(
                    client.settings._profile_role == 'knowledge_archivist' for client in clients)
                clients.append(self)

            def stream_chat(self, messages, **kwargs):
                self.calls.append(messages)
                return {'role': 'assistant', 'content': '有限图使用已访问集合避免沿环重复遍历。'}

            def json_chat(self, system, user, **kwargs):
                data = json.loads(user)
                self.calls.append(data)
                if self.fail:
                    raise RuntimeError('合成故障：知识模型暂不可用')
                if data.get('source_review'):
                    return {'overview': data['overview'], 'units': data['units'],
                            'unsupported_unit_ids': []}
                sources = ([block['id'] for block in data['blocks']] if 'blocks' in data
                           else list(dict.fromkeys(source for note in data['notes'] for source in note['source_ids'])))
                unit = {'title': '有限图遍历', 'content': '有限图使用已访问集合避免沿环重复遍历。',
                        'tags': ['图'], 'source_ids': sources}
                if 'notes' in data:
                    unit['merged_from'] = [note['id'] for note in data['notes']]
                return {'overview': '有限图的已访问集合。', 'units': [unit]}

        def get(path):
            code, value, _ = self.request(path)
            self.assertEqual(code, 200, value)
            return value

        def post(path, body, expected=200):
            code, value, _ = self.request(path, 'POST', body)
            self.assertEqual(code, expected, value)
            return value

        def wait_for(path, key, expected):
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                value = get(path)[key]
                if value['status'] not in {'queued', 'running'}:
                    self.assertEqual(value['status'], expected, value)
                    return value
                time.sleep(.02)
            self.fail('归档重试在验收时限内未结束。')

        with patch.dict(PROVIDERS, {'deepseek': RecordingClient}):
            post('/api/agent-profiles/global', {'api_key': 'fixture-main', 'skills': []})
            post('/api/agent-profiles/knowledge_archivist', {'api_key': 'fixture-archive', 'skills': []})
            identifier = post('/api/conversations/new', {}, 201)['conversation_id']
            path = '/api/conversations/' + identifier
            run = post('/api/chat/start', {'message': '遍历有限图时为什么要标记访问过的顶点？',
                       'conversation_id': identifier, 'web_enabled': False}, 202)
            wait_for('/api/chat/runs/' + run['run_id'], 'run', 'completed')
            original = get(path)['messages']
            self.assertEqual([message['role'] for message in original], ['user', 'assistant'])

            archived = post(path + '/archive', {'extract_knowledge': True})
            self.assertIsNone(archived['extraction_error'])
            archived_at = archived['conversation']['archived_at']
            self.assertTrue(archived_at)
            job_id = archived['conversation']['knowledge_job']['id']
            job_path = '/api/knowledge/ingestions/' + job_id
            failed = wait_for(job_path, 'job', 'failed')
            self.assertIn('知识模型暂不可用', failed['error'])
            self.assertIn('归档仍保留', failed['stage'])
            self.assertEqual(get(path)['messages'], original)
            self.assertEqual(get(path)['archived_at'], archived_at)
            self.assertEqual(get('/api/knowledge')['total'], 0)
            self.assertEqual(get('/api/knowledge/archives')['archives'], [])
            self.assertEqual(list(self.settings.output_dir.rglob('*.md')), [])
            self.assertEqual([item['id'] for item in get('/api/conversations?scope=archived')['conversations']], [identifier])

            retry_path = '/api/knowledge-ingestions/' + job_id + '/retry'
            self.assertEqual(post(retry_path, {}, 202)['job']['id'], job_id)
            completed = wait_for(job_path, 'job', 'completed')
            self.assertIsNone(completed['error'])
            self.assertEqual(completed['result']['count'], 1)
            document = completed['result']['document']
            code, content, _ = self.request(document['url'])
            self.assertEqual(code, 200)
            self.assertIn('有限图使用已访问集合避免沿环重复遍历。', content.decode())
            self.assertEqual(list(self.settings.output_dir.rglob('*.md')), [self.settings.output_dir / document['name']])
            archives = get('/api/knowledge/archives')['archives']
            self.assertEqual([(item['id'], item['available']) for item in archives], [(job_id, True)])
            knowledge = get('/api/knowledge')
            self.assertEqual((knowledge['total'], len(knowledge['entries'])), (1, 1))
            entry = get('/api/knowledge/' + knowledge['entries'][0]['id'])['entry']
            self.assertEqual(len(entry['sources']), 1)
            self.assertEqual(entry['sources'][0]['conversation_id'], identifier)
            self.assertEqual(entry['sources'][0]['message_ids'], [message['id'] for message in original])
            self.assertEqual([message['content'] for message in entry['sources'][0]['evidence']],
                             [message['content'] for message in original])

            knowledge_clients = [client for client in clients if client.settings._profile_role == 'knowledge_archivist']
            self.assertEqual([len(client.calls) for client in knowledge_clients], [1, 3])
            successful_calls = knowledge_clients[1].calls
            self.assertIn('blocks', successful_calls[0])
            self.assertFalse(successful_calls[0].get('source_review'))
            self.assertIn('notes', successful_calls[1])
            self.assertTrue(successful_calls[2]['source_review'])
            self.assertEqual(successful_calls[2]['blocks'], successful_calls[0]['blocks'])
            self.assertEqual(post(retry_path, {}, 202)['job']['result'], completed['result'])
            self.assertEqual(len(get('/api/knowledge/ingestions')['jobs']), 1)
            self.assertEqual(get('/api/knowledge')['total'], 1)
            self.assertEqual(len(get('/api/knowledge/archives')['archives']), 1)
            self.assertEqual([len(client.calls) for client in knowledge_clients], [1, 3])
            self.assertEqual(get(path)['archived_at'], archived_at)
            self.assertEqual(get(path)['messages'], original)
            restored = post(path + '/unarchive', {})
            self.assertIsNone(restored['archived_at'])
            self.assertEqual(restored['messages'], original)

    def test_private_files_and_traversal_are_not_served(self):
        secret_path = self.settings.data_home / '.env'
        secret_path.write_text('PRIVATE_TEST_SENTINEL')
        for path in ['/.env', '/outputs/../.env', '/outputs/%2e%2e/.env', '/unknown-private-file']:
            with self.subTest(path=path):
                code, value, _ = self.request(path)
                self.assertIn(code, (403, 404))
                self.assertNotIn('PRIVATE_TEST_SENTINEL', str(value))

    def test_unconfigured_model_fails_without_creating_background_task(self):
        code, response, _ = self.request('/api/chat/start', 'POST', {'message': '你好', 'web_enabled': False})
        self.assertEqual(code, 400)
        self.assertIn('API Key', response['error'])
        self.assertEqual(self.runtime.tasks.list(), [])

    def test_legacy_note_image_and_zip_remain_available_at_original_urls(self):
        import io
        import zipfile
        from storage_layout import migrate_legacy
        from tests.test_content_lifecycle import PNG
        root = self.settings.output_dir
        content = '# 老笔记\n![图](BVfixture_assets/frame.png)'
        (root / 'BVfixture.md').write_text(content)
        (root / 'BVfixture_assets').mkdir()
        (root / 'BVfixture_assets/frame.png').write_bytes(PNG)
        migrate_legacy(self.settings)
        code, note, _ = self.request('/outputs/BVfixture.md')
        self.assertEqual((code, note.decode()), (200, content))
        code, image, _ = self.request('/outputs/BVfixture_assets/frame.png')
        self.assertEqual((code, image), (200, PNG))
        code, data, _ = self.request('/api/export?file=BVfixture.md')
        self.assertEqual(code, 200)
        with zipfile.ZipFile(io.BytesIO(data)) as package:
            self.assertEqual(package.read('video_summary/BVfixture_assets/frame.png'), PNG)

    def test_http_content_trash_and_restore_match_library(self):
        note = self.runtime.artifacts().save('本地测试文档', '完整内容')
        code, data, _ = self.request('/api/artifacts/delete', 'POST', {'artifact_ids': [note['id']]})
        self.assertEqual(code, 200)
        code, missing, _ = self.request('/api/artifacts/' + note['id'])
        self.assertEqual(code, 404)
        code, restored, _ = self.request('/api/artifacts/restore', 'POST', {'trash_ids': [data['deleted'][0]['trash_id']]})
        self.assertEqual(code, 200)
        self.assertEqual(restored['restored'][0]['id'], note['id'])
        code, data, _ = self.request('/api/artifacts/' + note['id'])
        self.assertEqual(code, 200)
        self.assertIn('完整内容', data['artifact']['content'])

    def test_migrated_report_can_be_trashed_and_restored_through_both_identifiers(self):
        from storage_layout import migrate_legacy

        name = 'legacy-note.MD'
        legacy_id = hashlib.sha256(name.encode()).hexdigest()[:16]
        legacy = self.settings.output_dir / name
        legacy.write_text('# 早期笔记\n\n原文保留。')
        asset = legacy.with_name('legacy-note_assets') / 'frame.png'
        asset.parent.mkdir()
        asset.write_bytes(b'legacy-frame')
        migrate_legacy(self.settings)
        code, data, _ = self.request('/api/artifacts/' + legacy_id)
        self.assertEqual(code, 200)
        note = data['artifact']
        self.assertNotEqual(note['id'], legacy_id)
        code, data, _ = self.request('/api/artifacts/delete', 'POST', {'artifact_ids': [note['id'], legacy_id]})
        self.assertEqual(code, 200)
        removed, = data['deleted']
        trash_id = removed['trash_id']
        self.assertEqual(self.request('/api/artifacts/restore', 'POST', {'trash_ids': [trash_id, {}]})[0], 400)
        for endpoint in ('/outputs/' + name, note['url'], '/outputs/legacy-note_assets/frame.png'):
            self.assertEqual(self.request(endpoint)[0], 404)
        self.assertEqual(self.request('/api/artifacts/restore', 'POST', {'trash_ids': [trash_id]})[0], 200)
        self.assertEqual(self.request('/outputs/' + name)[1], '# 早期笔记\n\n原文保留。'.encode())
        self.assertEqual(self.request('/outputs/legacy-note_assets/frame.png')[1], b'legacy-frame')
        self.assertEqual(self.request('/api/artifacts/' + legacy_id)[1]['artifact']['id'], note['id'])


if __name__ == '__main__':
    unittest.main()
