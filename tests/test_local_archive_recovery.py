import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from conversation import ChatService
from knowledge import KnowledgeService


class ArchiveRecovery(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        root=Path(tmp.name)
        self.settings=SimpleNamespace(database_path=root/'chat.db',data_home=root,
            output_dir=root/'notes',api_key='fixture',model='fixture',base_url='https://api.deepseek.com')
        with patch.object(KnowledgeService,'_worker'):
            self.chat=ChatService(self.settings.database_path,lambda:self.settings,Mock())
        self.addCleanup(self.chat.close)
        self.service=self.chat.knowledge
        self.cid=self.chat.new()['conversation_id']
        self.chat._save(self.cid,{'role':'user','content':'遍历有限图时为什么要标记访问过的顶点？'})
        self.chat._save(self.cid,{'role':'assistant','content':'访问集合可避免沿环重复处理。'})
        self.points=[{'title':title,'content':body,'tags':['图'],'message_ids':[2],'source_urls':[]}
                     for title,body in [('访问集合','记录已处理的顶点。'),('处理环','遇到已访问顶点时跳过。')]]

    def work(self):
        job_id=self.service.pending.get_nowait()
        try:self.service._process(job_id)
        finally:self.service.pending.task_done()
        return self.service.job(job_id)

    def crash_after_publication(self):
        self.chat.archive(self.cid,True)
        original=self.service._update
        def update(job_id,**fields):
            if fields.get('status')=='completed':
                raise OSError('合成故障：文件已落盘，完成记录未提交')
            original(job_id,**fields)
        with patch.object(self.service,'_extract',return_value=self.points),patch.object(self.service,'_update',side_effect=update):
            failed=self.work()
        self.assertEqual(failed['status'],'failed')
        self.assertTrue(self.chat.select(self.cid)['archived_at'])
        doc,=self.settings.output_dir.rglob('*.md')
        return failed['id'],doc

    def test_archive_without_extraction_keeps_chat_and_creates_no_document(self):
        with patch.object(self.service,'request') as request:
            self.chat.archive(self.cid,False)
        request.assert_not_called()
        self.assertEqual(len(self.chat.unarchive(self.cid)['messages']),2)
        self.assertFalse(list(self.settings.output_dir.rglob('*.md')))

    def test_retry_reuses_published_document_without_rewriting_it(self):
        job,doc=self.crash_after_publication()
        inode=doc.stat().st_ino
        self.service.retry(job)
        with patch.object(self.service,'_extract') as extract:result=self.work()
        extract.assert_not_called()
        self.assertEqual(result['status'],'completed')
        self.assertEqual(doc.stat().st_ino,inode)
        self.assertEqual(self.service.store.count(),2)
        self.assertTrue(all(p['content'] in doc.read_text() for p in self.points))
        self.assertEqual(list(self.settings.output_dir.rglob('*.md')),[doc])

    def test_retry_preserves_user_edits_after_interruption(self):
        job,doc=self.crash_after_publication()
        doc.write_text('用户编辑后保存的版本')
        self.service.retry(job)
        with patch.object(self.service,'_extract') as extract:result=self.work()
        extract.assert_not_called()
        self.assertEqual(doc.read_text(),'用户编辑后保存的版本')
        self.assertEqual(result['status'],'failed')
        self.assertIn('已有',result['error'])
        self.assertEqual(self.service.store.count(),2)

    def test_archive_with_tasks_uses_durable_message_ids_without_nested_guard(self):
        import threading
        class Guard:
            def __init__(self): self.lock=threading.Lock()
            def __enter__(self):
                if not self.lock.acquire(timeout=.5):raise AssertionError('对话锁重复进入')
            def __exit__(self,*args):self.lock.release()
        self.chat._guard=Guard()
        task={'conversation_id':self.cid,'title':'合成论文','stage':'已完成','capability':'paper_summary','created_at':1}
        self.chat.task_provider=lambda:[{**task,'id':'done','status':'completed','markdown':'持久化的任务结果'},
                                        {**task,'id':'pending','status':'queued','stage':'等待处理'}]
        with patch.object(self.service,'request') as queue:
            payload=self.chat.archive(self.cid,True)
        snapshot=queue.call_args.args[0]
        self.assertEqual(len(snapshot['messages']),3)
        self.assertTrue(all(type(m['id']) is int for m in snapshot['messages']))
        self.assertIn('持久化的任务结果',snapshot['messages'][-1]['content'])
        self.assertTrue(any(m['id']=='task:pending' for m in payload['conversation']['messages']))
