"""Exercise paper generation, provenance persistence, and offline export together."""
import io
import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from urllib.parse import unquote, urlsplit
from unittest.mock import patch

from business_harnesses import PaperHarness
from capabilities import CapabilityContext
from harness import Settings
from paper_summary import PaperError, PaperUploads, _render_paper_citations
from report_exports import ExportError, build_report_archive, _markdown_destinations
from tests.test_paper_workflow import sample_pdf


class EvidenceModel:
    def json_chat(self, system, payload, **kwargs):
        data = json.loads(payload)
        if data.get('source_review'):
            return {'overview': data['overview'], 'units': data['units'],
                    'unsupported_unit_ids': []}
        if 'blocks' in data:
            return {'overview': 'Traversal', 'units': [{'title': 'Visited set',
                'content': 'A visited set prevents repeated traversal.',
                'source_ids': [block['id'] for block in data['blocks']]}]}
        return {'overview': 'Traversal', 'units': [{'title': 'Visited set',
            'content': 'A visited set prevents repeated traversal.',
            'source_ids': list(data['sources']),
            'merged_from': [note['id'] for note in data['notes']]}]}


class PaperEvidenceTests(unittest.TestCase):
    def test_custom_report_filename_keeps_pdf_and_evidence_links_offline(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {'MINIMAL_AGENT_HOME': directory,
                    'MINIMAL_AGENT_OUTPUT': str(Path(directory) / 'summaries')}):
                settings = Settings()
            settings.prepare()
            original = sample_pdf()
            upload = PaperUploads(settings.data_home).save(original, 'source.pdf')
            path = settings.output_dir / '论文 #1 (复习) 100%.md'
            harness = PaperHarness({'upload_id': upload['upload_id']},
                CapabilityContext(settings, client=EvidenceModel(), output_path=path))
            harness.produce()
            harness.inspect()
            harness.finish()
            markdown = path.read_text()
            links = list(_markdown_destinations(markdown, images=False))
            self.assertEqual(len(links), 2)
            expected = {path.stem + '_assets/source-evidence.json',
                        path.stem + '_assets/original.pdf'}
            self.assertEqual({unquote(urlsplit(link).path) for link in links}, expected)
            self.assertTrue(all(not urlsplit(link).fragment for link in links))
            with build_report_archive(settings.output_dir, path.name) as bundle, \
                    zipfile.ZipFile(bundle.stream) as archive:
                self.assertEqual(set(archive.namelist()), expected | {path.name})
                offline = Path(directory) / 'offline'
                archive.extractall(offline)
            self.assertEqual((offline / path.name).read_text(), markdown)
            for link in links:
                target = offline / unquote(urlsplit(link).path)
                if target.suffix == '.pdf':
                    self.assertEqual(target.read_bytes(), original)
                else:
                    evidence = json.loads(target.read_text())
                    self.assertEqual(evidence['source_blocks'][0]['id'], 'paper-page-1')

    def test_pdf_page_locations_survive_extraction_merge_and_rendering(self):
        review_inputs = []

        class PageModel(EvidenceModel):
            def json_chat(self, system, payload, **kwargs):
                data = json.loads(payload)
                if data.get('source_review'):
                    review_inputs.append(data)
                if 'blocks' in data and not data.get('source_review'):
                    return {'overview': 'Two source pages', 'units': [
                        {'title': block['id'], 'content': 'A visited set prevents repetition.',
                         'source_ids': [block['id']]} for block in data['blocks']]}
                return super().json_chat(system, payload, **kwargs)

        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {'MINIMAL_AGENT_HOME': directory,
                    'MINIMAL_AGENT_OUTPUT': str(Path(directory) / 'summaries')}):
                settings = Settings()
            settings.prepare()
            upload = PaperUploads(settings.data_home).save(sample_pdf(2), 'two-pages.pdf')
            harness = PaperHarness({'upload_id': upload['upload_id']},
                                   CapabilityContext(settings, client=PageModel()))
            harness.produce()
            harness.inspect()
            harness.finish()
            self.assertIn('依据 k1，PDF 第 1、2 页', harness.result['markdown'])
            path = Path(harness.result['path'])
            evidence = json.loads(path.with_name(path.stem + '_assets').joinpath(
                'source-evidence.json').read_text())
            self.assertCountEqual(evidence['summary']['units'][0]['source_ids'],
                                  ['paper-page-1', 'paper-page-2'])
            self.assertEqual(len(review_inputs), 1)
            self.assertFalse(review_inputs[0]['source_subset'])
            self.assertEqual([block['locator']['page'] for block in review_inputs[0]['blocks']], [1, 2])
            self.assertIn('全部 2 个物理页', evidence['material']['scope'])

    def test_report_preserves_reviewed_conditions_with_generated_citation(self):
        body = 'A visited set prevents repeated traversal only for the finite graphs considered here.'

        class ReviewedModel(EvidenceModel):
            def json_chat(self, system, payload, **kwargs):
                result = super().json_chat(system, payload, **kwargs)
                if json.loads(payload).get('source_review'):
                    result['units'][0]['content'] = body
                return result

        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {'MINIMAL_AGENT_HOME': directory,
                    'MINIMAL_AGENT_OUTPUT': str(Path(directory) / 'summaries')}):
                settings = Settings()
            settings.prepare()
            harness = PaperHarness({'paper_text': 'A visited set prevents repeated traversal. '*10},
                                   CapabilityContext(settings, client=ReviewedModel()))
            harness.produce()
            harness.inspect()
            harness.finish()
            self.assertIn(body + '\n\n（依据 k1）', harness.result['markdown'])

    def test_paper_scope_asides_do_not_replace_source_limitations(self):
        example = '\n\n```text\n引用示例，本片段未提供原图。\n```'
        evidence = ('作者报告未缩放的点积注意力表现较差，本片段未提供对应的具体数值表。'
                    '表 3 报告 4.92 和 25.8。论文未报告独立复现。'
                    '附录文字来自图注，图像未做视觉识别。原文引用“这是对照，本片段未展示原图。”') + example
        expected = ('作者报告未缩放的点积注意力表现较差。表 3 报告 4.92 和 25.8。论文未报告独立复现。'
                    '附录文字来自图注，图像未做视觉识别。原文引用“这是对照，本片段未展示原图。”') + example

        class ScopeModel(EvidenceModel):
            def json_chat(self, system, payload, **kwargs):
                result = super().json_chat(system, payload, **kwargs)
                if json.loads(payload).get('source_review'):
                    result['overview'] = '论文报告四组对照，本块没有展开细节。'
                    result['units'][0]['content'] = evidence
                return result

        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {'MINIMAL_AGENT_HOME': directory,
                    'MINIMAL_AGENT_OUTPUT': str(Path(directory) / 'summaries')}):
                settings = Settings()
            settings.prepare()
            upload = PaperUploads(settings.data_home).save(sample_pdf(), 'source.pdf')
            harness = PaperHarness({'upload_id': upload['upload_id']}, CapabilityContext(settings, client=ScopeModel()))
            harness.produce()
            harness.inspect()
            harness.finish()
            report = Path(harness.result['path'])
            sources = json.loads(report.with_name(report.stem + '_assets').joinpath('source-evidence.json').read_text())
            self.assertIn(expected + '\n\n（依据 k1，PDF 第 1 页）', report.read_text())
            self.assertEqual(sources['summary']['overview'], '论文报告四组对照。')
            self.assertEqual(sources['summary']['units'][0]['content'], expected)
            self.assertEqual(harness.result['summary_processing']['processing_window_asides_removed'], 2)

    def test_text_and_pdf_preserve_evidence_through_export(self):
        for use_pdf in (False, True):
            with self.subTest(pdf=use_pdf), tempfile.TemporaryDirectory() as directory:
                with patch.dict(os.environ, {'MINIMAL_AGENT_HOME': directory,
                        'MINIMAL_AGENT_OUTPUT': str(Path(directory) / 'summaries')}):
                    settings = Settings()
                settings.prepare()
                if use_pdf:
                    uploaded = PaperUploads(settings.data_home).save(sample_pdf(), 'source.pdf')
                    parameters = {'upload_id': uploaded['upload_id']}
                else:
                    parameters = {'paper_text': 'A visited set prevents repeated traversal. ' * 10}
                harness = PaperHarness(parameters, CapabilityContext(settings, client=EvidenceModel()))
                harness.produce()
                harness.inspect()
                harness.finish()
                result = harness.result
                self.assertNotIn('_source_evidence', result)
                self.assertIn('依据 k1', result['markdown'])
                self.assertNotIn('[[k1]]', result['markdown'])
                if use_pdf:
                    self.assertIn('PDF 第 1 页', result['markdown'])
                path = Path(result['path'])
                evidence_path = path.with_name(path.stem + '_assets') / 'source-evidence.json'
                evidence = json.loads(evidence_path.read_text())
                source_id = 'paper-page-1' if use_pdf else 'paper-text'
                self.assertEqual(evidence['summary']['units'][0]['source_ids'], [source_id])
                self.assertEqual(evidence['source_blocks'][0]['id'], source_id)
                self.assertIn('visited set', evidence['source_blocks'][0]['text'])
                self.assertNotIn('_pdf_path', evidence['material'])
                if use_pdf:
                    self.assertEqual(evidence['summary']['sources'][source_id]['page'], 1)
                with build_report_archive(settings.output_dir, path.name) as bundle:
                    with zipfile.ZipFile(io.BytesIO(bundle.stream.read())) as archive:
                        self.assertEqual(archive.read(evidence_path.relative_to(settings.output_dir).as_posix()),
                                         evidence_path.read_bytes())
                        if use_pdf:
                            original = evidence_path.with_name('original.pdf')
                            self.assertEqual(archive.read(original.relative_to(settings.output_dir).as_posix()),
                                             original.read_bytes())
                evidence_path.unlink()
                with self.assertRaisesRegex(RuntimeError, '依据文件缺失'):
                    harness.verify()
                with self.assertRaises(ExportError):
                    build_report_archive(settings.output_dir, path.name)


class PaperCitationPositions(unittest.TestCase):
    def setUp(self):
        self.evidence = {'units': [{'id':'k1','source_ids':['p5','p3','p5']},
                                   {'id':'k2','source_ids':['p9']}],
                         'sources': {'p3':{'kind':'pdf','page':3}, 'p5':{'kind':'pdf','page':5},
                                     'p9':{'kind':'pdf','page':9}}}

    def test_method_and_experiment_positions_come_from_reader_locations(self):
        result = _render_paper_citations('Method [[k1]]. Experiment [[k2]]. Reuse [[k1]].', self.evidence)
        self.assertEqual(result, 'Method （依据 k1，PDF 第 3、5 页）. Experiment （依据 k2，PDF 第 9 页）. Reuse （依据 k1，PDF 第 3、5 页）.')

    def test_unknown_or_missing_body_citations_are_rejected(self):
        for text in ('Metadata alone is insufficient.', 'Only [[k1]].', '[[k1]][[k2]][[k99]]'):
            with self.subTest(text=text), self.assertRaisesRegex(PaperError, '依据编号'):
                _render_paper_citations(text, self.evidence)

    def test_code_examples_and_escaped_markers_are_preserved(self):
        code = '`[[k99]]` and ``x`[[k99]]``\n```python\n[[k99]]\n````\n~~~text\n[[k99]]\n~~~~\n\\[[k99]]\n'
        result = _render_paper_citations(code + 'Evidence [[k1]][[k2]].', self.evidence)
        self.assertTrue(result.startswith(code))
        self.assertIn('PDF 第 3、5 页', result)
        with self.assertRaises(PaperError):
            _render_paper_citations('`[[k1]]`\n```\n[[k2]]\n```', self.evidence)

    def test_hidden_comment_markers_do_not_count_as_body_citations(self):
        for text in ('Visible claim. <!-- [[k1]][[k2]] -->',
                     'Visible [[k1]]. <!-- [[k2]]',
                     'Visible [[k1]]. <!-- [[k2]] -->'):
            with self.subTest(text=text), self.assertRaisesRegex(PaperError, '依据编号'):
                _render_paper_citations(text, self.evidence)
        comment = '<!-- literal [[k99]] -->'
        rendered = _render_paper_citations(comment + '\nEvidence [[k1]][[k2]].', self.evidence)
        self.assertTrue(rendered.startswith(comment))
