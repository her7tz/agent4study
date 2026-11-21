"""FR-12/15: real document/resources, exports, trash and application boundary."""
import base64
import hashlib
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from artifacts import ArtifactStore, ArtifactError
from local_apps import LocalAppService, LocalAppError, AppInstallation
from report_exports import build_report_archive, ExportError
from harness import JobStore
from web_server import UITaskStore


PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=')


class ContentContracts(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = ArtifactStore(self.root / 'library', self.root / 'tasks.sqlite3', self.root / 'private')
        self.tasks = UITaskStore(self.root / 'tasks.sqlite3')
        self.path = self.store.root / 'video_summary' / 'sample.md'
        self.path.parent.mkdir()
        self.assets = self.path.with_name('sample_assets')
        self.assets.mkdir()
        (self.assets / 'frame.png').write_bytes(PNG)
        self.path.write_text('# 知识笔记\n\n![原图](sample_assets/frame.png)\n', encoding='utf-8')
        self.note = self.store.list()[0]

    def test_export_contains_original_image_with_working_relative_path(self):
        with build_report_archive(self.store.root, self.note['name']) as package:
            with zipfile.ZipFile(package.stream) as archive:
                self.assertEqual(set(archive.namelist()), {'video_summary/sample.md', 'video_summary/sample_assets/frame.png'})
                self.assertEqual(archive.read('video_summary/sample_assets/frame.png'), PNG)
                self.assertIn(b'sample_assets/frame.png', archive.read('video_summary/sample.md'))

    def test_missing_image_fails_instead_of_exporting_broken_report(self):
        (self.assets / 'frame.png').unlink()
        with self.assertRaises(ExportError):
            build_report_archive(self.store.root, self.note['name'])

    def test_export_never_follows_an_image_symlink_outside_library(self):
        image = self.assets / 'frame.png'
        image.unlink()
        outside = self.root / 'outside.png'
        outside.write_bytes(PNG)
        image.symlink_to(outside)
        with self.assertRaises(ExportError):
            build_report_archive(self.store.root, self.note['name'])

    def test_zip_uses_actual_markdown_links_for_fixed_paper_companions(self):
        pdf = b'%PDF-1.7\nsynthetic fixture'
        evidence = b'{"version":1}\n'
        (self.assets / 'original.pdf').write_bytes(pdf)
        (self.assets / 'source-evidence.json').write_bytes(evidence)
        (self.assets / 'unlinked.json').write_text('{}')
        text = ('# 编辑后的笔记\n\n![原图](sample_assets/frame.png)\n\n'
                '[原文][原件]、[依据](<./sample_assets/source-evidence.json> "来源")。\n\n'
                '[原件]: ./sample_assets/original.pdf#page=1 "阅读"\n'
                '[其他附件](sample_assets/unlinked.json)\n')
        self.path.write_text(text)
        with build_report_archive(self.store.root, self.note['name']) as package, zipfile.ZipFile(package.stream) as archive:
            self.assertEqual(archive.read('video_summary/sample.md').decode(), text)
            self.assertEqual(archive.read('video_summary/sample_assets/original.pdf'), pdf)
            self.assertEqual(archive.read('video_summary/sample_assets/source-evidence.json'), evidence)
            self.assertNotIn('video_summary/sample_assets/unlinked.json', archive.namelist())

    def test_literal_or_unused_companion_paths_are_not_packaged(self):
        self.path.write_text('# 引用示例\n\n(sample_assets/original.pdf)\n\n'
                             '<!-- [依据](sample_assets/source-evidence.json) -->\n'
                             '[未引用]: sample_assets/original.pdf\n\n'
                             '```md\n[原文](sample_assets/original.pdf)\n```\n')
        with build_report_archive(self.store.root, self.note['name']) as package, zipfile.ZipFile(package.stream) as archive:
            self.assertEqual(archive.namelist(), ['video_summary/sample.md'])

    def test_longer_closing_code_fence_preserves_following_images_and_evidence(self):
        evidence = b'{"version":1}\n'
        (self.assets / 'source-evidence.json').write_bytes(evidence)
        for marker, newline in [('`', '\n'), ('~', '\r\n')]:
            with self.subTest(marker=marker, newline=newline):
                markdown = newline.join([
                    '# 编辑后的笔记', '', marker * 4 + 'markdown', marker * 3,
                    '![代码示例](sample_assets/missing.png)', '  ' + marker * 5 + ' ', '',
                    '![原图](sample_assets/frame.png)', '[依据](sample_assets/source-evidence.json)', '',
                ]).encode('utf-8')
                self.path.write_bytes(markdown)
                with build_report_archive(self.store.root, self.note['name']) as package, zipfile.ZipFile(package.stream) as archive:
                    self.assertEqual(archive.read('video_summary/sample.md'), markdown)
                    self.assertEqual(archive.read('video_summary/sample_assets/frame.png'), PNG)
                    self.assertEqual(archive.read('video_summary/sample_assets/source-evidence.json'), evidence)
                    self.assertEqual(len(archive.namelist()), 3)

    def test_quoted_code_fences_do_not_package_example_destinations(self):
        evidence = b'{"version":1}\n'
        (self.assets / 'source-evidence.json').write_bytes(evidence)
        for opening, quoted_content, closing in [
            ('> ~~~md', '> ', '> ~~~~'),
            ('> > ~~~md', '> > ', '>'),
            ('> ~~~md', '> ', ''),
        ]:
            with self.subTest(opening=opening, closing=closing):
                markdown = '\n'.join([
                    '# 笔记', '', opening,
                    quoted_content + '![代码示例](sample_assets/missing.png)',
                    quoted_content + '[不存在的原文](sample_assets/original.pdf)',
                    closing, '', '> ![原图](sample_assets/frame.png)',
                    '> [依据](sample_assets/source-evidence.json)', '',
                ]).encode('utf-8')
                self.path.write_bytes(markdown)
                with build_report_archive(self.store.root, self.note['name']) as bundle, zipfile.ZipFile(bundle.stream) as archive:
                    self.assertEqual(archive.read('video_summary/sample.md'), markdown)
                    self.assertEqual(archive.read('video_summary/sample_assets/frame.png'), PNG)
                    self.assertEqual(archive.read('video_summary/sample_assets/source-evidence.json'), evidence)
                    self.assertEqual(len(archive.namelist()), 3)

    def test_markdown_code_and_real_links_at_the_same_indentation(self):
        evidence = b'{"version":1}\n'
        (self.assets / 'source-evidence.json').write_bytes(evidence)
        text = ('# 笔记\n\n'
                '    ![缩进代码](sample_assets/missing.png)\n\n'
                '1. 实际步骤\n\n'
                '   ~~~md\n'
                '   [名单示例](sample_assets/original.pdf)\n'
                '   ![名单内代码](sample_assets/list-missing.png)\n'
                '   ~~~\n\n'
                '   ![真实原图][图]\n'
                '   [真实依据][依据]\n\n'
                '[图]: sample_assets/frame.png\n'
                '[依据]: sample_assets/source-evidence.json\n')
        self.path.write_text(text)
        with build_report_archive(self.store.root, self.note['name']) as bundle, zipfile.ZipFile(bundle.stream) as archive:
            self.assertEqual(set(archive.namelist()), {'video_summary/sample.md',
                'video_summary/sample_assets/frame.png', 'video_summary/sample_assets/source-evidence.json'})
            self.assertEqual(archive.read('video_summary/sample.md').decode(), text)
            self.assertEqual(archive.read('video_summary/sample_assets/frame.png'), PNG)
            self.assertEqual(archive.read('video_summary/sample_assets/source-evidence.json'), evidence)

    def test_export_preserves_legacy_html_image_with_escaped_filename(self):
        (self.assets / 'shot&view.png').write_bytes(PNG)
        self.path.write_text('# 笔记\n\n<img data-src="sample_assets/missing.png" src="sample_assets/shot&amp;view.png">\n')
        with build_report_archive(self.store.root, self.note['name']) as bundle, zipfile.ZipFile(bundle.stream) as archive:
            self.assertEqual(set(archive.namelist()), {'video_summary/sample.md', 'video_summary/sample_assets/shot&view.png'})
            self.assertEqual(archive.read('video_summary/sample_assets/shot&view.png'), PNG)

    def test_delete_restore_preserves_resource_family_and_task_links(self):
        self.path.with_suffix('.html').write_text('<h1>副本</h1>')
        task = self.tasks.create('https://youtu.be/fixture', True, 'fixture')
        self.tasks.update(task['id'], status='completed', stage='完成',
                          markdown=self.path.read_text(), output_file=self.note['name'])
        deleted = self.store.delete([self.note['id']])['deleted'][0]
        self.assertFalse(self.path.exists())
        self.assertFalse(self.assets.exists())
        hidden = self.tasks.get(task['id'], with_markdown=True)
        self.assertEqual(hidden['status'], 'deleted')
        self.assertIsNone(hidden['markdown'])
        self.assertIsNone(hidden['output_file'])
        restored = self.store.restore([deleted['trash_id']])['restored'][0]
        self.assertEqual(restored['id'], self.note['id'])
        self.assertEqual((self.assets / 'frame.png').read_bytes(), PNG)
        self.assertTrue(self.path.with_suffix('.html').exists())
        self.assertEqual(self.tasks.get(task['id'])['output_file'], self.note['name'])
        self.assertEqual(self.tasks.get(task['id'])['status'], 'completed')

    def test_existing_document_blocks_restore_without_overwrite(self):
        deleted = self.store.delete([self.note['id']])['deleted'][0]
        self.path.write_text('# 用户新写的内容')
        with self.assertRaises(ArtifactError) as raised:
            self.store.restore([deleted['trash_id']])
        self.assertEqual(raised.exception.status, 409)
        self.assertEqual(self.path.read_text(), '# 用户新写的内容')
        self.assertEqual(len(self.store.list_trash()), 1)

    def test_two_trashed_versions_cannot_overwrite_each_other_in_one_restore(self):
        first = self.store.delete([self.note['id']])['deleted'][0]
        self.path.write_text('# 第二个版本')
        second = self.store.delete([self.note['id']])['deleted'][0]
        with self.assertRaises(ArtifactError) as raised:
            self.store.restore([first['trash_id'], second['trash_id']])
        self.assertEqual(raised.exception.status, 409)
        self.assertFalse(self.path.exists())
        self.assertEqual(len(self.store.list_trash()), 2)

    def test_historical_id_and_current_id_move_one_document_family(self):
        self.store.data_home.mkdir()
        (self.store.data_home / 'output-aliases.json').write_text(json.dumps({'sample.md': self.note['name']}))
        legacy_id = hashlib.sha256(b'sample.md').hexdigest()[:16]
        task = self.tasks.create('https://youtu.be/fixture', True, 'fixture')
        self.tasks.update(task['id'], status='completed', stage='完成', output_file=self.note['name'])
        jobs = JobStore(self.store.database)
        jobs.add('fixture', 'https://youtu.be/fixture', 'once', None, 100)
        jobs.record_run('fixture', 100, 'completed', '完成', str(self.path))
        deleted, = self.store.delete([legacy_id, self.note['id'], legacy_id])['deleted']
        self.assertFalse(self.path.exists())
        self.assertFalse(self.assets.exists())
        self.assertEqual(self.tasks.get(task['id'])['status'], 'deleted')
        self.assertIsNone(jobs.recent_runs()[0]['output_path'])
        self.assertEqual(len(self.store.list_trash()), 1)
        restored, = self.store.restore([deleted['trash_id'], deleted['trash_id']])['restored']
        self.assertEqual(restored['id'], self.note['id'])
        self.assertEqual(self.store.read(legacy_id)['id'], restored['id'])
        self.assertEqual((self.assets / 'frame.png').read_bytes(), PNG)
        self.assertEqual(self.tasks.get(task['id'])['output_file'], self.note['name'])
        self.assertEqual(jobs.recent_runs()[0]['output_path'], str(self.path))

    def test_mixed_case_suffixes_use_real_paths_for_trash_and_restore(self):
        report = self.path.with_name('caps.MD')
        html = self.path.with_name('caps.HTML')
        report.write_text('# 大写扩展名')
        html.write_text('<h1>副本</h1>')
        note = next(item for item in self.store.list() if item['name'].endswith('caps.MD'))
        deleted, = self.store.delete([note['id']])['deleted']
        self.assertFalse(report.exists())
        self.assertFalse(html.exists())
        restored, = self.store.restore([deleted['trash_id']])['restored']
        self.assertEqual(restored['id'], note['id'])
        self.assertEqual({path.name for path in report.parent.iterdir() if path.stem == 'caps'},
                         {'caps.MD', 'caps.HTML'})

    def test_overlapping_batch_is_rejected_before_moving_any_file(self):
        child = self.assets / 'related.md'
        child.write_text('# 资源目录内的笔记')
        child_id = next(note['id'] for note in self.store.list() if note['name'].endswith('related.md'))
        with self.assertRaises(ArtifactError) as raised, patch('artifacts.shutil.move') as move:
            self.store.delete([self.note['id'], child_id])
        self.assertEqual(raised.exception.status, 409)
        move.assert_not_called()
        self.assertTrue(self.path.exists())
        self.assertTrue(child.exists())
        self.assertEqual(self.store.list_trash(), [])

    def test_parent_trash_and_restore_updates_links_to_nested_documents(self):
        child = self.assets / 'related.md'
        child.write_text('# 资源目录内的笔记')
        child_name = child.relative_to(self.store.root).as_posix()
        task = self.tasks.create('https://youtu.be/fixture', True, 'fixture')
        self.tasks.update(task['id'], status='completed', stage='完成', output_file=child_name)
        deleted, = self.store.delete([self.note['id']])['deleted']
        self.assertFalse(child.exists())
        self.assertEqual(self.tasks.get(task['id'])['status'], 'deleted')
        self.store.restore([deleted['trash_id']])
        self.assertEqual(child.read_text(), '# 资源目录内的笔记')
        self.assertEqual(self.tasks.get(task['id'])['output_file'], child_name)

    def test_restoring_with_an_invalid_id_leaves_every_entry_in_trash(self):
        deleted, = self.store.delete([self.note['id']])['deleted']
        with self.assertRaises(ArtifactError) as raised:
            self.store.restore([deleted['trash_id'], {'id': 'invalid'}])
        self.assertEqual(raised.exception.status, 400)
        self.assertFalse(self.path.exists())
        self.assertEqual(len(self.store.list_trash()), 1)

    def platform(self):
        platform = Mock()
        platform.name = 'darwin'
        platform.discover.return_value = AppInstallation(
            Path('/Applications/Typora.app'), 'abnerworks.Typora', 'fixture', ('.md', '.txt'), True)
        return platform

    def test_typora_submits_only_resolved_library_document(self):
        platform = self.platform()
        result = LocalAppService(self.store, platform=platform).open_document('typora', self.note['id'])
        self.assertEqual(result['status'], 'submitted')
        self.assertEqual(platform.launch.call_args.args[1], self.path)
        self.assertNotIn('completed', result.values())

    def test_typora_rejects_arbitrary_path_without_launch(self):
        platform = self.platform()
        with self.assertRaises(ArtifactError):
            LocalAppService(self.store, platform=platform).open_document('typora', '/etc/passwd')
        platform.launch.assert_not_called()

    def test_missing_typora_is_actionable_and_does_not_launch(self):
        platform = self.platform()
        platform.discover.return_value = None
        with self.assertRaises(LocalAppError) as raised:
            LocalAppService(self.store, platform=platform).open_document('typora', self.note['id'])
        self.assertEqual(raised.exception.code, 'app_not_installed')
        platform.launch.assert_not_called()


if __name__ == '__main__':
    unittest.main()
