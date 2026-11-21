"""Video source, frame scope and honest degradation contracts; no network."""
import copy
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import unquote
from unittest.mock import Mock, patch

from harness import DeepSeekClient
from model_client import ModelJSONError
from runtime_control import CancellationToken, RunCancelled
from video_summary import _CANCELLATION, _analyze_frames, _crop_direction_region, _parse_caption_file, _plan_summary, summarize_video

URL = 'https://www.bilibili.com/video/BV1it411g7CX/'


class VideoWorkflow(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.settings = SimpleNamespace(output_dir=self.root, prepare=lambda: None)
        self.material = {'info': {'id': 'fixture', 'title': '有限图遍历', 'webpage_url': URL, 'duration': 40},
            'transcript': [{'start': 0, 'end': 10, 'text': '已访问集合防止重复遍历。'},
                           {'start': 30, 'end': 40, 'text': '图必须有限。'}],
            'source': '平台字幕', 'language': 'zh'}
        self.plan = {'overview': '', 'overview_diagram': '', 'summary_processing': {},
            'source_index': {'s1': {'start_seconds': 0, 'end_seconds': 10}},
            'knowledge_units': [{'id': 'u1', 'title': '已访问集合', 'content': '用已访问集合记录顶点。',
                                 'source_ids': ['s1']}],
            'sections': [{'title': '避免重复遍历', 'summary': '用已访问集合记录顶点。',
                'diagram': '', 'diagram_type': 'none', 'frames': [5], 'start': 0, 'end': 10,
                'source_ids': ['s1'], 'unit_ids': ['u1']}]}

    def run_video(self, ffmpeg=True, video=True, extraction=True, descriptions=None, error=None,
                  include_frames=True, progress=None, output_path=None, url=URL):
        def extract(path, stamp, target):
            if extraction:
                target.write_bytes(b'\xff\xd8\xfffixture')
            return extraction
        with patch('video_summary._get_video_material', return_value=copy.deepcopy(self.material)), \
             patch('video_summary._plan_summary', return_value=copy.deepcopy(self.plan)), \
             patch('video_summary.resolve_tool', return_value='/fixture/ffmpeg' if ffmpeg else None), \
             patch('video_summary._download_video', return_value=self.root/'video.mp4' if video else None), \
             patch('video_summary._extract_frame', side_effect=extract), \
             patch('video_summary._analyze_frames', return_value=descriptions or {}, side_effect=error) as analyze:
            result = summarize_video(url, Mock(), self.settings, include_frames=include_frames,
                                     progress=progress, output_path=output_path)
        return result, analyze

    def test_report_and_evidence_retain_the_resolved_part(self):
        original = URL + '?p=02&spm_id_from=fixture'
        resolved = URL + '?p=2'
        self.material['info'].update(id='BV1it411g7CX_p2', webpage_url=resolved)
        result, _ = self.run_video(descriptions={5.0: '分集图示'}, url=original)
        self.assertIn('- 来源：' + resolved + '\n', result['markdown'])
        self.assertEqual(result['frames'], 1)
        path = Path(result['path'])
        evidence = json.loads((path.with_name(path.stem + '_assets') / 'source-evidence.json').read_text())
        self.assertEqual((evidence['url'], evidence['source_url'], evidence['video_id']),
                         (original, resolved, 'BV1it411g7CX_p2'))

    def test_degradation_is_visible_in_saved_note_and_source_evidence(self):
        for kwargs, status in [({'ffmpeg': False}, 'ffmpeg_unavailable'),
                               ({'video': False}, 'media_unavailable'),
                               ({'extraction': False}, 'extraction_failed'),
                               ({'error': RuntimeError('vision unavailable')}, 'analysis_failed')]:
            with self.subTest(status=status):
                result, _ = self.run_video(**kwargs)
                self.assertEqual(result['visual_review']['status'], status)
                self.assertEqual(result['frames'], 0)
                self.assertIn('降级为文字总结', result['markdown'])
                path = Path(result['path'])
                self.assertEqual(path.read_text(), result['markdown'])
                assets = path.with_name(path.stem + '_assets')
                self.assertEqual(list(assets.glob('frame-*.jpg')), [])
                evidence = json.loads((assets/'source-evidence.json').read_text())
                self.assertEqual(evidence['transcript'], self.material['transcript'])
                self.assertEqual(evidence['visual_review']['status'], status)

    def test_only_frames_with_relevant_descriptions_are_retained(self):
        result, _ = self.run_video(descriptions={5.0: '已访问集合避免重复处理顶点。'})
        self.assertEqual(result['frames'], 1)
        self.assertEqual(result['visual_review']['included'], 1)
        self.assertIn('![已访问集合', result['markdown'])
        self.assertIn('平台字幕', result['markdown'])
        self.assertNotIn('00:05', result['markdown'])
        result, _ = self.run_video()
        self.assertEqual(result['frames'], 0)
        self.assertIn('没有取得可用于笔记', result['markdown'])

    def test_disabled_and_unneeded_frames_never_call_vision(self):
        result, analyze = self.run_video(include_frames=False)
        analyze.assert_not_called()
        self.assertEqual(result['visual_review']['status'], 'not_requested')
        self.plan['sections'][0]['frames'] = []
        result, analyze = self.run_video()
        analyze.assert_not_called()
        self.assertEqual(result['visual_review']['status'], 'no_candidates')

    def test_caption_showing_markdown_cannot_add_a_second_image(self):
        from report_exports import _image_destinations
        result, _ = self.run_video(descriptions={5.0: '数组 a[0]，示例 ![图](missing.jpg)\n<img src="x.jpg">'})
        images = list(_image_destinations(result['markdown']))
        self.assertEqual(len(images), 1)
        self.assertTrue((Path(result['path']).parent / images[0]).is_file())

    def test_offline_zip_contains_video_frame_and_linked_evidence_for_custom_filename(self):
        from business_harnesses import VideoHarness
        from report_exports import _image_destinations, _markdown_destinations, build_report_archive

        path = self.root / '图遍历 #1 (复习).md'
        result, _ = self.run_video(output_path=path, descriptions={5.0: '已访问集合。'})
        harness = object.__new__(VideoHarness)
        harness.result = result
        with patch('business_harnesses.BusinessHarness.verify', return_value={'path': str(path)}):
            harness.verify()
        with build_report_archive(self.root, path.name) as package, zipfile.ZipFile(package.stream) as archive:
            asset = path.stem + '_assets/'
            frame, = path.with_name(path.stem + '_assets').glob('frame-*.jpg')
            self.assertEqual(set(archive.namelist()), {path.name, asset + frame.name, asset + 'source-evidence.json'})
            self.assertEqual(archive.read(asset + frame.name), frame.read_bytes())
            evidence = json.loads(archive.read(asset + 'source-evidence.json'))
            self.assertEqual(evidence['transcript'], self.material['transcript'])
            self.assertEqual(evidence['sections'][0]['unit_ids'], ['u1'])
            offline = self.root / 'offline'
            archive.extractall(offline)
        markdown = (offline / path.name).read_text()
        image, = _image_destinations(markdown)
        citation, = _markdown_destinations(markdown, images=False)
        self.assertEqual((offline / unquote(image)).read_bytes(), frame.read_bytes())
        self.assertEqual(json.loads((offline / unquote(citation)).read_text()), evidence)

    def test_cancel_before_save_does_not_publish_a_completed_note(self):
        token = CancellationToken()
        marker = _CANCELLATION.set(token)
        try:
            with self.assertRaises(RunCancelled):
                self.run_video(include_frames=False,
                    progress=lambda stage: token.cancel() if stage == '正在保存总结' else None)
        finally:
            _CANCELLATION.reset(marker)
        self.assertEqual(list(self.root.glob('*.md')), [])

    def test_frame_planning_rejects_unreferenced_gaps_and_unknown_units(self):
        first, second = 'video-00001', 'video-00002'
        self.material['transcript'].append({'start': 150, 'end': 160, 'text': '再考虑一次遍历。'})
        self.material['info']['duration'] = 160
        bundle = {'overview': '', 'units': [{'id': 'u1', 'content': '有限图上的已访问集合。',
                  'source_ids': [first, second]}], 'processing': {},
            'sources': {first: {'order': 0, 'start_seconds': 0, 'end_seconds': 40},
                        second: {'order': 1, 'start_seconds': 150, 'end_seconds': 160}}}
        client = Mock()
        section = {'title': '有限图遍历', 'summary': '有限图上的已访问集合。',
                   'unit_ids': ['u1'], 'source_ids': [first], 'frame_timestamps': [20, 5]}
        client.json_chat.return_value = {'sections': [section]}
        with patch('video_summary.SummaryEngine') as engine:
            engine.return_value.summarize.return_value.as_dict.return_value = bundle
            plan = _plan_summary(client, self.material, '')
            self.assertEqual(plan['sections'][0]['frames'], [5])
            self.assertEqual(plan['sections'][0]['source_ids'], [first, second])
            self.assertEqual(plan['source_index'][first]['cue_ranges'], [[0, 10], [30, 40]])
            self.assertEqual(plan['source_index'][second]['cue_ranges'], [[150, 160]])
            section['frame_timestamps'] = [80, 155]
            self.assertEqual(_plan_summary(client, self.material, '')['sections'][0]['frames'], [155])
            section['unit_ids'] = ['invented']
            with self.assertRaisesRegex(RuntimeError, '未知知识编号'):
                _plan_summary(client, self.material, '')

    def test_saved_report_keeps_reviewed_conditions_and_resolvable_unit_ids(self):
        client = Mock()
        reviewed = ['已访问集合可以防止重复遍历。', '只有图有限时，才能由此保证遍历终止。']
        reviewed_overview = '有限图的遍历与终止条件。'

        def respond(system, user, **kwargs):
            data = json.loads(user)
            if data.get('source_review'):
                return {'overview': reviewed_overview, 'unsupported_unit_ids': [],
                        'units': [{**unit, 'content': content} for unit, content in zip(data['units'], reviewed)]}
            if 'knowledge' in data:
                units = data['knowledge']['units']
                return {'include_overview': True, 'overview': '遍历总会终止。',
                        'sections': [{'title': '遍历与终止', 'summary': '即使图无限也一定终止。',
                                      'unit_ids': [unit['id'] for unit in reversed(units)],
                                      'source_ids': list(data['knowledge']['sources'])}]}
            source = data['blocks'][0]['id'] if 'blocks' in data else data['notes'][0]['source_ids'][0]
            units = [{'title': title, 'content': title, 'tags': [], 'source_ids': [source]}
                     for title in ('避免重复', '保证终止')]
            if 'notes' in data:
                for unit, note in zip(units, data['notes']):
                    unit['merged_from'] = [note['id']]
            return {'overview': '遍历。', 'units': units}

        client.json_chat.side_effect = respond
        with patch('video_summary._get_video_material', return_value=copy.deepcopy(self.material)):
            result = summarize_video(URL, client, self.settings, include_frames=False)
        path = Path(result['path'])
        markdown = path.read_text()
        self.assertIn(reviewed_overview, markdown)
        self.assertIn('\n\n'.join(reversed(reviewed)), markdown)
        self.assertNotIn('即使图无限', markdown)
        self.assertNotIn('遍历总会终止', markdown)
        evidence = json.loads((path.with_name(path.stem + '_assets') / 'source-evidence.json').read_text())
        units = {unit['id']: unit for unit in evidence['knowledge_units']}
        section = evidence['sections'][0]
        self.assertEqual([units[unit_id]['content'] for unit_id in section['unit_ids']], list(reversed(reviewed)))
        self.assertTrue(all(source in evidence['source_index'] for unit in units.values() for source in unit['source_ids']))
        self.assertEqual(result['summary_processing']['source_review_batches'], 1)

    def test_caption_dedup_keeps_separate_occurrences_and_extends_overlap(self):
        path = self.root / 'repeat.vtt'
        path.write_text('WEBVTT\n\n00:01.000 --> 00:03.000\n重复解释\n\n'
                        '00:02.000 --> 00:04.000\n重复解释\n\n'
                        '00:12.000 --> 00:14.000\n重复解释\n', encoding='utf-8')
        self.assertEqual(_parse_caption_file(path), [
            {'start': 1.0, 'end': 4.0, 'text': '重复解释'},
            {'start': 12.0, 'end': 14.0, 'text': '重复解释'},
        ])

    def test_webvtt_preserves_spoken_note_and_style_and_ignores_noncue_blocks(self):
        path = self.root / 'technical.vtt'
        path.write_text(
            'WEBVTT\nKind: captions\nLanguage: zh\n\n'
            'STYLE\n::cue { color: red }\n\n'
            'REGION\nid: slides\n\n'
            'NOTE 剪辑备注\n00:01.000 --> 00:02.000\n内部提示不属于字幕\n\n'
            '片段一\n00:03.000 --> 00:05.000 align:start\n'
            '<v Lecturer><c.accent>STYLE 是样式表。</c></v>\n'
            'NOTE 是注释。<00:04.500> 当 x &lt; y 时保留条件。\n\n'
            '这不是时码行 00:05.000 --> 00:06.000\n无对应时码的文字\n\n'
            '00:62.000 --> 00:63.000\n无效位置\n\n'
            '00:08.000 --> 00:09.000\n下一条有效字幕。',
            encoding='utf-8',
        )
        self.assertEqual(_parse_caption_file(path), [
            {'start': 3, 'end': 5, 'text': 'STYLE 是样式表。 NOTE 是注释。 当 x < y 时保留条件。'},
            {'start': 8, 'end': 9, 'text': '下一条有效字幕。'},
        ])

    def test_srt_preserves_math_angles_and_removes_formatting_tags(self):
        path = self.root / 'math.srt'
        path.write_bytes(
            '1\r\n00:00:03,000 --> 00:00:05,000 X1:22\r\n'
            '<font color="red"><i>当 a<b 且 b>c 时</i></font>保留条件。\r\n'
            'STYLE 是样式表。\r\nNOTE 是注释。'.encode('utf-8-sig')
        )
        self.assertEqual(_parse_caption_file(path), [
            {'start': 3, 'end': 5, 'text': '当 a<b 且 b>c 时保留条件。 STYLE 是样式表。 NOTE 是注释。'},
        ])

    def test_vision_rejects_malformed_descriptions_and_receives_cancellation(self):
        path = self.root/'frame.jpg'; path.write_bytes(b'\xff\xd8\xfffixture')
        frames = [{'timestamp': 5, 'path': str(path), 'section_title': '集合'}]
        client = Mock()
        client.json_chat.return_value = {'description': {'not': 'text'}}
        token = CancellationToken(); marker = _CANCELLATION.set(token)
        try:
            self.assertEqual(_analyze_frames(client, frames, self.material['transcript'], skill_instructions=''), {})
            self.assertIs(client.json_chat.call_args.kwargs['cancellation'], token)
            client.json_chat.assert_called_once()
        finally:
            _CANCELLATION.reset(marker)

    def test_cancel_after_observation_skips_relevance_review(self):
        path = self.root / 'frame.jpg'
        path.write_bytes(b'fixture')
        frame = {'timestamp': 5, 'path': str(path)}
        token = CancellationToken()
        client = Mock()

        def observe(*args, **kwargs):
            token.cancel()
            return {'description': '有限顶点图示'}

        client.json_chat.side_effect = observe
        marker = _CANCELLATION.set(token)
        try:
            with self.assertRaises(RunCancelled):
                _analyze_frames(client, [frame], [], skill_instructions='')
            client.json_chat.assert_called_once()
        finally:
            _CANCELLATION.reset(marker)

    def test_vision_keeps_chapter_context_out_of_observation_and_preserves_its_caption(self):
        import base64

        first, second, third = (self.root / f'{name}.jpg' for name in ('first', 'second', 'third'))
        for path in (first, second, third):
            path.write_bytes(path.stem.encode())
        frames = [{'timestamp': seconds, 'path': str(path), 'section_title': path.stem,
                   'draft': '章节草稿只用于判断是否相关。'}
                  for seconds, path in ((5, first), (20, second), (35, third))]
        client = Mock()
        client.json_chat.side_effect = [
            {'description': '集合图示'},
            {'description': '课程目录'},
            {'description': '有限顶点图示'},
            {'frames': [{'id': 2, 'relevant': True}, {'id': 1, 'relevant': False},
                        {'id': 0, 'relevant': True, 'description': '擅自补充了十个节点'},
                        {'id': 999, 'relevant': True}, {'id': True, 'relevant': True}]},
        ]
        captions = _analyze_frames(client, frames, self.material['transcript'], skill_instructions='')
        self.assertEqual(captions, {5.0: '集合图示', 35.0: '有限顶点图示'})
        vision_calls = client.json_chat.call_args_list[:-1]
        self.assertEqual(len(vision_calls), 3)
        for call, path in zip(vision_calls, (first, second, third)):
            prompt, content = call.args
            self.assertNotIn(path.stem, prompt)
            image, = content
            self.assertEqual(image['type'], 'image_url')
            self.assertEqual(image['image_url']['detail'], 'high')
            self.assertEqual(base64.b64decode(image['image_url']['url'].split(',', 1)[1]), path.read_bytes())
        review = json.loads(client.json_chat.call_args.args[1])['frames']
        self.assertEqual([item['知识主题'] for item in review], ['first', 'second', 'third'])
        self.assertIn('已访问集合', review[0]['附近字幕'])
        self.assertEqual(review[1]['章节草稿'], frames[1]['draft'])

    def test_one_unparseable_observation_preserves_the_other_frames(self):
        path = self.root / 'frame.jpg'
        path.write_bytes(b'fixture')
        frames = [{'timestamp': stamp, 'path': str(path)} for stamp in (5, 20, 35)]
        client = DeepSeekClient(self.settings)
        logger = Mock()
        malformed = {'content': r'{"description":"\sigma(x)"}'}
        with patch.object(client, 'chat', side_effect=[
            {'content': '{"description":"集合图示"}'},
            malformed,
            {'content': '{"description":"有限顶点图示"}'},
            {'content': '{"frames":[{"id":0,"relevant":true},{"id":2,"relevant":true}]}'},
        ]) as chat:
            result = _analyze_frames(client, frames, [], skill_instructions='', logger=logger)
            self.assertEqual(result, {5.0: '集合图示', 35.0: '有限顶点图示'})
            self.assertEqual(chat.call_count, 4)
            review_input = json.loads(chat.call_args.args[0][1]['content'])
            self.assertEqual([item['id'] for item in review_input['frames']], [0, 2])
        logger.warning.assert_called_once()
        self.assertEqual(logger.warning.call_args.args[1], 2)
        with patch.object(client, 'chat', return_value=malformed) as chat:
            with self.assertRaises(ModelJSONError):
                _analyze_frames(client, frames[:1], [], skill_instructions='')
            chat.assert_called_once()

    def test_direction_recheck_is_bounded_and_falls_back_to_the_same_frame(self):
        import base64

        frames = []
        for stamp in (0, 1, 5, 20, 35):
            path = self.root / f'frame-{stamp}.jpg'
            path.write_bytes(f'original-{stamp}'.encode())
            frames.append({'timestamp': stamp, 'path': str(path)})
        region = [600, 100, 850, 700]
        diagram = {'direction_region': region, 'node_labels': ['1', '2']}
        client, logger = Mock(), Mock()
        client.json_chat.side_effect = [
            {'description': '图中公式为 w₁a₁+w₂a₂。', 'direction_region': region},
            {'description': '原图可见 1→2。', **diagram},
            {'description': '候选图一包含节点 1 和 2。', **diagram},
            {'arrows': [[None, '1'], ['1', '2'], ['invented', '1'], ['1', {'wrong': 'type'}]]},
            {'description': '候选图二', **diagram},
            ModelJSONError('局部复核格式错误'),
            {'description': '候选图三', **diagram},
            {'frames': [{'id': index, 'relevant': True} for index in range(len(frames))]},
        ]
        with patch('video_summary._crop_direction_region', side_effect=[b'crop-first', b'crop-second']) as crop:
            captions = _analyze_frames(client, frames, [], skill_instructions='', logger=logger)
        self.assertEqual(captions, {0.0: '图中公式为 w₁a₁+w₂a₂。', 1.0: '原图可见 1→2。',
                                    5.0: '候选图一包含节点 1 和 2。 局部放大可辨箭头：入口→1、1→2。',
                                    20.0: '候选图二', 35.0: '候选图三'})
        self.assertEqual([call.args for call in crop.call_args_list],
                         [(Path(frame['path']), region) for frame in frames[2:4]])
        images = [call.args[1][0]['image_url']['url'] for call in client.json_chat.call_args_list[:-1]]
        self.assertEqual([base64.b64decode(image.split(',', 1)[1]) for image in images],
                         [b'original-0', b'original-1', b'original-5', b'crop-first', b'original-20', b'crop-second', b'original-35'])
        self.assertEqual(Path(frames[2]['path']).read_bytes(), b'original-5')
        logger.warning.assert_called_once()

    def test_direction_crop_validates_bounds_includes_margin_and_uses_no_output_path(self):
        path = self.root / 'frame.jpg'
        with patch('video_summary.resolve_tool', return_value='/fixture/ffmpeg'), \
             patch('video_summary._run_process', return_value=SimpleNamespace(returncode=0, stdout=b'crop')) as run:
            for invalid in (None, [False, 0, 400, 400], [-1, 0, 400, 400], [0, 0, 1000, 1000], [0, 0, 1, 500]):
                self.assertIsNone(_crop_direction_region(path, invalid))
            run.assert_not_called()
            self.assertEqual(_crop_direction_region(path, [600, 30, 980, 900]), b'crop')
            command, = run.call_args.args
            self.assertEqual(command[command.index('-i') + 1], str(path))
            self.assertIn('crop=iw*440/1000:ih*940/1000:iw*560/1000:ih*0/1000', command[command.index('-vf') + 1])
            self.assertEqual(command[-1], 'pipe:1')

    def test_visual_disagreement_quotes_only_the_actual_section_draft(self):
        path = self.root / 'frame.jpg'
        path.write_bytes(b'fixture')
        frame = {'timestamp': 5, 'path': str(path), 'draft': '用 pass(t) 表示测试路径。'}
        client = Mock()
        observation = {'description': '投影片给出路径定义 path(t)。'}
        item = {'id': 0, 'relevant': True,
                'draft_quote': 'pass(t)', 'visible_quote': 'path(t)'}

        def analyze():
            client.json_chat.side_effect = [observation, {'frames': [item]}]
            return _analyze_frames(client, [frame], [], skill_instructions='')[5]

        self.assertIn('文字草稿写作「pass(t)」，模型从本图读到「path(t)」', analyze())
        item['draft_quote'] = 'invented(t)'
        self.assertEqual(analyze(), observation['description'])
        item.update(draft_quote='pass(t)', visible_quote='invented(t)')
        self.assertEqual(analyze(), observation['description'])


if __name__ == '__main__':
    unittest.main()
