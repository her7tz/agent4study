import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from storage_layout import artifact_alias, migrate_legacy, prepare_library, resolve_output_alias


class StorageMigration(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = self.base/'outputs'
        self.settings = SimpleNamespace(output_dir=self.root, data_home=self.base, database_path=self.base/'runs.db')
        prepare_library(self.root)
        with sqlite3.connect(self.settings.database_path) as db:
            db.execute('CREATE TABLE ui_tasks(capability,output_file)')
            db.execute('INSERT INTO ui_tasks VALUES(?,?)',('paper_summary','lesson.md'))
            db.execute('CREATE TABLE job_runs(output_path)')
            db.executemany('INSERT INTO job_runs VALUES(?)', [('lesson.md',),(str(self.root/'lesson.md'),)])
        self.doc = self.root/'lesson.md'; self.target=self.root/'paper_summary'
        self.doc.write_text('# 用户编辑的笔记\n![图](lesson_assets/chart.png)')
        self.assets = self.root/'lesson_assets';self.assets.mkdir()
        (self.assets/'chart.png').write_bytes(b'fixture-image')
        self.map = self.base/'output-aliases.json'

    def test_restart_after_only_document_was_moved(self):
        self.map.write_text(json.dumps({s:'paper_summary/'+s for s in ('lesson.md','lesson_assets')}))
        self.doc.rename(self.target/self.doc.name)
        edited=self.target/self.doc.name
        edited.write_text('# 中断后继续编辑\n![图](lesson_assets/chart.png)')
        for _ in range(2):
            migrate_legacy(self.settings)
            self.assertEqual((self.target/'lesson_assets/chart.png').read_bytes(),b'fixture-image')
            self.assertIn('中断后继续编辑',edited.read_text())
            self.assertEqual(resolve_output_alias('lesson_assets/chart.png',self.base),'paper_summary/lesson_assets/chart.png')
        self.assertFalse(self.assets.exists())
        digest=lambda name:hashlib.sha256(name.encode()).hexdigest()[:16]
        self.assertEqual(artifact_alias(digest('lesson.md'),self.base),digest('paper_summary/lesson.md'))
        with sqlite3.connect(self.settings.database_path) as db:
            self.assertEqual(db.execute('SELECT output_file FROM ui_tasks').fetchone()[0],'paper_summary/lesson.md')
            self.assertEqual({r[0] for r in db.execute('SELECT output_path FROM job_runs')},
                             {'paper_summary/lesson.md',str(edited.resolve())})

    def test_siblings_take_category_from_task_linked_document(self):
        (self.root/'lesson.html').write_text('offline copy')
        migrate_legacy(self.settings)
        self.assertEqual((self.target/'lesson.html').read_text(),'offline copy')
        self.assertTrue((self.target/'lesson.md').is_file())

    def test_dangling_destination_symlink_never_replaced(self):
        destination=self.target/self.doc.name
        destination.symlink_to(self.base/'missing')
        with self.assertRaises(ValueError): migrate_legacy(self.settings)
        self.assertTrue(destination.is_symlink())
        self.assertTrue(self.doc.exists())
        self.assertTrue(self.assets.exists())
        self.assertFalse(self.map.exists())

    def test_conflicting_task_categories_preserve_original_files(self):
        (self.root/'lesson.html').write_text('second')
        with sqlite3.connect(self.settings.database_path) as db:
            db.execute('INSERT INTO ui_tasks VALUES(?,?)',('video_summary','lesson.html'))
        with self.assertRaises(ValueError): migrate_legacy(self.settings)
        self.assertTrue(self.doc.is_file())
        self.assertFalse(self.map.exists())

    def test_failure_writing_database_restores_original_files(self):
        with sqlite3.connect(self.settings.database_path) as db:
            db.execute("CREATE TRIGGER abort_updates BEFORE UPDATE ON ui_tasks BEGIN SELECT RAISE(ABORT,'fixture'); END")
        with self.assertRaises(sqlite3.IntegrityError): migrate_legacy(self.settings)
        self.assertIn('用户编辑',self.doc.read_text())
        self.assertEqual((self.assets/'chart.png').read_bytes(),b'fixture-image')
        self.assertFalse((self.target/'lesson.md').exists())
        self.assertFalse(self.map.exists())

    def test_external_alias_is_rejected_without_moving_content(self):
        self.map.write_text(json.dumps({'lesson.md':'../lesson.md'}))
        with self.assertRaises(ValueError): migrate_legacy(self.settings)
        self.assertTrue(self.doc.exists())
        self.assertTrue(self.assets.exists())
