import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from conversation import ChatService
from tool_registry import Tool, ToolRegistry


def request(call_id='call-1'):
    return {'role':'assistant','content':'','reasoning_content':'','tool_calls':[
        {'type':'function','id':call_id,'function':{'name':'write_once','arguments':'{"value":1}'}}]}


class ChatRetry(unittest.TestCase):
    def setUp(self):
        directory=tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root=Path(directory.name)
        self.settings=SimpleNamespace(data_home=root, output_dir=root/'notes', database_path=root/'chat.db',
            api_key='fixture',model='fixture',base_url='https://api.deepseek.com',max_parallel_tasks=2)
        self.chat=self.open()
        self.cid=self.chat.new()['conversation_id']
        self.writer=Mock(return_value={'saved':True})
        self.registry=ToolRegistry()
        self.registry.register(Tool('write_once','fixture',{'type':'object','properties':{'value':{'type':'integer'}}},self.writer,'write'))

    def open(self):
        chat=ChatService(self.settings.database_path,lambda:self.settings,Mock())
        self.addCleanup(chat.close)
        return chat

    def drive(self,reply,retry=False):
        client=SimpleNamespace(settings=self.settings,timeout=55,stream_chat=reply)
        with patch('conversation.create_client',return_value=client),patch.object(self.chat,'_registry',return_value=self.registry):
            run=self.chat.start(None if retry else '保存一次',self.cid,retry=retry,web_enabled=False)
            return self.await_run(run['run_id'])

    def await_run(self,runid):
        deadline=time.monotonic()+4
        while time.monotonic()<deadline:
            with self.chat._guard:
                if runid not in self.chat._active:break
            time.sleep(.005)
        result=self.chat.run(runid)
        self.assertNotIn(result['run']['status'],('running','queued','stopping'))
        return result

    def test_retry_preserves_background_delivery_and_does_not_repeat_completed_write(self):
        first=self.drive(Mock(side_effect=[request(),RuntimeError('联调：模型回复中断')]))
        self.assertEqual(first['run']['status'],'failed')
        task={'id':'external-task','conversation_id':self.cid,'status':'completed','stage':'完成',
              'title':'独立的后台成果','capability':'paper_summary','markdown':'后台返回的正文',
              'output_file':'paper_summary/existing.md'}
        self.chat.deliver_task(task)
        delivery=self.chat.select(self.cid)['messages'][-1]
        self.assertEqual(delivery['source_file'], 'paper_summary/existing.md')
        result=self.drive(Mock(side_effect=[request('call-2'),{'role':'assistant','content':'保存结果已核对。'}]),True)
        self.assertEqual(result['run']['status'],'completed')
        self.writer.assert_called_once()
        self.chat.deliver_task(task)
        messages=self.chat.select(self.cid)['messages']
        self.assertEqual([m['id'] for m in messages if '后台返回的正文' in m['content']], [delivery['id']])
        self.assertFalse(any(m['status']=='failed' for m in messages))

    def test_failed_stream_keeps_received_text_and_requires_explicit_retry(self):
        def disconnect(*args, **kwargs):
            kwargs['on_delta']('已经收到的简短文本。')
            raise RuntimeError('连接中断')

        model = Mock(side_effect=disconnect)
        failure = self.drive(model)
        self.assertEqual(failure['run']['status'], 'failed')
        saved = failure['conversation']['messages'][-1]
        self.assertEqual(saved['status'], 'failed')
        self.assertIn('连接中断', saved['content'])
        self.assertIn('已经收到的简短文本。', saved['content'])
        model.assert_called_once()

        self.chat.close()
        self.chat = self.open()
        self.assertEqual(self.chat.select(self.cid)['messages'][-1]['content'], saved['content'])
        retry = Mock(return_value={'role': 'assistant', 'content': '手动重试完成。'})
        completed = self.drive(retry, retry=True)
        self.assertEqual(completed['run']['status'], 'completed')
        retry.assert_called_once()
        self.assertNotIn('已经收到的简短文本。', str(retry.call_args.args[0]))

    def test_restart_unknown_write_is_not_replayed_by_identical_retry(self):
        self.chat._save(self.cid,{'role':'user','content':'保存一次'})
        self.chat._save(self.cid,request())
        with self.chat._connect() as db:
            db.execute("INSERT INTO chat_runs(id,conversation_id,status,stage,created_at) VALUES(?,?,'running','工具执行中',0)",('crashed',self.cid))
        self.chat.close()
        self.chat=self.open()
        self.assertEqual(self.chat.run('crashed')['run']['status'],'interrupted')
        result=self.drive(Mock(side_effect=[request('another'),{'role':'assistant','content':'结果待核对。'}]),True)
        self.assertEqual(result['run']['status'],'completed')
        self.writer.assert_not_called()
        tools=[json.loads(r['message']) for r in self.chat._rows(self.cid)]
        replay=next(x for x in tools if x.get('tool_call_id')=='another')
        self.assertFalse(json.loads(replay['content'])['success'])

    def test_historical_delivery_survives_retry_without_new_origin_marker(self):
        self.chat._save(self.cid,{'role':'user','content':'整理笔记'})
        task={'id':'legacy-task','conversation_id':self.cid,'status':'completed','stage':'完成',
              'title':'旧论文','capability':'paper_summary','output_file':'paper_summary/moved.md'}
        self.chat.deliver_task(task)
        with self.chat._connect() as db:
            db.execute("UPDATE conversation_messages SET origin='legacy' WHERE origin='task_delivery'")
            row=db.execute("SELECT id,message FROM conversation_messages WHERE origin='legacy' AND message LIKE '%source_file%'").fetchone()
            message=json.loads(row['message'])
            message.pop('source_file')
            db.execute('UPDATE conversation_messages SET message=? WHERE id=?',(json.dumps(message),row['id']))
        self.chat.task_lookup=lambda identifier:task if identifier==task['id'] else None
        self.assertEqual(self.chat.select(self.cid)['messages'][-1]['source_file'], 'paper_summary/moved.md')
        result=self.drive(Mock(return_value={'role':'assistant','content':'已经完成。'}),True)
        self.assertEqual(result['run']['status'],'completed')
        self.assertTrue(any(m['content'].startswith('任务已完成：旧论文') for m in result['conversation']['messages']))

    def test_restart_scopes_reused_call_ids_to_the_current_invocation(self):
        self.chat._save(self.cid,{'role':'user','content':'第一项'})
        self.chat._save(self.cid,request())
        self.chat._save(self.cid,{'role':'tool','tool_call_id':'call-1','content':'{"saved":true}'})
        self.chat._save(self.cid,{'role':'user','content':'第二项'})
        self.chat._save(self.cid,request())
        with self.chat._connect() as db:
            db.execute("INSERT INTO chat_runs(id,conversation_id,status,stage,created_at) VALUES('latest',?,'running','执行中',0)",(self.cid,))
        self.chat.close()
        self.chat=self.open()
        replies=[json.loads(m['content']) for row in self.chat._rows(self.cid)
                 for m in [json.loads(row['message'])] if m.get('tool_call_id')=='call-1']
        self.assertEqual(len(replies),2)
        self.assertEqual(replies[-1]['outcome'],'unknown')

    def test_retry_without_new_delegation_keeps_all_subagent_task_cards(self):
        self.chat._save(self.cid,{'role':'user','content':'整理两篇论文'})
        call=request();call['tool_calls'][0]['function']['name']='delegate_subagents'
        self.chat._save(self.cid,call)
        result={'group_id':'g','tasks':[{'id':i,'agent_id':'paper_reading','capability':'paper_summary'} for i in ('a','b')]}
        self.chat._save(self.cid,{'role':'tool','tool_call_id':'call-1','content':json.dumps(result)})
        done=self.drive(Mock(return_value={'role':'assistant','content':'两项任务已提交。'}),True)
        cards=done['conversation']['messages'][-1]['actions']
        self.assertEqual([(c['task_id'],c['group_id']) for c in cards if c['type']=='task'],[('a','g'),('b','g')])

    def test_stop_wins_over_late_text_from_model(self):
        entered,release=threading.Event(),threading.Event()
        def late(*args,**kwargs):
            entered.set()
            if not release.wait(3):raise AssertionError('未释放测试模型')
            return {'role':'assistant','content':'晚到的完成声明'}
        client=SimpleNamespace(settings=self.settings,timeout=55,stream_chat=late)
        with patch('conversation.create_client',return_value=client):
            run=self.chat.start('测试停止',self.cid,web_enabled=False)['run_id']
            try:
                self.assertTrue(entered.wait(2))
                self.chat.stop(run)
            finally:
                release.set()
            result=self.await_run(run)
        self.assertEqual(result['run']['status'],'cancelled')
        self.assertNotIn('晚到的完成声明',[m['content'] for m in result['conversation']['messages']])
