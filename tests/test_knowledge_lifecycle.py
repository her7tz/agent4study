"""Local knowledge edit, retrieval and conversation lifecycle contracts."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from conversation import ChatService
from knowledge import KnowledgeError
from knowledge_context import retrieve_context


class KnowledgeLifecycle(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        self.settings = SimpleNamespace(data_home=root, output_dir=root/'summaries',
            database_path=root/'harness.sqlite3', api_key='fixture', model='fixture',
            base_url='https://api.deepseek.com', max_parallel_tasks=4)
        self.chat = ChatService(self.settings.database_path, lambda: self.settings, Mock())
        self.addCleanup(self.chat.close)
        self.store = self.chat.knowledge.store
        self.conversation_id = self.chat.current()['conversation_id']
        self.snapshot = {'conversation_id': self.conversation_id, 'title': '递归学习',
            'messages': [{'id': 1, 'role': 'assistant', 'content': '递归需要终止条件。'}]}
        self.point = {'title': '递归终止条件', 'content': '递归必须有终止条件。',
                      'tags': ['递归'], 'message_ids': [1], 'source_urls': []}
        self.identifier = self.store.ingest('batch-1', self.snapshot, [self.point])['entry_ids'][0]

    def test_revision_conflict_preserves_current_content_and_original_archive(self):
        entry = self.store.get(self.identifier)
        changed = self.store.update(self.identifier, entry['title'], '递归必须逐步接近终止条件。', entry['tags'], entry['revision'])
        self.assertEqual(changed['revision'], entry['revision'] + 1)
        with self.assertRaises(KnowledgeError) as raised:
            self.store.update(self.identifier, entry['title'], '过期编辑', [], entry['revision'])
        self.assertEqual(raised.exception.status, 409)
        self.assertEqual(self.store.get(self.identifier)['content'], changed['content'])
        self.assertEqual(self.store.document('batch-1')['points'][0]['content'], self.point['content'])
        with self.store.connect() as db:
            prior = json.loads(db.execute('SELECT payload FROM knowledge_revisions WHERE entry_id=?', (self.identifier,)).fetchone()[0])
        self.assertEqual(prior['content'], self.point['content'])

    def test_soft_delete_excludes_context_and_restore_returns_same_entry(self):
        self.assertEqual(retrieve_context(self.store, '递归终止条件', [])[0]['entry_id'], self.identifier)
        self.store.delete(self.identifier)
        self.assertEqual(retrieve_context(self.store, '递归终止条件', []), [])
        self.assertEqual(self.store.search('递归', deleted=True)[0]['id'], self.identifier)
        self.store.restore(self.identifier)
        self.assertEqual(retrieve_context(self.store, '递归终止条件', [])[0]['entry_id'], self.identifier)

    def test_exact_duplicates_add_sources_and_conflicting_content_stays_separate(self):
        second = {**self.snapshot, 'conversation_id': 'other', 'title': '第二个来源'}
        linked = self.store.ingest('batch-2', second, [self.point])
        self.assertEqual(linked['entry_ids'], [self.identifier])
        self.assertEqual(len(self.store.get(self.identifier)['sources']), 2)
        other = self.store.ingest('batch-3', second, [{**self.point, 'content': '这是一条保留待核对的不同说法。'}])
        self.assertNotEqual(other['entry_ids'], [self.identifier])
        self.assertEqual(self.store.count(), 2)

    def test_followup_keeps_source_context_and_respects_budget(self):
        history = [{'role': 'user', 'content': '递归终止条件是什么？'}]
        values = retrieve_context(self.store, '进一步展开', history)
        self.assertEqual(values[0]['entry_id'], self.identifier)
        self.assertEqual(values[0]['sources'][0]['conversation_id'], self.conversation_id)
        size = len(json.dumps(values, ensure_ascii=False))
        self.assertEqual(retrieve_context(self.store, '递归终止条件', [], budget=size), values)
        self.assertEqual(retrieve_context(self.store, '递归终止条件', [], budget=size - 1), [])

    def test_short_new_topic_does_not_retrieve_previous_private_topic(self):
        history = [{'role': 'user', 'content': '递归终止条件是什么？'}]
        for query in ('快速排序', '为什么快速排序不稳定？', '你好'):
            with self.subTest(query=query):
                self.assertEqual(retrieve_context(self.store, query, history), [])

    def test_followup_stays_with_most_recent_topic_across_followup_chain(self):
        sort = {**self.point, 'title': '快速排序分区', 'content': '分区按基准值划分元素。', 'tags': ['快速排序']}
        sort_id, = self.store.ingest('batch-sort', self.snapshot, [sort])['entry_ids']
        history = [{'role': 'user', 'content': text} for text in
                   ('递归终止条件是什么？', '解释快速排序分区', '进一步展开', '继续')]
        values = retrieve_context(self.store, '继续', history)
        self.assertEqual([item['entry_id'] for item in values], [sort_id])
        no_match = history[:1] + [{'role': 'user', 'content': '解释操作系统的页表'}]
        self.assertEqual(retrieve_context(self.store, '刚才那个呢？', no_match), [])

    def test_explicit_topic_overrides_followup_prefix_and_previous_sources(self):
        sort = {**self.point, 'title': '快速排序分区', 'content': '分区按基准值划分元素。', 'tags': ['快速排序']}
        sort_id, = self.store.ingest('batch-sort', self.snapshot, [sort])['entry_ids']
        history = [{'role':'user', 'content':'解释递归终止条件。'}]
        switched = '这部分递归终止条件先放一放，换个话题：快速排序分区。'
        for query in (switched, '继续讲快速排序分区。', '接着解释快速排序分区。',
                      '递归终止条件，New topic: 快速排序分区。'):
            with self.subTest(query=query):
                self.assertEqual([item['entry_id'] for item in retrieve_context(self.store, query, history)], [sort_id])
        self.assertEqual(retrieve_context(self.store, '继续讲操作系统的页表。', history), [])
        after_switch = history + [{'role':'user', 'content':switched}]
        self.assertEqual([item['entry_id'] for item in retrieve_context(self.store, '再展开一点', after_switch)], [sort_id])
        self.assertEqual([item['entry_id'] for item in retrieve_context(self.store, '继续讲一下', history)], [self.identifier])

    def test_topic_reset_without_subject_does_not_restore_the_old_source(self):
        history = [{'role':'user','content':'递归终止条件是什么？'}]
        reset = '刚才的递归终止条件先放一放，换个话题。'
        self.assertEqual(retrieve_context(self.store, reset, history), [])
        self.assertEqual(retrieve_context(self.store, '刚才那个呢？', history + [{'role':'user','content':reset}]), [])

    def test_named_topic_after_referential_prefix_controls_current_and_next_turn(self):
        sort = {**self.point, 'title': '快速排序分区', 'content': '分区按基准值划分元素。', 'tags': ['快速排序']}
        sort_id, = self.store.ingest('batch-sort', self.snapshot, [sort])['entry_ids']
        history = [{'role': 'user', 'content': '递归终止条件是什么？'}]
        for query in ('那么快速排序分区呢？', '那怎么实现快速排序分区？', '这个快速排序分区有什么作用？',
                      '刚才提到的快速排序分区怎么实现？'):
            with self.subTest(query=query):
                self.assertEqual([item['entry_id'] for item in retrieve_context(self.store, query, history)], [sort_id])
                followup = history + [{'role': 'user', 'content': query}]
                self.assertEqual([item['entry_id'] for item in retrieve_context(self.store, '为什么？', followup)], [sort_id])
        for query in ('那么操作系统页表呢？', '这个页表有什么作用？'):
            with self.subTest(query=query):
                self.assertEqual(retrieve_context(self.store, query, history), [])
        for query in ('这个怎么用？', '那怎么实现？', '刚才那个呢？', '它有什么作用？', '这部分能举个例子吗？'):
            with self.subTest(query=query):
                self.assertEqual([item['entry_id'] for item in retrieve_context(self.store, query, history)], [self.identifier])

    def test_deleting_conversation_does_not_delete_knowledge_or_evidence(self):
        self.chat.delete(self.conversation_id)
        entry = self.store.get(self.identifier)
        self.assertIsNone(entry['deleted_at'])
        self.assertEqual(entry['sources'][0]['evidence'][0]['content'], self.snapshot['messages'][0]['content'])
        self.assertEqual(self.store.document('batch-1')['points'], [self.point])

    def test_main_model_receives_bounded_knowledge_as_quoted_background(self):
        client = SimpleNamespace(settings=self.settings, timeout=55)
        captured = []
        def answer(messages, **kwargs):
            captured.extend(messages)
            return {'role': 'assistant', 'content': '递归需要终止条件。'}
        client.stream_chat = answer
        question = '请解释递归终止条件'
        with patch('conversation.create_client', return_value=client):
            self.chat.send(question, web_enabled=False)
        self.assertIn('个人知识是引用背景', captured[0]['content'])
        self.assertNotIn(self.identifier, captured[0]['content'])
        self.assertNotIn(self.point['content'], captured[0]['content'])
        payload = json.loads(captured[-1]['content'])
        self.assertEqual((captured[-1]['role'], payload['user_message']), ('user', question))
        self.assertEqual(payload['personal_knowledge'][0]['entry_id'], self.identifier)
        self.assertEqual(payload['personal_knowledge'][0]['content'], self.point['content'])
        self.assertEqual(payload['personal_knowledge'][0]['sources'][0]['conversation_id'], self.conversation_id)
        stored = self.chat._payload(self.conversation_id)['messages']
        self.assertEqual(next(item['content'] for item in stored if item['role'] == 'user'), question)

        captured.clear()
        with patch('conversation.create_client', return_value=client):
            self.chat.send('快速排序', conversation_id=self.conversation_id, web_enabled=False)
        self.assertEqual(captured[-1]['content'], '快速排序')
        self.assertFalse(any(self.identifier in item.get('content', '') for item in captured))


if __name__ == '__main__':
    unittest.main()
