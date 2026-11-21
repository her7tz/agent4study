"""Long-page continuation and retry preserve actual reading coverage."""
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from conversation import ChatService, ConversationError
from web_research import PageRangeError, PublicFetchError, WebSources

URL = 'https://example.org/long-article'
BODY = 'A' * 1000 + 'B' * 1000 + 'Final correction applies only under condition C.' * 20


class WebReadRanges(unittest.TestCase):
    def read(self, web, start, size=1000, text=BODY):
        with patch('web_research.fetch_public', return_value=(URL, '<main>'+text+'</main>', 'text/html')):
            return web.read(URL, size, start)

    def test_overlapping_slices_continue_to_tail_without_counting_twice(self):
        web = WebSources()
        first = self.read(web, 0)
        self.assertEqual((first['start_char'], first['end_char'], first['next_start']), (0, 1000, 1000))
        overlap = self.read(web, 500)
        self.assertEqual(overlap['content'], BODY[500:1500])
        self.assertEqual(web.metadata()[0]['read_chars'], 1500)
        tail = self.read(web, 1500, 24000)
        self.assertIn('Final correction', tail['content'])
        self.assertIsNone(tail['next_start'])
        meta = web.metadata()[0]
        self.assertEqual(meta['read_ranges'], [[0, len(BODY)]])
        self.assertEqual(meta['read_chars'], len(BODY))
        self.assertFalse(meta['truncated'])
        self.assertEqual(web.scope_note(), '')

    def test_changed_page_resets_coverage_even_if_length_is_equal(self):
        web = WebSources()
        self.read(web, 0, text='A'*2000)
        self.read(web, 1000, text='B'*2000)
        self.assertEqual(web.metadata()[0]['read_ranges'], [[1000, 2000]])
        self.assertEqual(web.metadata()[0]['read_chars'], 1000)
        self.assertTrue(web.metadata()[0]['truncated'])

    def test_a_successful_prefix_reread_does_not_clear_failed_continuations(self):
        web = WebSources()
        self.read(web, 0)
        for start in (1000, 2000):
            with patch('web_research.fetch_public', side_effect=PublicFetchError(503)), self.assertRaises(PublicFetchError):
                web.read(URL + '#section', 1000, start)
        self.read(web, 0)
        failures = [item for item in web.metadata() if item['kind'] == 'read_failed']
        self.assertEqual([item['start_char'] for item in failures], [1000, 2000])
        self.assertIn('2 次正文续读未成功', web.scope_note())
        self.read(web, 1000)
        self.assertEqual([item['start_char'] for item in web.metadata() if item['kind'] == 'read_failed'], [2000])
        self.assertEqual(web.metadata()[0]['read_ranges'], [[0, 2000]])
        self.read(web, 2000)
        self.assertEqual([item['kind'] for item in web.metadata()], ['page'])
        self.assertEqual(web.scope_note(), '')

    def test_invalid_offsets_do_not_report_site_failure(self):
        web = WebSources()
        for value in (-1, True, '1'):
            with self.subTest(value=value), patch('web_research.fetch_public') as fetch:
                with self.assertRaises(PageRangeError):
                    web.read(URL, start_char=value)
                fetch.assert_not_called()
        with self.assertRaisesRegex(PageRangeError, '超出范围'):
            self.read(web, len(BODY))
        self.assertEqual(web.metadata(), [])

    def test_legacy_page_result_keeps_its_original_scope(self):
        web = WebSources()
        web.record_page({'kind':'page', 'url':URL, 'content':'old excerpt', 'truncated':True})
        self.assertIn('只读取了节选', web.scope_note())
        self.assertNotIn('read_ranges', web.metadata()[0])

    def test_page_links_resolve_from_final_url_without_claiming_they_were_read(self):
        web = WebSources()
        page = ('<aside><a href="/outside">Outside</a></aside><div role="main">' + '正文'*100 +
                '<a href="chapter.html#example">Coroutines <em>and tasks</em></a>'
                '<a href="chapter.html#second">Duplicate</a><a href="#local">Local</a>'
                '<a href="javascript:alert(1)">Script</a><a href="mailto:x@example.org">Mail</a>'
                '<a href="https://user:password@example.org/page">Credential</a></div>')
        final = 'https://example.org/docs/index.html'
        with patch('web_research.fetch_public', return_value=(final, page, 'text/html')):
            result = web.read(URL)
        self.assertEqual(result['links'], [{'url':'https://example.org/docs/chapter.html',
                                          'title':'Coroutines and tasks'}])
        self.assertEqual([source['url'] for source in web.metadata()], [final])

    def test_navigation_links_are_bounded_and_absent_for_plain_text(self):
        web = WebSources()
        page = '<main>' + 'Content '*40 + ''.join(
            '<a href="/page-{}">Page {}</a>'.format(i,i) for i in range(100)) + '</main>'
        with patch('web_research.fetch_public', return_value=(URL, page, 'text/html')):
            self.assertEqual(len(web.read(URL)['links']), 80)
        with patch('web_research.fetch_public', return_value=(URL, BODY, 'text/plain')):
            self.assertEqual(web.read(URL)['links'], [])

    def test_main_agent_retry_restores_coverage_without_network_replay(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            settings = SimpleNamespace(data_home=root, output_dir=root/'notes', database_path=root/'chat.db',
                api_key='fixture', model='fixture', base_url='https://api.deepseek.com', max_parallel_tasks=2)
            chat = ChatService(settings.database_path, lambda: settings, Mock())
            model = Mock()
            model.last_reasoning = ''
            model.timeout = 90
            calls = [{'id': 'read-'+str(start), 'function': {'name':'read_webpage',
                     'arguments':json.dumps({'url':URL+'#chapter', 'max_chars':1000, 'start_char':start})}}
                     for start in (0, 1000)]
            failed = {'id':'failed-alias', 'function':{'name':'read_webpage', 'arguments':json.dumps({'url':URL+'#earlier'})}}
            failed_tail = {'id': 'tail-busy', 'function': {'name': 'read_webpage',
                'arguments': json.dumps({'url': URL, 'start_char': 2000})}}
            model.stream_chat.side_effect = [{'role':'assistant', 'tool_calls':[failed, *calls, failed_tail]},
                                            RuntimeError('model response interrupted')]
            page = (URL, '<main>' + 'A' * 3000 + '</main>', 'text/html')
            try:
                with patch('conversation.create_client', return_value=model), patch('web_research.fetch_public',
                        side_effect=[PublicFetchError(403), page, page, PublicFetchError(503)]):
                    with self.assertRaises(ConversationError):
                        chat.send('请读完两段正文')
                model.stream_chat.side_effect = [
                    {'role':'assistant', 'tool_calls':[{**calls[0], 'id':'disabled-read'}]},
                    {'role':'assistant', 'content':'已核对之前取得的两段材料。'}]
                with patch('conversation.create_client', return_value=model), patch('web_research.fetch_public') as fetch:
                    run = chat.start(retry=True, web_enabled=False)
                    deadline = time.monotonic()+4
                    while time.monotonic() < deadline:
                        status = chat.run(run['run_id'])['run']['status']
                        if status not in {'queued','running','stopping'}:
                            break
                        time.sleep(.005)
                    self.assertEqual(status, 'completed')
                    fetch.assert_not_called()
                    repeated = model.stream_chat.call_args.args[0][-1]
                    self.assertIn('未注册的工具', json.loads(repeated['content'])['error'])
                reply = chat.current()['messages'][-1]
                source = next(action for action in reply['actions'] if action.get('kind')=='page')
                self.assertEqual((source['read_chars'], source['read_ranges']), (2000, [[0,2000]]))
                self.assertTrue(source['truncated'])
                failures = [action for action in reply['actions'] if action.get('kind') == 'read_failed']
                self.assertEqual([(failure['start_char'], failure['reason']) for failure in failures], [(2000, 'HTTP 503')])
                self.assertIn('1 次正文续读未成功', reply['content'])
            finally:
                chat.close()

    def test_main_agent_retry_keeps_scope_after_search_provider_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            settings = SimpleNamespace(data_home=root, output_dir=root/'notes', database_path=root/'chat.db',
                api_key='fixture', model='fixture', base_url='https://api.deepseek.com', max_parallel_tasks=2)
            chat = ChatService(settings.database_path, lambda: settings, Mock())
            model = Mock()
            model.last_reasoning = ''
            model.stream_chat.side_effect = [{'role':'assistant', 'tool_calls':[{
                'id':'failed-search', 'function':{'name':'search_web', 'arguments':'{"query":"网页研究"}'}}]},
                RuntimeError('model interrupted')]
            try:
                with patch('conversation.create_client', return_value=model), \
                     patch('web_research.fetch_public', side_effect=TimeoutError):
                    with self.assertRaises(ConversationError):
                        chat.send('请搜索网页研究')
                model.stream_chat.side_effect = None
                model.stream_chat.return_value = {'role':'assistant', 'content':'这是重试后的回答。'}
                with patch('conversation.create_client', return_value=model), patch('web_research.fetch_public') as fetch:
                    run = chat.start(retry=True)
                    deadline = time.monotonic() + 4
                    while time.monotonic() < deadline:
                        status = chat.run(run['run_id'])['run']['status']
                        if status not in {'queued', 'running', 'stopping'}:
                            break
                        time.sleep(.005)
                    self.assertEqual(status, 'completed')
                    fetch.assert_not_called()
                self.assertIn('没有取得网页正文', chat.current()['messages'][-1]['content'])
            finally:
                chat.close()
