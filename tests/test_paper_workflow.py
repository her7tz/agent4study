"""Disposable PDF and task lifecycle acceptance; no accounts or live models."""
import io
import logging
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from pypdf import PdfReader, PdfWriter
from pypdf.generic import ArrayObject, DecodedStreamObject, DictionaryObject, NameObject, NumberObject

from harness import Settings
from paper_summary import PaperError, PaperUploads, _paper_source_blocks, _pdf, read_paper
from runtime_control import CancellationToken
from scheduler import Scheduler
from web_server import APIError, UITaskStore, WebRuntime


def sample_pdf(pages=1, password=None, text=True, rotated=False):
    writer = PdfWriter()
    font = DictionaryObject({NameObject('/Type'): NameObject('/Font'),
        NameObject('/Subtype'): NameObject('/Type1'), NameObject('/BaseFont'): NameObject('/Helvetica')})
    font_ref = writer._add_object(font)
    for number in range(pages):
        page = writer.add_blank_page(width=612, height=792)
        if text:
            page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'):
                DictionaryObject({NameObject('/F1'): font_ref})})
            lines = [f'Synthetic paper page {number + 1}.'] + [
                'A visited set prevents repeated traversal of a finite graph.',
                'The algorithm assumes a finite set of vertices and edges.',
                'Every reachable vertex is processed once; cycles terminate.',
                'This example contains no empirical benchmark or performance claim.',
                'Its purpose is to check readable PDF material and page provenance.']
            stream = DecodedStreamObject()
            stream.set_data(('BT /F1 11 Tf 40 740 Td 16 TL ' +
                ' T* '.join('(' + line + ') Tj' for line in lines) + ' ET').encode())
            if rotated:
                stream.set_data(stream.get_data() + b'\nBT /F1 9 Tf 0 1 -1 0 550 220 Tm (ROTATED_LABEL) Tj ET')
            page[NameObject('/Contents')] = writer._add_object(stream)
    writer.add_metadata({'/Title': 'Synthetic finite graph paper'})
    if password:
        writer.encrypt(password)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def image_only_pdf(footer='', hidden_text=False):
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    image = DecodedStreamObject()
    image.update({NameObject('/Type'): NameObject('/XObject'),
                  NameObject('/Subtype'): NameObject('/Image'),
                  NameObject('/Width'): NumberObject(2),
                  NameObject('/Height'): NumberObject(2),
                  NameObject('/ColorSpace'): NameObject('/DeviceGray'),
                  NameObject('/BitsPerComponent'): NumberObject(8)})
    image.set_data(bytes([0, 255, 255, 0]))
    page[NameObject('/Resources')] = DictionaryObject({NameObject('/XObject'):
        DictionaryObject({NameObject('/Scan'): writer._add_object(image)})})
    content = DecodedStreamObject()
    content.set_data(b'q 400 0 0 600 40 40 cm /Scan Do Q')
    if footer:
        font = DictionaryObject({NameObject('/Type'): NameObject('/Font'),
            NameObject('/Subtype'): NameObject('/Type1'), NameObject('/BaseFont'): NameObject('/Helvetica')})
        page['/Resources'][NameObject('/Font')] = DictionaryObject({
            NameObject('/F1'): writer._add_object(font)})
        rendering = ' 3 Tr' if hidden_text else ''
        content.set_data(content.get_data() +
            ('\nBT /F1 10 Tf' + rendering + ' 40 20 Td (' + footer + ') Tj ET').encode())
    page[NameObject('/Contents')] = writer._add_object(content)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def nested_text_pdf(text, *, hidden=False, visible_heading=False):
    writer = PdfWriter()
    writer.append(PdfReader(io.BytesIO(image_only_pdf(text, hidden_text=hidden))))
    page = writer.pages[0]
    resources = page['/Resources']
    scan, nested_text = page.get_contents().get_data().split(b'\n', 1)

    def form(data, resources):
        stream = DecodedStreamObject()
        stream.update({NameObject('/Type'): NameObject('/XObject'),
            NameObject('/Subtype'): NameObject('/Form'), NameObject('/Resources'): resources,
            NameObject('/BBox'): ArrayObject([NumberObject(n) for n in (0, 0, 612, 792)])})
        stream.set_data(data)
        return writer._add_object(stream)

    inner = form(nested_text, DictionaryObject({NameObject('/Font'): resources['/Font']}))
    outer = form(b'/Inner Do', DictionaryObject({NameObject('/XObject'): DictionaryObject({NameObject('/Inner'): inner})}))
    resources['/XObject'][NameObject('/Outer')] = outer
    content = DecodedStreamObject()
    heading = b'\nBT /F1 10 Tf 40 740 Td (Visible page heading.) Tj ET' if visible_heading else b''
    content.set_data(scan + heading + b'\n/Outer Do')
    page[NameObject('/Contents')] = writer._add_object(content)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


class PDFMaterialContracts(unittest.TestCase):
    def parse(self, data):
        return _pdf(data, CancellationToken(), lambda _: None)

    def test_selectable_rotated_text_kept_with_layout_limitation(self):
        material = self.parse(sample_pdf(rotated=True))
        self.assertIn('ROTATED_LABEL', material['text'])
        self.assertIn('旋转文字', material['scope'])

    def test_real_pdf_text_keeps_page_provenance_and_scope(self):
        material = self.parse(sample_pdf(2))
        self.assertEqual(material['pages'], 2)
        self.assertEqual([item['page'] for item in material['_page_texts']], [1, 2])
        self.assertIn('cycles terminate', material['text'])
        self.assertIn('未做视觉识别', material['scope'])

    def test_password_empty_corruption_and_page_limit_are_explained(self):
        for data, reason in [(sample_pdf(password='private'), '密码'),
                             (sample_pdf(text=False), 'OCR'),
                             (b'%PDF-1.7\nbroken', '无法解析'),
                             (sample_pdf(201, text=False), '200 页')]:
            with self.subTest(reason=reason), self.assertRaisesRegex(PaperError, reason):
                self.parse(data)

    def test_valid_paper_with_blank_page_is_not_rejected_as_corrupt(self):
        writer = PdfWriter()
        writer.append(PdfReader(io.BytesIO(sample_pdf())))
        writer.add_blank_page(width=612, height=792)
        buffer = io.BytesIO()
        writer.write(buffer)
        material = self.parse(buffer.getvalue())
        self.assertEqual(material['pages'], 2)
        self.assertEqual(material['empty_pages'], [2])
        self.assertEqual(len(material['_page_texts']), 1)
        self.assertIn('第 2 页文字不足', material['scope'])

    def test_image_only_page_reports_missing_ocr_and_mixed_document_scope(self):
        scanned = image_only_pdf()
        page = PdfReader(io.BytesIO(scanned)).pages[0]
        self.assertEqual(page['/Resources']['/XObject']['/Scan']['/Subtype'], '/Image')
        self.assertIn(b'/Scan Do', page.get_contents().get_data())
        with self.assertRaisesRegex(PaperError, 'OCR'):
            self.parse(scanned)
        writer = PdfWriter()
        writer.append(PdfReader(io.BytesIO(sample_pdf())))
        writer.append(PdfReader(io.BytesIO(scanned)))
        buffer = io.BytesIO()
        writer.write(buffer)
        material = self.parse(buffer.getvalue())
        self.assertEqual(material['empty_pages'], [2])
        self.assertEqual([item['page'] for item in material['_page_texts']], [1])
        self.assertIn('第 2 页文字不足', material['scope'])
        self.assertIn('未做视觉识别', material['scope'])

    def test_upload_is_local_and_source_bytes_are_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            uploads = PaperUploads(Path(directory))
            original = sample_pdf()
            metadata = uploads.save(original, '../../paper.pdf')
            path, saved = uploads.get(metadata['upload_id'])
            self.assertEqual(path.read_bytes(), original)
            self.assertNotIn('/', saved['filename'])
            self.assertEqual(list(Path(directory).rglob('*.md')), [])

    def test_page_labels_and_sparse_footers_do_not_make_scans_readable(self):
        for footer in ('1', 'Scanned page'):
            with self.subTest(footer=footer):
                writer = PdfWriter()
                scanned = PdfReader(io.BytesIO(image_only_pdf(footer)))
                for _ in range(40):
                    writer.add_page(scanned.pages[0])
                buffer = io.BytesIO()
                writer.write(buffer)
                with self.assertRaisesRegex(PaperError, 'OCR'):
                    self.parse(buffer.getvalue())
                writer.append(PdfReader(io.BytesIO(sample_pdf())))
                buffer = io.BytesIO()
                writer.write(buffer)
                material = self.parse(buffer.getvalue())
                self.assertEqual(material['empty_pages'], list(range(1, 41)))
                self.assertIn('cycles terminate', material['text'])

    def test_hidden_scan_text_is_disclosed_and_sparse_pages_are_not_evidence(self):
        writer = PdfWriter()
        writer.append(PdfReader(io.BytesIO(sample_pdf())))
        writer.append(PdfReader(io.BytesIO(image_only_pdf('Recoverable scan text. ' * 12, hidden_text=True))))
        writer.append(PdfReader(io.BytesIO(image_only_pdf('SCAN FOLIO', hidden_text=True))))
        writer.append(PdfReader(io.BytesIO(image_only_pdf('Folio ' + 'X Y Z 0 a b c d e f ' * 3, hidden_text=True))))
        writer.append(PdfReader(io.BytesIO(image_only_pdf('Readable OCR after the diagram.', hidden_text=True))))
        output = io.BytesIO()
        writer.write(output)
        material = self.parse(output.getvalue())
        self.assertEqual(material['hidden_text_pages'], [2, 3, 4, 5])
        self.assertEqual(material['empty_pages'], [3])
        self.assertEqual(material['fragmented_scan_pages'], [4])
        self.assertEqual([item['page'] for item in material['_page_texts']], [1, 2, 5])
        self.assertIn('Recoverable scan text', material['text'])
        self.assertIn('Readable OCR after the diagram', material['text'])
        self.assertNotIn('SCAN FOLIO', material['text'])
        self.assertIn('PDF 内嵌的不可见文字层', material['scope'])
        self.assertIn('未与页面图像核对', material['scope'])
        self.assertIn('第 4 页内嵌文字多为短字符碎片', material['scope'])

    def test_nested_form_text_is_recovered_without_inventing_layout_or_evidence(self):
        complete = 'Nested form has the only complete discussion of this algorithm. ' * 6
        writer = PdfWriter()
        for data in (nested_text_pdf(complete, visible_heading=True),
                     nested_text_pdf('Embedded scan text remains visible to extraction. ' * 7, hidden=True),
                     nested_text_pdf('Folio ' + 'X Y Z 0 a b c d e f ' * 3, hidden=True),
                     sample_pdf()):
            writer.append(PdfReader(io.BytesIO(data)))
        output = io.BytesIO()
        writer.write(output)
        material = self.parse(output.getvalue())
        self.assertIn('Visible page heading.', material['text'])
        self.assertIn(complete.strip(), material['text'])
        self.assertIn('Embedded scan text remains visible to extraction.', material['text'])
        self.assertEqual(material['hidden_text_pages'], [2, 3])
        self.assertEqual(material['fragmented_scan_pages'], [3])
        self.assertEqual(material['form_text_pages'], [1, 2])
        self.assertIn('第 1、2 页按内容流顺序提取文字', material['scope'])
        blocks = _paper_source_blocks(material['text'], material, material['_page_texts'])
        self.assertEqual([block.locator['page'] for block in blocks], [1, 2, 4])
        self.assertEqual(blocks[0].locator['text_order'], 'content_stream')
        self.assertNotIn('table_layout', blocks[0].locator)


class PaperTaskLifecycle(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        with patch.dict(os.environ, {'MINIMAL_AGENT_HOME': temporary.name,
                'MINIMAL_AGENT_OUTPUT': str(Path(temporary.name) / 'summaries'),
                'DEEPSEEK_API_KEY': 'synthetic-key'}):
            self.settings = Settings()
        with patch.object(WebRuntime, '_schedule_loop'):
            self.runtime = WebRuntime(self.settings, logging.getLogger('paper-workflow'))
        self.addCleanup(self.runtime.close)
        self.upload = PaperUploads(self.settings.data_home).save(sample_pdf(), 'fixture.pdf')
        self.notify = patch('web_server.notify_local').start()
        self.addCleanup(patch.stopall)
        patch('site_sessions._addresses', return_value=['93.184.216.34']).start()

    def task(self):
        task = self.runtime.tasks.create('https://ieeexplore.ieee.org/document/11029963',
            False, 'fixture', capability='paper_summary', parameters={
                'url': 'https://ieeexplore.ieee.org/document/11029963', 'title': 'Fixture'}, title='论文总结')
        self.runtime.tasks.update(task['id'], conversation_id=self.runtime.chat.current()['conversation_id'])
        self.runtime.task_tokens[task['id']] = CancellationToken()
        return self.runtime.tasks.get(task['id'])

    def test_immediate_resume_keeps_new_attempt_cancellation_token(self):
        task = self.task()
        previous = self.runtime.task_tokens[task['id']]
        new_tokens = []

        def notification(title, *_):
            if title == '需要论文材料':
                self.runtime.resume_task(task['id'], self.upload['upload_id'])
                new_tokens.append(self.runtime.task_tokens[task['id']])

        self.notify.side_effect = notification
        with patch.object(self.runtime.executor, 'submit') as submit, \
                patch.object(self.runtime.chat, 'deliver_task') as deliver:
            self.runtime._run_capability(task['id'], self.settings)
        submit.assert_called_once()
        deliver.assert_not_called()
        self.assertIsNot(previous, new_tokens[0])
        self.assertIs(self.runtime.task_tokens.get(task['id']), new_tokens[0])
        self.assertEqual(self.runtime.tasks.get(task['id'])['attempt'], 2)
        self.runtime.cancel_task(task['id'])
        self.assertTrue(new_tokens[0].event.is_set())

    def test_import_continues_same_task_and_preserves_source(self):
        task = self.task()
        self.runtime._run_capability(task['id'], self.settings)
        waiting = self.runtime.tasks.get(task['id'])
        self.assertEqual(waiting['status'], 'waiting_user')
        session_id = waiting['session_id']
        with patch.object(self.runtime.executor, 'submit'):
            queued = self.runtime.resume_task(task['id'], self.upload['upload_id'])
            with self.assertRaises(APIError):
                self.runtime.resume_task(task['id'], self.upload['upload_id'])
        self.assertEqual(queued['id'], task['id'])
        self.assertEqual(queued['attempt'], 2)
        self.assertEqual(queued['parameters']['source_url'], task['url'])

        def execute(capability, parameters, settings, **kwargs):
            material = read_paper(parameters, settings, kwargs['cancellation'], kwargs['progress'])
            self.assertEqual(material['pages'], 1)
            self.assertEqual(Path(material['_pdf_path']).read_bytes(), sample_pdf())
            output = settings.output_dir / 'paper_summary' / 'fixture.md'
            output.parent.mkdir(exist_ok=True)
            output.write_text('# Fixture\n\nVerified PDF import.')
            return {'path': str(output), 'markdown': output.read_text(), 'title': 'Fixture'}

        with patch('web_server.execute_capability', side_effect=execute):
            self.runtime._run_capability(task['id'], self.settings)
        done = self.runtime.tasks.get(task['id'], with_markdown=True)
        self.assertEqual(done['status'], 'completed')
        self.assertEqual(done['session_id'], session_id)
        self.assertEqual(len(self.runtime.tasks.list()), 1)
        self.runtime.site_sessions.update(session_id, 'clear_pending')
        self.runtime.site_sessions.update(session_id, 'cleared')
        self.assertTrue((self.settings.output_dir / done['output_file']).is_file())

    def test_import_retains_selected_presentation_options(self):
        parameters = {'url': 'https://ieeexplore.ieee.org/document/11029963',
                      'include_presentation': True, 'presentation_minutes': 15,
                      'presentation_language': 'en'}
        task = self.runtime.tasks.create(parameters['url'], False, 'fixture',
            capability='paper_summary', parameters=parameters, title='论文 PPT')
        self.runtime.tasks.update(task['id'], status='waiting_user')
        with patch.object(self.runtime.executor, 'submit'):
            resumed = self.runtime.resume_task(task['id'], self.upload['upload_id'])
        self.assertEqual(resumed['id'], task['id'])
        self.assertEqual(resumed['parameters']['upload_id'], self.upload['upload_id'])
        for key in ('include_presentation', 'presentation_minutes', 'presentation_language'):
            self.assertEqual(resumed['parameters'][key], parameters[key])

    def test_queued_cancellation_never_calls_capability(self):
        task = self.task()
        self.runtime.cancel_task(task['id'])
        with patch('web_server.execute_capability') as execute:
            self.runtime._run_capability(task['id'], self.settings)
        execute.assert_not_called()
        self.assertEqual(self.runtime.tasks.get(task['id'])['status'], 'cancelled')

    def test_restart_marks_only_unfinished_execution_interrupted(self):
        task = self.task()
        waiting = self.task()
        self.runtime.tasks.update(waiting['id'], status='waiting_user')
        restarted = UITaskStore(self.settings.database_path)
        self.assertEqual(restarted.get(task['id'])['status'], 'interrupted')
        self.assertEqual(restarted.get(waiting['id'])['status'], 'waiting_user')
        restarted.update(task['id'], status='completed')
        self.assertEqual(restarted.get(task['id'])['status'], 'interrupted')

    def scheduled_paper(self):
        task = self.task()
        due = 100.0
        self.runtime.store.add('paper', task['url'], 'once', None, due,
                               capability='paper_summary', parameters=task['parameters'])
        self.assertEqual(self.runtime.store.claim_due('paper', due, None, False), 'new')
        self.runtime.store.record_run('paper', due, 'running', '开始执行')
        self.runtime.tasks.update(task['id'], schedule_job_id='paper', scheduled_at=due)
        return task, due

    def test_scheduled_paper_continuation_keeps_one_run_through_restart_and_cancel(self):
        task, due = self.scheduled_paper()
        self.runtime.tasks.update(task['id'], status='waiting_user', stage='等待论文 PDF')
        run, = self.runtime.store.recent_runs()
        self.assertEqual(run['status'], 'waiting_user')

        with patch.object(self.runtime.executor, 'submit'):
            self.runtime.resume_task(task['id'], self.upload['upload_id'])
        self.assertEqual(self.runtime.store.recent_runs()[0]['status'], 'queued')
        self.runtime.tasks.update(task['id'], status='running', stage='读取已导入的 PDF')
        self.assertEqual(self.runtime.store.recent_runs()[0]['status'], 'running')
        self.runtime.tasks.update(task['id'], stage='检查论文全文')
        self.assertEqual(self.runtime.store.recent_runs()[0]['detail'], '检查论文全文')
        self.assertFalse(self.runtime.store.record_run('paper', due, 'waiting_user', '迟到的旧阶段'))
        Scheduler(self.runtime.store, self.settings, Mock(), self.runtime.logger,
                  executor=self.runtime.executor).recover_pending()
        self.assertEqual(self.runtime.store.recent_runs()[0]['status'], 'running')

        restarted = UITaskStore(self.settings.database_path)
        self.assertEqual(restarted.get(task['id'])['status'], 'interrupted')
        interrupted, = self.runtime.store.recent_runs()
        self.assertEqual((interrupted['id'], interrupted['status']), (run['id'], 'interrupted'))
        self.assertFalse(self.runtime.store.get_jobs()[0]['enabled'])

        with patch.object(self.runtime.executor, 'submit'):
            self.runtime.resume_task(task['id'], self.upload['upload_id'])
        self.assertEqual(self.runtime.cancel_scheduled_run(run['id'])['status'], 'cancelled')
        self.assertEqual(self.runtime.tasks.get(task['id'])['status'], 'cancelled')
        final, = self.runtime.store.recent_runs()
        self.assertEqual((final['id'], final['status']), (run['id'], 'cancelled'))

    def test_legacy_scheduled_paper_wait_can_resume_and_publish_to_same_run(self):
        task, due = self.scheduled_paper()
        with self.runtime.tasks._connect() as db:
            db.execute("UPDATE ui_tasks SET status='waiting_user' WHERE id=?", (task['id'],))
        self.runtime.store.record_run('paper', due, 'interrupted', '旧版进程在标记等待时退出')
        run, = self.runtime.store.recent_runs()
        self.runtime.tasks = UITaskStore(self.settings.database_path)
        self.assertEqual(self.runtime.store.recent_runs()[0]['status'], 'waiting_user')
        with patch.object(self.runtime.executor, 'submit'):
            self.runtime.resume_task(task['id'], self.upload['upload_id'])
        output = self.settings.output_dir / 'paper_summary' / 'resumed.md'
        output.parent.mkdir(exist_ok=True)
        output.write_text('# Synthetic paper')
        with patch('web_server.execute_capability', return_value={'path': str(output)}):
            self.runtime._run_capability(task['id'], self.settings)
        completed, = self.runtime.store.recent_runs()
        self.assertEqual((completed['id'], completed['status']), (run['id'], 'completed'))
        self.assertEqual(Path(completed['output_path']), output.resolve())


if __name__ == '__main__':
    unittest.main()
