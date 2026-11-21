"""Source provenance and topic coverage for paper presentation plans."""
import copy
import json
import unittest
import os
from pathlib import Path
import tempfile
import zipfile
from unittest.mock import create_autospec, patch

from paper_presentation import REQUIRED_TOPICS, TOPICS, plan_presentation, review_presentation_numbers, validate_slide_plan
from harness import DeepSeekClient
from runtime_control import CancellationToken, RunCancelled
from summary_engine import SummaryError


def _write_presentation_fixture(path, *, slide=True, editable=True, media=None):
    presentation_ns = 'http://schemas.openxmlformats.org/presentationml/2006/main'
    drawing_ns = 'http://schemas.openxmlformats.org/drawingml/2006/main'
    relation_ns = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
    package_ns = 'http://schemas.openxmlformats.org/package/2006/relationships'
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('ppt/presentation.xml', f'<p:presentation xmlns:p="{presentation_ns}" '
            f'xmlns:r="{relation_ns}"><p:sldIdLst><p:sldId id="256" r:id="test"/>'
            '</p:sldIdLst></p:presentation>')
        archive.writestr('ppt/_rels/presentation.xml.rels', f'<Relationships xmlns="{package_ns}">'
            f'<Relationship Id="test" Type="{relation_ns}/slide" Target="slides/slide1.xml"/>'
            '</Relationships>')
        if slide:
            text = '<p:sp><p:txBody><a:p><a:r><a:t>Slide text</a:t></a:r></a:p></p:txBody></p:sp>' if editable else ''
            archive.writestr('ppt/slides/slide1.xml', f'<p:sld xmlns:p="{presentation_ns}" '
                f'xmlns:a="{drawing_ns}"><p:cSld><p:spTree>{text}</p:spTree></p:cSld></p:sld>')
        if media is not None:
            archive.writestr('ppt/media/orphan.png', media)


class PaperPresentationPlanningTests(unittest.TestCase):
    def setUp(self):
        self.evidence = {
            "material": {"title": "Finite graph traversal", "scope": "PDF text only"},
            "summary": {"units": [{"id": "k1", "source_ids": ["p1"],
                                     "content": "A visited set avoids duplicate traversal."}],
                        "sources": {"p1": {"page": 1}}},
            "source_blocks": [{"id": "p1", "text": "Synthetic paper title and author"}],
        }
        self.plan = {"slides": [{"title": topic, "topics": [topic],
                                 "unit_ids": ["k1"], "bullets": ["Supported fact"],
                                 "missing_topics": [], "diagram": None, "pages": []}
                                for topic in TOPICS if topic in REQUIRED_TOPICS]}
        overview = next(slide for slide in self.plan['slides'] if slide['topics'] == ['overview'])
        overview.update(bullets=[], diagram={'nodes': ['Input', 'Output'], 'edges': [[0, 1]]})

    def test_plan_preserves_required_order_and_derives_source_ids(self):
        result = validate_slide_plan(self.plan, self.evidence, 5)
        self.assertEqual(len(result["slides"]), 10)
        self.assertEqual(result["slides"][0]["source_ids"], ["p1"])
        self.assertEqual(result["scope"], "PDF text only")
        compressed = copy.deepcopy(self.plan)
        compressed["slides"][0]["topics"] += compressed["slides"][1]["topics"]
        compressed["slides"].pop(1)
        self.assertEqual(len(validate_slide_plan(compressed, self.evidence, 5)["slides"]), 9)

    def test_omitted_required_topic_and_wrong_order_are_rejected(self):
        for action in (lambda slides: slides.pop(2), lambda slides: slides.reverse()):
            plan = copy.deepcopy(self.plan)
            action(plan["slides"])
            with self.assertRaises(SummaryError):
                validate_slide_plan(plan, self.evidence, 20)

    def test_short_deck_reserves_pages_for_figures_and_diagrams(self):
        plan = copy.deepcopy(self.plan)
        overview = next(slide for slide in plan['slides'] if slide['topics'] == ['overview'])
        overview['diagram'] = {'nodes': ['Input', 'Output'], 'edges': [[0, 1]]}
        overview['bullets'] = ['Supported facts']
        overview['pages'] = [{'page': 1, 'label': 'Figure 1'}]
        with self.assertRaisesRegex(SummaryError, '需要 12 页，上限 10 页'):
            validate_slide_plan(plan, self.evidence, 5)
        plan['slides'][1]['bullets'] = ['x' * 91]
        with self.assertRaisesRegex(SummaryError, '短汇报'):
            validate_slide_plan(plan, self.evidence, 5)
        self.assertEqual(len(validate_slide_plan(plan, self.evidence, 15)['slides']), 10)

    def test_overview_cannot_be_a_text_only_heading_and_accepts_disclosed_absence(self):
        page = next(slide for slide in self.plan['slides'] if slide['topics'] == ['overview'])
        page.update(diagram=None, bullets=['Supported facts'])
        with self.assertRaisesRegex(SummaryError, '方法总览缺少'):
            validate_slide_plan(self.plan, self.evidence, 20)
        page.update(missing_topics=['overview'], bullets=['The abstract does not describe the method architecture.'])
        result = validate_slide_plan(self.plan, self.evidence, 20)
        saved = next(slide for slide in result['slides'] if slide['topics'] == ['overview'])
        self.assertEqual(saved['missing_topics'], ['overview'])

    def test_short_deck_reserves_one_page_for_either_original_overview_or_fallback(self):
        page = next(slide for slide in self.plan['slides'] if slide['topics'] == ['overview'])
        candidate = {'page': 1, 'label': 'Figure 1', 'purpose': 'method_overview'}
        page['pages'] = [candidate]
        result = validate_slide_plan(self.plan, self.evidence, 5)
        saved = next(slide for slide in result['slides'] if slide['topics'] == ['overview'])
        self.assertEqual(saved['pages'], [candidate])
        self.plan['slides'][-1]['pages'] = [candidate]
        with self.assertRaisesRegex(SummaryError, '仅首张方法总览'):
            validate_slide_plan(self.plan, self.evidence, 20)

    def test_unknown_units_and_figures_outside_source_pages_are_rejected(self):
        plan = copy.deepcopy(self.plan)
        plan["slides"][0]["unit_ids"] = ["invented"]
        with self.assertRaisesRegex(SummaryError, "未知知识"):
            validate_slide_plan(plan, self.evidence, 20)
        plan = copy.deepcopy(self.plan)
        plan["slides"][0]["pages"] = [{"page": 2, "label": "Figure 1"}]
        with self.assertRaisesRegex(SummaryError, "来源范围"):
            validate_slide_plan(plan, self.evidence, 20)
        plan["slides"][0]["pages"][0]["page"] = 1
        self.assertEqual(validate_slide_plan(plan, self.evidence, 20)["slides"][0]["pages"][0]["page"], 1)

    def test_caption_repairs_figure_page_without_relabeling_statement_provenance(self):
        self.evidence['source_blocks'].extend([
            {'id': 'p6', 'locator': {'page': 6}, 'text': 'Figure 4 compares the results.'},
            {'id': 'p7', 'locator': {'page': 7}, 'text': '    Fig. 4: Normalized action sequences.'},
        ])
        slide = next(slide for slide in self.plan['slides'] if slide['topics'] == ['main_results'])
        slide['pages'] = [{'page': 1, 'label': 'Fig. 4(a)', 'caption_source_id': 'model-invented'}]
        client = create_autospec(DeepSeekClient, instance=True)
        client.json_chat.return_value = self.plan
        result = plan_presentation(client, self.evidence)
        payload = json.loads(client.json_chat.call_args.args[1])
        self.assertEqual(payload['figure_captions'], [{'page': 7, 'source_id': 'p7',
            'label': 'Fig. 4', 'excerpt': 'Normalized action sequences.'}])
        rendered = next(page for page in result['slides'] if page['topics'] == ['main_results'])
        self.assertEqual(rendered['source_ids'], ['p1'])
        self.assertEqual(rendered['pages'], [{'page': 7, 'label': 'Fig. 4(a)',
            'requested_page': 1, 'caption_source_id': 'p7'}])

    def test_ambiguous_captions_do_not_override_the_cited_page(self):
        self.evidence['source_blocks'].extend([
            {'id': 'p2', 'locator': {'page': 2}, 'text': 'TABLE II: Development results.'},
            {'id': 'p3', 'locator': {'page': 3}, 'text': 'Table II: Test results.'},
        ])
        self.plan['slides'][0]['pages'] = [{'page': 1, 'label': 'Table II'}]
        result = validate_slide_plan(self.plan, self.evidence, 20)
        self.assertEqual(result['slides'][0]['pages'], [{'page': 1, 'label': 'Table II'}])
        self.plan['slides'][0]['pages'][0]['page'] = 2
        with self.assertRaisesRegex(SummaryError, '无唯一图注'):
            validate_slide_plan(self.plan, self.evidence, 20)

    def test_missing_required_evidence_needs_visible_disclosure(self):
        plan = copy.deepcopy(self.plan)
        slide = plan["slides"][2]
        slide["unit_ids"] = []
        with self.assertRaisesRegex(SummaryError, "没有知识依据"):
            validate_slide_plan(plan, self.evidence, 20)
        slide["missing_topics"] = slide["topics"]
        slide["bullets"] = ["The provided material does not define the research problem."]
        result = validate_slide_plan(plan, self.evidence, 20)
        self.assertEqual(result["slides"][2]["source_ids"], [])

    def test_request_passes_time_language_and_evidence_and_honors_cancellation(self):
        client = create_autospec(DeepSeekClient, instance=True)
        client.json_chat.return_value = self.plan
        result = plan_presentation(client, self.evidence, 10, "en")
        payload = json.loads(client.json_chat.call_args.args[1])
        self.assertEqual((payload["minutes"], payload["language"]), (10, "en"))
        self.assertEqual(payload["knowledge"], self.evidence["summary"])
        self.assertEqual((result["minutes"], result["language"], payload["max_slides"]), (10, "en", 20))
        token = CancellationToken()
        token.cancel()
        client.reset_mock()
        with self.assertRaises(RunCancelled):
            plan_presentation(client, self.evidence, cancellation=token)
        client.json_chat.assert_not_called()

    def test_source_kind_and_scope_come_from_the_reader_not_from_the_plan(self):
        self.evidence['material'].update(kind='abstract', scope='Only the public abstract was read.')
        client = create_autospec(DeepSeekClient, instance=True)
        client.json_chat.return_value = {**self.plan, 'source_kind': 'pdf', 'scope': 'Full text read.'}
        result = plan_presentation(client, self.evidence)
        self.assertEqual(result['source_kind'], 'abstract')
        self.assertEqual(result['scope'], 'Only the public abstract was read.')
        self.assertEqual(json.loads(client.json_chat.call_args.args[1])['source_kind'], 'abstract')

    def test_bibliography_comes_from_material_and_long_cover_title_is_allowed(self):
        bibliography = {'venue': {'text': 'Fixture 2019', 'source': {'kind': 'pdf_first_page', 'page': 1}}}
        self.evidence['material']['bibliography'] = bibliography
        self.plan['slides'][0]['title'] = '方法与证据' * 30
        client = create_autospec(DeepSeekClient, instance=True)
        client.json_chat.return_value = {**self.plan, 'bibliography': {'venue': {'text': 'Invented'}}}
        plan = plan_presentation(client, self.evidence)
        self.assertEqual(plan['bibliography'], bibliography)
        self.assertEqual(json.loads(client.json_chat.call_args.args[1])['bibliography'], bibliography)

    def test_invalid_plan_gets_one_bounded_revision_with_error_and_same_sources(self):
        invalid = copy.deepcopy(self.plan)
        invalid["slides"].reverse()
        client = create_autospec(DeepSeekClient, instance=True)
        client.json_chat.side_effect = [invalid, self.plan]
        result = plan_presentation(client, self.evidence)
        self.assertEqual(result["slides"][0]["topics"], ["title"])
        self.assertEqual(client.json_chat.call_count, 2)

        correction = json.loads(client.json_chat.call_args.args[1])
        self.assertIn("顺序", correction["revision"]["validation_error"])
        self.assertEqual(correction["knowledge"], self.evidence["summary"])
        client.json_chat.side_effect = [invalid, invalid]
        client.reset_mock()
        with self.assertRaises(SummaryError):
            plan_presentation(client, self.evidence)
        self.assertEqual(client.json_chat.call_count, 2)


class PaperBibliographyTests(unittest.TestCase):
    def test_pdf_cover_extracts_explicit_proceedings_from_first_page_stream(self):
        import io
        from pypdf import PdfReader, PdfWriter
        from pypdf.generic import DecodedStreamObject, NameObject
        from paper_bibliography import cover_bibliography
        from tests.test_paper_workflow import sample_pdf

        writer = PdfWriter()
        writer.append(PdfReader(io.BytesIO(sample_pdf())))
        page = writer.pages[0]
        original = page.get_contents().get_data()
        contents = DecodedStreamObject()
        contents.set_data(b'BT /F1 9 Tf 20 30 Td (Proceedings of Fixture 2019, pages 1-8) Tj ET\n' + original)
        page[NameObject('/Contents')] = writer._add_object(contents)
        writer.add_metadata({'/Author': 'Paper Author; Coauthor', '/Subject': 'Do not infer from subject'})
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'record.pdf'
            writer.write(path)
            fields = cover_bibliography({'kind': 'pdf'}, path, CancellationToken())
        self.assertEqual(fields['authors']['text'], 'Paper Author; Coauthor')
        self.assertEqual(fields['venue'], {'text': 'Fixture 2019', 'source': {'kind': 'pdf_first_page',
                           'page': 1, 'quote': 'Proceedings of Fixture 2019, pages 1-8'}})

    def test_preprint_is_separate_and_absent_metadata_is_not_guessed(self):
        import io
        from pypdf import PdfReader, PdfWriter
        from paper_bibliography import cover_bibliography
        from tests.test_paper_workflow import sample_pdf

        writer = PdfWriter()
        writer.append(PdfReader(io.BytesIO(sample_pdf())))
        writer.add_metadata({'/arXivID': 'https://arxiv.org/abs/2506.18824v2', '/Subject': 'Conference 2099'})
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'preprint.pdf'
            writer.write(path)
            fields = cover_bibliography({'kind': 'pdf'}, path, CancellationToken())
        self.assertEqual(set(fields), {'preprint'})
        self.assertEqual(fields['preprint']['text'], 'arXiv:2506.18824v2')

    def test_publisher_metadata_retains_repeat_authors_as_separate_names(self):
        from paper_bibliography import publisher_bibliography
        from paper_summary import _PaperPage

        parser = _PaperPage()
        parser.feed('''<meta name="citation_author" content="First Author">
            <meta name="citation_author" content="Second Author">
            <meta name="citation_conference_title" content="Fixture Proceedings">''')
        url = 'https://example.org/paper'
        fields = publisher_bibliography(parser.citations, url)
        self.assertEqual(fields['authors']['text'], 'First Author; Second Author')
        self.assertEqual(fields['venue']['source']['url'], url)
        self.assertEqual(fields['venue']['text'], 'Fixture Proceedings')

class PaperPresentationDeliveryTests(unittest.TestCase):
    def test_numerical_review_can_fix_a_number_bound_to_the_wrong_comparison(self):
        original = 'The single model scored 93.2 F1 on test.'
        corrected = 'On test, the single model scored 91.8 F1; the ensemble scored 93.2.'
        source = 'Single + TriviaQA test F1: 91.8. Ensemble + TriviaQA test F1: 93.2.'
        evidence = {'summary': {'units': [{'id': 'k8', 'content': source}]}}
        plan = {'language': 'en', 'minutes': 15, 'slides': [{'title': 'Test results',
                'topics': ['main_results'], 'unit_ids': ['k8'], 'bullets': ['A qualitative note.', original]}]}
        client = create_autospec(DeepSeekClient, instance=True)
        client.json_chat.return_value = {'checked': ['1:2'], 'changes': [
            {'id': '1:2', 'text': corrected, 'unit_id': 'k8', 'quote': source}]}
        review_presentation_numbers(client, plan, evidence, CancellationToken(), lambda _: None)
        request = json.loads(client.json_chat.call_args.args[1])
        self.assertEqual([(item['id'], item['text']) for item in request['claims']], [('1:2', original)])
        self.assertEqual(plan['slides'][0]['bullets'], ['A qualitative note.', corrected])
        self.assertEqual(plan['number_review']['changes'][0]['before'], original)

    def test_numerical_review_requires_complete_coverage_and_real_evidence(self):
        evidence = {'summary': {'units': [{'id': 'k1', 'content': 'The test value is 18.'}]}}
        draft = {'language': 'en', 'minutes': 5, 'slides': [{'title': 'Result', 'topics': ['main_results'],
                 'unit_ids': ['k1'], 'bullets': ['The test value is 8.']}]}
        client = create_autospec(DeepSeekClient, instance=True)
        bad_changes = [{'checked': [], 'changes': []}, {'checked': ['1:1'], 'changes': [
            {'id': '1:1', 'text': 'The test value is 19.', 'unit_id': 'k1', 'quote': 'value is 19'}]}]
        for response in bad_changes:
            with self.subTest(response=response):
                plan = copy.deepcopy(draft)
                client.json_chat.return_value = response
                with self.assertRaisesRegex(SummaryError, '复核|修订'):
                    review_presentation_numbers(client, plan, evidence, CancellationToken(), lambda _: None)
                self.assertEqual(plan, draft)

    def test_overview_uses_original_only_when_its_role_is_verified(self):
        from paper_presentation_rendering import _original_figures, _select_method_overview
        from tests.test_content_lifecycle import PNG

        for matches, fallback, expected in ((True, True, 'original'),
                                            (False, True, 'schematic'),
                                            (False, False, 'missing')):
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as directory:
                assets = Path(directory)
                slide = {'topics': ['overview'], 'diagram': {'nodes': ['Read', 'Check']} if fallback else None,
                         'pages': [{'page': 3, 'label': 'Figure 1', 'purpose': 'method_overview'}]}
                plan = {'slides': [slide], 'language': 'en'}
                client = create_autospec(DeepSeekClient, instance=True)
                client.json_chat.side_effect = [
                    {'supported': True, 'bbox': [.15, .15, .8, .8]},
                    {'complete': True, 'caption': 'A chart appears.', 'method_overview': matches},
                ]

                def rasterize(command, cancellation):
                    Path(command[-1]).with_suffix('.png').write_bytes(PNG)

                with patch('paper_presentation_rendering._run', side_effect=rasterize):
                    reviews = _original_figures(plan, assets / 'fixture.pdf', assets,
                        {'pdftoppm': 'test-rasterizer'}, client, CancellationToken(), lambda _: None)
                _select_method_overview(plan)
                self.assertEqual(slide['overview_display'], expected)
                self.assertEqual(reviews[0]['status'], 'observed' if matches else 'overview_not_confirmed')
                if matches:
                    self.assertEqual(slide['images'][0]['purpose'], 'method_overview')
                else:
                    self.assertNotIn('images', slide)

    def test_verifier_rejects_missing_slide_picture_only_slide_and_orphan_media(self):
        from paper_presentation_rendering import PresentationError, verify_presentation

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'presentation.pptx'
            for options in ({'slide': False}, {'editable': False}):
                with self.subTest(options=options):
                    _write_presentation_fixture(path, **options)
                    with self.assertRaisesRegex(PresentationError, '结构无法验证'):
                        verify_presentation(path)
            image = Path(directory) / 'crop.png'
            image.write_bytes(b'unreferenced crop')
            _write_presentation_fixture(path, media=image.read_bytes())
            self.assertEqual(verify_presentation(path), 1)
            with self.assertRaisesRegex(PresentationError, '原图未完整嵌入'):
                verify_presentation(path, [image])

    def test_notes_survive_presentation_failure_and_cancellation(self):
        from business_harnesses import PaperHarness
        from capabilities import CapabilityContext
        from harness import Settings
        from paper_presentation_rendering import PresentationError
        from tests.test_paper_evidence import EvidenceModel

        for error in (PresentationError('渲染器退出'), RunCancelled()):
            with self.subTest(error=type(error).__name__), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                with patch.dict(os.environ, {'MINIMAL_AGENT_HOME': str(root / 'local'),
                                            'MINIMAL_AGENT_OUTPUT': str(root / 'reports')}):
                    settings = Settings()
                settings.prepare()
                harness = PaperHarness({'paper_text': 'A visited set prevents repeated traversal. ' * 10,
                                        'include_presentation': True},
                                       CapabilityContext(settings, client=EvidenceModel()))
                with patch('paper_presentation_rendering.create_presentation', side_effect=error) as render:
                    with self.assertRaises(type(error)) as caught:
                        harness.produce()
                render.assert_called_once()
                if isinstance(error, PresentationError):
                    self.assertIn('论文总结已保存', str(caught.exception))
                    self.assertFalse(caught.exception.retryable)
                report, = settings.output_dir.rglob('*.md')
                self.assertIn('A visited set', report.read_text())
                self.assertNotIn('presentation.pptx', report.read_text())

    def test_missing_presentation_runtime_preserves_note_and_evidence(self):
        from business_harnesses import PaperHarness
        from capabilities import CapabilityContext
        from harness import Settings
        from paper_presentation_rendering import PresentationError
        from paper_summary import PaperUploads
        from tests.test_paper_evidence import EvidenceModel
        from tests.test_paper_workflow import sample_pdf

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.dict(os.environ, {'MINIMAL_AGENT_HOME': str(root / 'local'),
                                        'MINIMAL_AGENT_OUTPUT': str(root / 'reports')}):
                settings = Settings()
            settings.prepare()
            stages = []
            original = sample_pdf()
            upload = PaperUploads(settings.data_home).save(original, 'source.pdf')
            harness = PaperHarness({'upload_id': upload['upload_id'], 'include_presentation': True},
                                   CapabilityContext(settings, client=EvidenceModel(), progress=stages.append))

            with self.assertRaisesRegex(PresentationError, '论文总结已保存，PPT 生成未完成.*渲染环境尚未配置'):
                harness.produce()

            report, = settings.output_dir.rglob('*.md')
            self.assertIn('A visited set', report.read_text())
            self.assertIn('source-evidence.json', report.read_text())
            self.assertNotIn('presentation.pptx', report.read_text())
            assets = report.with_name(report.stem + '_assets')
            self.assertTrue((assets / 'source-evidence.json').is_file())
            self.assertEqual((assets / 'original.pdf').read_bytes(), original)
            self.assertFalse((assets / 'presentation.pptx').exists())
            self.assertEqual(stages[-1], 'PPT：检查本地渲染环境')

    def test_original_figure_must_be_confirmed_again_after_cropping(self):
        from paper_presentation_rendering import _original_figures
        from tests.test_content_lifecycle import PNG

        for complete in (False, True):
            with self.subTest(complete=complete), tempfile.TemporaryDirectory() as directory:
                assets = Path(directory)
                slide = {'pages': [{'page': 9, 'label': 'Table II'}]}
                plan = {'slides': [slide], 'language': 'en'}
                client = create_autospec(DeepSeekClient, instance=True)
                client.json_chat.side_effect = [
                    {'supported': True, 'bbox': [.05, .04, .92, .20]},
                    {'complete': complete, 'caption': 'The results table has three agent groups.'},
                ]

                def rasterize(command, cancellation):
                    Path(command[-1]).with_suffix('.png').write_bytes(PNG)

                with patch('paper_presentation_rendering._run', side_effect=rasterize):
                    reviews = _original_figures(plan, assets / 'fixture.pdf', assets,
                        {'pdftoppm': 'test-rasterizer'}, client, CancellationToken(), lambda _: None)
                self.assertEqual(client.json_chat.call_count, 2)
                self.assertEqual(reviews[0]['status'], 'observed' if complete else 'crop_not_confirmed')
                if complete:
                    self.assertGreater(reviews[0]['bbox'][3], .20)
                    self.assertEqual(Path(slide['images'][0]['path']).read_bytes(), PNG)
                    self.assertIn('three agent groups', slide['images'][0]['caption'])
                else:
                    self.assertNotIn('images', slide)
                    self.assertEqual(list(assets.iterdir()), [])
                    self.assertIn('could not be verified as complete', slide['figure_limitations'][0])

    def test_bad_rasterizer_keeps_text_only_slide_and_does_not_mask_cancellation(self):
        from paper_presentation_rendering import _original_figures

        with tempfile.TemporaryDirectory() as directory:
            assets = Path(directory)
            slide = {'pages': [{'page': 9, 'label': 'Table II'}]}
            plan = {'slides': [slide], 'language': 'zh'}
            client = create_autospec(DeepSeekClient, instance=True)
            args = (plan, assets / 'fixture.pdf', assets, {'pdftoppm': 'missing'}, client,
                    CancellationToken(), lambda _: None)
            with patch('paper_presentation_rendering._run', side_effect=FileNotFoundError()):
                reviews = _original_figures(*args)
            self.assertEqual(reviews[0]['status'], 'raster_failed')
            self.assertNotIn('images', slide)
            self.assertIn('提取原文图表失败', slide['figure_limitations'][0])
            client.json_chat.assert_not_called()
            with patch('paper_presentation_rendering._run', side_effect=RunCancelled()):
                with self.assertRaises(RunCancelled):
                    _original_figures(*args)

    def test_selected_ppt_is_saved_linked_exported_and_required_for_finish(self):
        from business_harnesses import PaperHarness
        from capabilities import CapabilityContext, validate_parameters
        from harness import Settings
        from report_exports import build_report_archive
        from tests.test_paper_evidence import EvidenceModel

        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {'MINIMAL_AGENT_HOME': directory,
                    'MINIMAL_AGENT_OUTPUT': str(Path(directory) / 'summaries')}):
                settings = Settings()
            settings.prepare()
            parameters = validate_parameters('paper_summary', {
                'paper_text': 'A visited set prevents repeated traversal. ' * 10,
                'include_presentation': True, 'presentation_minutes': 15,
                'presentation_language': 'en'})

            def produce(parameters, evidence, source, assets, *args):
                self.assertEqual(parameters['presentation_minutes'], 15)
                self.assertEqual(parameters['presentation_language'], 'en')
                self.assertTrue(evidence['summary']['units'])
                _write_presentation_fixture(assets / 'presentation.pptx')
                (assets / 'presentation-plan.json').write_text('{}')
                return {'slides': 1, 'figure_review': []}

            harness = PaperHarness(parameters, CapabilityContext(settings, client=EvidenceModel()))
            with patch('paper_presentation_rendering.create_presentation', side_effect=produce):
                harness.produce()
                harness.inspect()
                harness.finish()
            path = Path(harness.result['path'])
            assets = path.with_name(path.stem + '_assets')
            self.assertIn('presentation.pptx', path.read_text())
            with build_report_archive(settings.output_dir, path.name) as package, zipfile.ZipFile(package.stream) as z:
                self.assertEqual(z.read(assets.name + '/presentation.pptx'), (assets / 'presentation.pptx').read_bytes())
                self.assertIn(assets.name + '/presentation-plan.json', z.namelist())
            (assets / 'presentation.pptx').unlink()
            with self.assertRaisesRegex(RuntimeError, 'PPT 文件不存在'):
                harness.verify()



if __name__ == "__main__":
    unittest.main()
