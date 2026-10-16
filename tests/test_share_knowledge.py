"""Offline public-share knowledge ingestion, provenance and migration checks.

All data lives under TemporaryDirectory. The worker thread, page reader,
knowledge extraction and OS notifications are replaced with local fixtures.
"""

import copy
import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from knowledge import KnowledgeError, KnowledgeService, KnowledgeStore
from knowledge_context import retrieve_context
from runtime_control import RunCancelled
from share_links import ShareLinkError


URL = "https://chatgpt.com/share/00000000-1111-2222-3333-444444444444"


def snapshot():
    return {"title": "递归学习分享", "source_title": "递归学习分享", "source_type": "chatgpt_share",
        "source_url": URL, "source_urls": [URL, "https://example.org/recursion"],
        "messages": [
            {"id": 1, "role": "user", "content": "怎样理解递归？", "source_message_id": "original-u1"},
            {"id": 2, "role": "assistant", "content": "递归需要终止条件。", "source_message_id": "original-a1"},
            {"id": 3, "role": "assistant", "content": "递归步骤把问题缩小。", "source_message_id": "original-a2"},
        ], "source_details": {"reader": "fixture", "message_count": 3,
            "limitations": ["只含公开分享文字，模型说法未独立核验。", "附件未读取。"]}}


def points():
    return [
        {"title": "递归终止条件", "content": "递归终止条件决定何时停止递归。", "tags": ["递归"], "message_ids": [1, 2], "source_urls": [URL]},
        {"title": "递归步骤", "content": "递归步骤必须把问题转化为更小的同类问题。", "tags": ["递归"], "message_ids": [3], "source_urls": [URL]},
    ]


class ShareKnowledgePipelineTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.settings = SimpleNamespace(data_home=root / "private", output_dir=root / "outputs",
            api_key="fixture-not-real-api-key", _profile_secrets=("fixture-not-real-api-key",))
        self.settings.output_dir.mkdir()
        self.chat_database = root / "chat.sqlite3"
        for name, value in (("knowledge.threading.Thread", None), ("harness.notify_local", None),
                ("agent_profiles.resolve_agent", self.settings)):
            patcher = patch(name, **({"return_value": value} if value is not None else {}))
            mocked = patcher.start()
            self.addCleanup(patcher.stop)
            if name == "harness.notify_local":
                self.notification = mocked
        self.service = KnowledgeService(self.chat_database, lambda: self.settings, logger=Mock())
        self.addCleanup(self.service.close)

    def dispatch(self, job):
        self.assertEqual(self.service.pending.get_nowait(), job["id"])
        self.service.pending.task_done()
        self.service._process(job["id"])
        return self.service.job(job["id"])

    def complete(self, title=""):
        job = self.service.request_share(URL, title)
        with patch("share_links.read_shared_conversation", return_value=snapshot()) as reader, \
                patch.object(self.service, "_extract", return_value=points()) as extract:
            completed = self.dispatch(job)
        reader.assert_called_once()
        extract.assert_called_once()
        self.assertEqual(completed["status"], "completed", completed.get("error"))
        return completed

    def counts(self):
        with self.service.store.connect() as database:
            return {name: database.execute("SELECT COUNT(*) FROM " + name).fetchone()[0]
                for name in ("knowledge_entries", "knowledge_documents", "knowledge_sources", "knowledge_batches")}

    def test_single_document_contains_all_points_and_stays_in_feature_directory(self):
        job = self.complete()
        document = job["result"]["document"]
        files = list(self.settings.output_dir.rglob("*.md"))
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].parent.name, "knowledge_archives")
        self.assertEqual(document["name"], "knowledge_archives/archive_" + job["id"] + ".md")
        text = files[0].read_text(encoding="utf-8")
        for point in points():
            self.assertIn(point["title"], text)
            self.assertIn(point["content"], text)
        self.assertIn(URL, text)
        self.assertIn("模型说法未独立核验", text)
        self.assertEqual(self.counts(), {"knowledge_entries": 2, "knowledge_documents": 1, "knowledge_sources": 2, "knowledge_batches": 1})
        self.assertEqual(job["source_type"], "chatgpt_share")
        self.assertEqual(job["source_url"], URL)
        self.assertNotIn("snapshot", job)
        archive = self.service.archives()[0]
        self.assertTrue(archive["available"])
        self.assertEqual(archive["source_url"], URL)

    def test_entry_evidence_preserves_roles_source_ids_and_no_fake_conversation_link(self):
        job = self.complete()
        entry = self.service.store.get(job["result"]["entry_ids"][0])
        source = entry["sources"][0]
        self.assertEqual(source["source_type"], "chatgpt_share")
        self.assertEqual(source["source_url"], URL)
        self.assertIsNone(source["conversation_id"])
        self.assertEqual([item["role"] for item in source["evidence"]], ["user", "assistant"])
        self.assertEqual([item["source_message_id"] for item in source["evidence"]], ["original-u1", "original-a1"])
        with self.service.store.connect() as database:
            saved = database.execute("SELECT messages FROM knowledge_documents WHERE id=?", (job["id"],)).fetchone()[0]
        self.assertEqual(json.loads(saved), snapshot()["messages"])

    def test_knowledge_context_uses_the_share_url_as_provenance(self):
        self.complete()
        context = retrieve_context(self.service.store, "递归终止条件", [])
        self.assertTrue(context)
        self.assertEqual(context[0]["sources"][0]["source_type"], "chatgpt_share")
        self.assertEqual(context[0]["sources"][0]["source_url"], URL)
        self.assertIsNone(context[0]["sources"][0]["conversation_id"])

    def test_queued_duplicate_url_host_query_and_normalized_title_reuse_one_job(self):
        first = self.service.request_share(URL, " 自定义   标题 ")
        second = self.service.request_share(URL.replace("chatgpt.com", "chat.openai.com") + "/?track=1#message", "自定义 标题")
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(self.service.pending.qsize(), 1)
        self.assertEqual(len(self.service.jobs()), 1)

    def test_completed_duplicate_reuses_snapshot_without_new_work(self):
        original = self.complete()
        duplicate = self.service.request_share(URL + "?tracking=2")
        retry = self.service.retry(original["id"])
        self.assertEqual(duplicate["id"], original["id"])
        self.assertEqual(retry["status"], "completed")
        self.assertTrue(self.service.pending.empty())
        self.assertEqual(len(list(self.settings.output_dir.rglob("*.md"))), 1)

    def test_user_title_changes_document_title_without_replacing_source_title(self):
        job = self.complete("我的递归笔记")
        self.assertEqual(job["source_title"], "递归学习分享")
        self.assertTrue(job["result"]["document"]["title"].startswith("我的递归笔记"))
        document = self.service.store.document(job["id"])
        self.assertEqual(document["title"], "我的递归笔记")
        self.assertEqual(document["source_title"], "递归学习分享")

    def test_read_failure_writes_no_knowledge_or_archive_and_retry_reads_again(self):
        job = self.service.request_share(URL)
        with patch("share_links.read_shared_conversation", side_effect=ShareLinkError("读取公开分享失败")), \
                patch.object(self.service, "_extract") as extract, \
                patch.object(self.service.store, "ingest", wraps=self.service.store.ingest) as ingest:
            failed = self.dispatch(job)
        self.assertEqual(failed["status"], "failed")
        self.assertIn("读取公开分享失败", failed["error"])
        extract.assert_not_called()
        ingest.assert_not_called()
        self.assertEqual(sum(self.counts().values()), 0)
        self.assertFalse(list(self.settings.output_dir.rglob("*.md")))
        self.notification.assert_called_once()
        self.assertNotIn(URL, str(self.notification.call_args))
        retry = self.service.retry(job["id"])
        self.assertEqual(retry["id"], job["id"])
        self.assertEqual(retry["status"], "queued")
        with patch("share_links.read_shared_conversation", return_value=snapshot()) as reader, \
                patch.object(self.service, "_extract", return_value=points()):
            completed = self.dispatch(retry)
        reader.assert_called_once()
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(self.service.store.count(), 2)

    def test_retry_discards_previously_fetched_share_before_failed_extraction(self):
        job = self.service.request_share(URL)
        with patch("share_links.read_shared_conversation", return_value=snapshot()), \
                patch.object(self.service, "_extract", side_effect=RuntimeError("model fixture failure")):
            failed = self.dispatch(job)
        self.assertEqual(failed["status"], "failed")
        retry = self.service.retry(job["id"])
        with self.service.connect() as database:
            pending_snapshot = json.loads(database.execute("SELECT snapshot FROM knowledge_ingestions WHERE id=?", (job["id"],)).fetchone()[0])
        self.assertEqual(pending_snapshot["messages"], [])
        self.assertNotIn("source_details", pending_snapshot)
        replacement = snapshot()
        replacement["source_title"] = "重试时读取的分享标题"
        with patch("share_links.read_shared_conversation", return_value=replacement) as reader, \
                patch.object(self.service, "_extract", return_value=points()):
            completed = self.dispatch(retry)
        self.assertEqual(completed["source_title"], "重试时读取的分享标题")
        reader.assert_called_once()

    def test_file_write_failure_retries_saved_batch_without_duplicate_ingestion(self):
        job = self.service.request_share(URL)
        with patch("share_links.read_shared_conversation", return_value=snapshot()), \
                patch.object(self.service, "_extract", return_value=points()), \
                patch.object(self.service, "_write_document", side_effect=OSError("fixture output failure")):
            failed = self.dispatch(job)
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(self.service.store.count(), 2)
        retry = self.service.retry(job["id"])
        with patch("share_links.read_shared_conversation") as reader, patch.object(self.service, "_extract") as extract:
            completed = self.dispatch(retry)
        reader.assert_not_called()
        extract.assert_not_called()
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(self.counts(), {"knowledge_entries": 2, "knowledge_documents": 1, "knowledge_sources": 2, "knowledge_batches": 1})
        self.assertEqual(len(list(self.settings.output_dir.rglob("*.md"))), 1)

    def test_empty_knowledge_still_saves_one_share_archive(self):
        job = self.service.request_share(URL)
        with patch("share_links.read_shared_conversation", return_value=snapshot()), patch.object(self.service, "_extract", return_value=[]):
            completed = self.dispatch(job)
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["result"]["count"], 0)
        self.assertEqual(self.service.store.count(), 0)
        files = list(self.settings.output_dir.rglob("*.md"))
        self.assertEqual(len(files), 1)
        self.assertIn("未发现可复用的知识点", files[0].read_text(encoding="utf-8"))

    def test_missing_api_key_fails_before_reading_remote_share(self):
        self.settings.api_key = ""
        job = self.service.request_share(URL)
        with patch("share_links.read_shared_conversation") as reader, patch.object(self.service, "_extract") as extract:
            failed = self.dispatch(job)
        self.assertEqual(failed["status"], "failed")
        self.assertIn("API Key", failed["error"])
        reader.assert_not_called()
        extract.assert_not_called()
        self.assertEqual(sum(self.counts().values()), 0)

    def test_shutdown_cancels_queued_work_and_rejects_new_requests(self):
        job = self.service.request_share(URL)
        token = self.service.tokens[job["id"]]
        self.service.close()
        with self.assertRaises(RunCancelled):
            token.check()
        with self.assertRaises(KnowledgeError) as error:
            self.service.request_share(URL)
        self.assertEqual(error.exception.status, 503)
        with patch("share_links.read_shared_conversation") as reader:
            interrupted = self.dispatch(job)
        self.assertEqual(interrupted["status"], "interrupted")
        reader.assert_not_called()

    def test_invalid_request_and_missing_job_do_not_enqueue(self):
        for url, title in (("https://example.org/share/abcdefgh", ""), (URL, "x" * 201), (URL, 123)):
            with self.subTest(url=url, title=title), self.assertRaises(KnowledgeError):
                self.service.request_share(url, title)
        self.assertTrue(self.service.pending.empty())
        with self.assertRaises(KnowledgeError) as invalid:
            self.service.job("not-a-job")
        self.assertEqual(invalid.exception.status, 400)
        with self.assertRaises(KnowledgeError) as missing:
            self.service.job("f" * 32)
        self.assertEqual(missing.exception.status, 404)


class ShareKnowledgeMigrationTests(unittest.TestCase):
    def test_legacy_knowledge_document_keeps_local_conversation_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "knowledge.sqlite3"
            messages = [{"id": 1, "role": "assistant", "content": "原来的递归解释"}]
            with sqlite3.connect(path) as database:
                database.executescript("""
                    CREATE TABLE knowledge_documents(id TEXT PRIMARY KEY,conversation_id TEXT NOT NULL,title TEXT NOT NULL,messages TEXT NOT NULL,created_at REAL NOT NULL);
                    CREATE TABLE knowledge_entries(id TEXT PRIMARY KEY,title TEXT NOT NULL,content TEXT NOT NULL,tags TEXT NOT NULL,title_key TEXT NOT NULL,fingerprint TEXT NOT NULL,revision INTEGER NOT NULL DEFAULT 1,created_at REAL NOT NULL,updated_at REAL NOT NULL,deleted_at REAL);
                    CREATE TABLE knowledge_sources(entry_id TEXT NOT NULL,document_id TEXT NOT NULL,message_ids TEXT NOT NULL,urls TEXT NOT NULL,PRIMARY KEY(entry_id,document_id));
                """)
                database.execute("INSERT INTO knowledge_documents VALUES(?,?,?,?,?)", ("legacy-batch", "local-conversation", "旧对话", json.dumps(messages), 1.0))
                database.execute("INSERT INTO knowledge_entries(id,title,content,tags,title_key,fingerprint,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)", ("legacy-entry", "递归", "原来的递归解释", '["递归"]', "递归", "legacy-fingerprint", 1.0, 1.0))
                database.execute("INSERT INTO knowledge_sources VALUES(?,?,?,?)", ("legacy-entry", "legacy-batch", "[1]", "[]"))
            store = KnowledgeStore(path)
            entry = store.get("legacy-entry")
            self.assertEqual(entry["content"], "原来的递归解释")
            self.assertEqual(entry["sources"][0]["source_type"], "conversation")
            self.assertEqual(entry["sources"][0]["conversation_id"], "local-conversation")
            self.assertEqual(entry["sources"][0]["source_url"], "")
            document = store.document("legacy-batch")
            self.assertEqual(document["source_type"], "conversation")
            self.assertEqual(document["source_details"], {})
            self.assertEqual(document["points"][0]["content"], "原来的递归解释")
            self.assertEqual(store.count(), 1)

    def test_legacy_ingestion_table_adds_source_metadata_without_losing_job(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "chat.sqlite3"
            local_snapshot = {"conversation_id": "local-chat", "title": "旧对话", "messages": [{"id": 1, "role": "assistant", "content": "递归需要终止条件。"}], "source_urls": []}
            fingerprint = hashlib.sha256(json.dumps(local_snapshot["messages"], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
            old_job_id = "a" * 32
            with sqlite3.connect(path) as database:
                database.execute("CREATE TABLE knowledge_ingestions(id TEXT PRIMARY KEY,conversation_id TEXT NOT NULL,fingerprint TEXT NOT NULL,snapshot TEXT NOT NULL,status TEXT NOT NULL,stage TEXT NOT NULL,created_at REAL NOT NULL,finished_at REAL,error TEXT,result TEXT,UNIQUE(conversation_id,fingerprint))")
                database.execute("INSERT INTO knowledge_ingestions(id,conversation_id,fingerprint,snapshot,status,stage,created_at) VALUES(?,?,?,?,'failed','旧任务失败',?)", (old_job_id, "local-chat", fingerprint, json.dumps(local_snapshot), 1.0))
            settings = SimpleNamespace(data_home=root / "private", output_dir=root / "outputs", api_key="fixture-key", _profile_secrets=())
            with patch("knowledge.threading.Thread"), patch("agent_profiles.resolve_agent", return_value=settings), patch("harness.notify_local"):
                service = KnowledgeService(path, lambda: settings)
                try:
                    migrated = service.job(old_job_id)
                    self.assertEqual(migrated["status"], "failed")
                    self.assertEqual(migrated["source_type"], "conversation")
                    self.assertEqual(migrated["source_url"], "")
                    retry = service.retry(old_job_id)
                    self.assertEqual(retry["id"], old_job_id)
                    point = copy.deepcopy(points()[0])
                    point["message_ids"], point["source_urls"] = [1], []
                    with patch("share_links.read_shared_conversation") as reader, patch.object(service, "_extract", return_value=[point]):
                        service._process(old_job_id)
                    reader.assert_not_called()
                    self.assertEqual(service.job(old_job_id)["status"], "completed")
                    self.assertEqual(service.store.get(service.job(old_job_id)["result"]["entry_ids"][0])["sources"][0]["conversation_id"], "local-chat")
                finally:
                    service.close()


if __name__ == "__main__":
    unittest.main()
