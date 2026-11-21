"""FR-06: complete material traversal, consolidation and citation boundaries."""
import json
import unittest
from unittest.mock import Mock

from runtime_control import CancellationToken, RunCancelled
from summary_engine import SourceBlock, SummaryEngine, SummaryError


class RecordingModel:
    def __init__(self):
        self.inputs = []

    def json_chat(self, system, payload, **kwargs):
        data = json.loads(payload)
        self.inputs.append(data)
        if data.get('source_review'):
            return {'overview': data['overview'], 'units': data['units'],
                    'unsupported_unit_ids': []}
        if 'blocks' in data:
            sources = list(dict.fromkeys(block['id'] for block in data['blocks']))
            unit = {'title': '知识', 'content': '材料中的知识。', 'source_ids': sources}
        else:
            unit = {'title': '整合知识', 'content': '整合后的知识。',
                'source_ids': list(data['sources']), 'merged_from': [note['id'] for note in data['notes']]}
        return {'overview': '概述', 'units': [unit]}


class SummaryContracts(unittest.TestCase):
    def summarize(self, model, blocks):
        return SummaryEngine(model).summarize(blocks, kind='fixture', title='验收材料')

    def test_short_material_also_receives_whole_material_consolidation(self):
        model = RecordingModel()
        bundle = self.summarize(model, [SourceBlock('p1', '内容', {'page': 1})]).as_dict()
        self.assertEqual(len(model.inputs), 3)
        self.assertTrue(model.inputs[1]['whole_material'])
        self.assertTrue(model.inputs[2]['source_review'])
        self.assertEqual(model.inputs[2]['blocks'], model.inputs[0]['blocks'])
        self.assertEqual(bundle['processing']['model_calls'], 3)
        self.assertEqual(bundle['sources']['p1']['page'], 1)
        self.assertEqual(bundle['units'][0]['id'], 'k1')

    def test_long_single_source_is_fully_read_including_tail(self):
        text = '前部' * 14000 + '最后的更正必须保留'
        model = RecordingModel()
        bundle = self.summarize(model, [SourceBlock('long', text)]).as_dict()
        extractions = [call for call in model.inputs if 'blocks' in call and not call.get('source_review')]
        reviews = [call for call in model.inputs if call.get('source_review')]
        blocks = [block for call in extractions for block in call['blocks']]
        self.assertEqual(''.join(block['text'] for block in blocks), text)
        self.assertEqual({block['id'] for block in blocks}, {'long'})
        self.assertEqual([call['blocks'] for call in reviews], [call['blocks'] for call in extractions])
        self.assertTrue(all(call['source_subset'] for call in reviews))
        self.assertEqual(len(extractions), bundle['processing']['chunks'])
        self.assertEqual(len(model.inputs), bundle['processing']['model_calls'])
        self.assertGreater(bundle['processing']['chunks'], 1)
        self.assertTrue(bundle['processing']['all_chunks_processed'])

    def test_unknown_source_fails_before_final_report(self):
        model = Mock()
        model.json_chat.return_value = {'units': [{'title': '编造', 'content': '未知来源', 'source_ids': ['not-read']}]}
        with self.assertRaisesRegex(SummaryError, '未提供的来源'):
            self.summarize(model, [SourceBlock('p1', '材料')])
        self.assertEqual(model.json_chat.call_count, 1)

    def test_merge_cannot_silently_drop_an_extracted_note(self):
        unit = lambda title: {'title': title, 'content': '正文', 'source_ids': ['p1']}
        model = Mock()
        model.json_chat.side_effect = [
            {'units': [unit('主题一'), unit('主题二')]},
            {'units': [{**unit('只留下一个'), 'merged_from': ['n1_1']}]},
        ]
        with self.assertRaisesRegex(SummaryError, '遗漏'):
            self.summarize(model, [SourceBlock('p1', '材料')])

    def test_merge_cannot_drop_an_input_notes_source_page(self):
        model = Mock()
        model.json_chat.side_effect = [
            {'units': [
                {'title': '方法概述', 'content': '方法概述。', 'source_ids': ['p3']},
                {'title': '方法细节', 'content': '方法细节。', 'source_ids': ['p5']},
            ]},
            {'units': [{'title': '完整方法', 'content': '方法概述与细节。',
                        'source_ids': ['p3'], 'merged_from': ['n1_1', 'n1_2']}]},
        ]
        with self.assertRaisesRegex(SummaryError, '遗漏.*来源'):
            self.summarize(model, [SourceBlock('p3', '方法概述', {'page': 3}),
                                   SourceBlock('p5', '方法细节', {'page': 5})])

    def test_split_can_distribute_one_notes_sources_across_output_units(self):
        model = Mock()
        responses = iter([
            {'units': [{'title': '完整方法', 'content': '方法概述与细节。',
                        'source_ids': ['p3', 'p5']}]},
            {'units': [
                {'title': '方法概述', 'content': '方法概述。',
                 'source_ids': ['p3'], 'merged_from': ['n1_1']},
                {'title': '方法细节', 'content': '方法细节。',
                 'source_ids': ['p5'], 'merged_from': ['n1_1']},
            ]},
        ])
        def respond(system, payload, **kwargs):
            data = json.loads(payload)
            if data.get('source_review'):
                return {'overview': data['overview'], 'units': data['units'],
                        'unsupported_unit_ids': []}
            return next(responses)
        model.json_chat.side_effect = respond
        bundle = self.summarize(model, [SourceBlock('p3', '方法概述', {'page': 3}),
                                        SourceBlock('p5', '方法细节', {'page': 5})]).as_dict()
        self.assertEqual([unit['source_ids'] for unit in bundle['units']], [['p3'], ['p5']])
        self.assertEqual({source['page'] for source in bundle['sources'].values()}, {3, 5})

    def test_other_notes_sources_cannot_mask_a_dropped_source(self):
        model = Mock()
        model.json_chat.side_effect = [
            {'units': [
                {'title': '完整方法', 'content': '方法概述与细节。', 'source_ids': ['p3', 'p5']},
                {'title': '实验', 'content': '另一项实验。', 'source_ids': ['p5']},
            ]},
            {'units': [
                {'title': '完整方法', 'content': '方法概述与细节。',
                 'source_ids': ['p3'], 'merged_from': ['n1_1']},
                {'title': '实验', 'content': '另一项实验。',
                 'source_ids': ['p5'], 'merged_from': ['n1_2']},
            ]},
        ]
        with self.assertRaisesRegex(SummaryError, '遗漏.*来源'):
            self.summarize(model, [SourceBlock('p3', '方法概述', {'page': 3}),
                                   SourceBlock('p5', '方法细节和另一项实验', {'page': 5})])

    def test_budget_exceeded_rejects_before_model_call(self):
        model = Mock()
        engine = SummaryEngine(model)
        engine.MAX_SOURCE_CHARS = 5
        with self.assertRaisesRegex(SummaryError, '上限'):
            engine.summarize([SourceBlock('p1', '超过五个字符的材料')], kind='fixture', title='超限')
        model.json_chat.assert_not_called()

    def test_cancelled_material_does_not_call_model(self):
        token = CancellationToken()
        token.cancel()
        model = Mock()
        with self.assertRaises(RunCancelled):
            SummaryEngine(model, cancellation=token).summarize([SourceBlock('p1', '内容')], kind='fixture', title='取消')
        model.json_chat.assert_not_called()

    def test_bundle_export_cannot_mutate_saved_sources(self):
        bundle = self.summarize(RecordingModel(), [SourceBlock('p1', '内容', {'page': 1})])
        export = bundle.as_dict()
        export['sources']['p1']['page'] = 99
        export['units'][0]['source_ids'].append('invented')
        self.assertEqual(bundle.as_dict()['sources']['p1']['page'], 1)
        self.assertEqual(bundle.as_dict()['units'][0]['source_ids'], ['p1'])


if __name__ == '__main__':
    unittest.main()
