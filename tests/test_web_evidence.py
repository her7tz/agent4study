import http.client
import json
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from conversation import ChatService
from runtime_control import CancellationToken, RunCancelled
from web_research import IncompletePublicResponse, PublicFetchError, WebSources, fetch_public

URL='https://example.org/lesson'


class WebEvidence(unittest.TestCase):
    def read(self, page, max_chars=16000):
        with patch('web_research.fetch_public',return_value=(URL,page,'text/html')):
            return self.web.read(URL,max_chars)

    def setUp(self):
        self.web=WebSources()

    def test_empty_main_with_long_sidebar_is_not_body_evidence(self):
        with self.assertRaisesRegex(RuntimeError,'正文太少'):
            self.read('<main></main><aside>'+'促销'*400+'</aside>')
        self.assertEqual(self.web.metadata()[0]['kind'],'read_failed')
        self.assertIn('没有取得网页正文',self.web.scope_note())

    def test_short_genuine_article_excludes_template_content(self):
        body='一段公开课程正文。'*16
        page='<aside>'+'导航'*300+'</aside><article>'+body+'</article>'
        self.assertEqual(self.read(page)['content'],body)

    def test_interstitial_failure_is_cleared_after_successful_retry(self):
        with self.assertRaisesRegex(RuntimeError,'访问验证'):
            self.read('<title>Just a moment...</title><p>Enable JavaScript and cookies to continue</p>'+' . '*200)
        content='保护网页的 CAPTCHA 机制。'*100
        self.read('<title>关于访问验证</title><article>'+content+'</article>',1000)
        meta=self.web.metadata()
        self.assertEqual(len(meta),1)
        self.assertEqual((meta[0]['kind'],meta[0]['read_chars'],meta[0]['available_chars']),('page',1000,len(content)))
        self.assertIn('只读取了节选',self.web.scope_note())

    def test_cancel_does_not_claim_site_failed(self):
        with patch('web_research.fetch_public',side_effect=RunCancelled('已停止')):
            with self.assertRaises(RunCancelled):self.web.read(URL)
        self.assertEqual(self.web.metadata(),[])

    def socket_response(self, chunked=False):
        reader, writer = socket.socketpair()
        self.addCleanup(reader.close)
        self.addCleanup(writer.close)
        reader.settimeout(1)
        connection = http.client.HTTPConnection('example.org')
        connection.sock = reader
        body = b'<article>' + b'partial page ' * 50 + b'</article>'
        if chunked:
            header = b'Transfer-Encoding: chunked'
            body = ('%x\r\n' % len(body)).encode() + body + b'\r\n'
        else:
            header = b'Content-Length: 2000'
        writer.sendall(b'HTTP/1.1 200 OK\r\nConnection: close\r\nContent-Type: text/html\r\n'
                       + header + b'\r\n\r\n' + body)
        return connection, writer

    def test_premature_close_does_not_become_a_page_source(self):
        for chunked in (False, True):
            with self.subTest(chunked=chunked):
                connection, writer = self.socket_response(chunked)
                writer.shutdown(socket.SHUT_WR)
                web = WebSources()
                with patch('web_research._addresses', return_value=['1.1.1.1']), \
                     patch('web_research._PinnedConnection', return_value=connection), \
                     self.assertRaises(IncompletePublicResponse):
                    web.read(URL)
                self.assertEqual(web.metadata()[0]['kind'], 'read_failed')
                self.assertEqual(web.metadata()[0]['reason'], '网页响应传输未完成')
                self.assertIn('没有取得网页正文', web.scope_note())

    def test_timeout_and_cancellation_interrupt_detached_http_response(self):
        for cancel in (False, True):
            with self.subTest(cancel=cancel):
                connection, _ = self.socket_response()
                token = CancellationToken()
                if cancel:
                    timer = threading.Timer(.04, token.cancel)
                    self.addCleanup(timer.cancel)
                    timer.start()
                expected = RunCancelled if cancel else TimeoutError
                with patch('web_research._addresses', return_value=['1.1.1.1']), \
                     patch('web_research._PinnedConnection', return_value=connection), \
                     self.assertRaises(expected):
                    fetch_public(URL, token, timeout=.12)
                self.assertIsNone(connection.sock)

    def test_explicit_search_provider_can_bypass_irrelevant_primary_results(self):
        html = '<a class="result__a" href="https://docs.python.org/3/library/asyncio-task.html">TaskGroup</a>'
        with patch('web_research.fetch_public', return_value=('https://html.duckduckgo.com/html/', html, 'text/html')) as fetch:
            result = self.web.search('TaskGroup asyncio', provider='duckduckgo')
        self.assertEqual(fetch.call_count, 1)
        self.assertTrue(fetch.call_args.args[0].startswith('https://html.duckduckgo.com/'))
        self.assertEqual(result['results'][0]['provider'], 'DuckDuckGo')
        self.assertEqual(result['results'][0]['url'], 'https://docs.python.org/3/library/asyncio-task.html')

    def test_auto_search_falls_back_but_explicit_failure_and_cancel_do_not(self):
        html = '<a class="result__a" href="https://example.org/article">Article</a>'
        with patch('web_research.fetch_public', side_effect=[PublicFetchError(503), ('https://html.duckduckgo.com/', html, 'text/html')]) as fetch:
            self.assertEqual(self.web.search('Article')['results'][0]['provider'], 'DuckDuckGo')
            self.assertEqual(fetch.call_count, 2)
        for error in (TimeoutError('timed out'), RunCancelled('已停止')):
            with self.subTest(error=type(error).__name__), patch('web_research.fetch_public', side_effect=error) as fetch:
                with self.assertRaises(type(error) if isinstance(error, RunCancelled) else RuntimeError):
                    self.web.search('Article', provider='duckduckgo')
                self.assertEqual(fetch.call_count, 1)

    def test_constrained_search_falls_back_if_primary_only_returns_noise(self):
        bing = ('<rss><channel><item><title>ASE community</title><link>https://ase-lib.org/</link></item>'
                '<item><title>ASE conference</title><link>https://researchr.org.example/ase</link></item>'
                '<item><title>Baseline conference</title><link>https://conf.researchr.org/base</link></item></channel></rss>')
        link = 'https://conf.researchr.org/home/ase-2026'
        duck = (f'<a class="result__a" href="{link}">ASE</a>'
                f'<a class="result__snippet" href="{link}">IEEE/ACM <b>Conference</b> site</a>')
        with patch('web_research.fetch_public', side_effect=[('', bing, 'application/rss+xml'), ('', duck, 'text/html')]) as fetch:
            result = self.web.search('ASE', site='researchr.org', required_terms=['ASE', 'Conference'])
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual([entry['status'] for entry in result['attempts']], ['no_match', 'matched'])
        self.assertEqual([entry['url'] for entry in result['results']], [link])
        self.assertEqual([entry['url'] for entry in self.web.metadata()], [link])

    def test_constrained_search_matches_path_before_applying_result_limit(self):
        xml = ('<rss><channel><item><title>General docs</title><link>https://docs.python.org/3/</link></item>'
               '<item><title>Broken result</title><link>https://[malformed</link></item>'
               '<item><title>Coroutines and Tasks</title><link>https://docs.python.org/3/library/asyncio-task.html</link></item></channel></rss>')
        with patch('web_research.fetch_public', return_value=('', xml, 'application/rss+xml')) as fetch:
            result = self.web.search('TaskGroup', limit=1, site='docs.python.org', required_terms=['Asyncio'])
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(result['results'][0]['url'], 'https://docs.python.org/3/library/asyncio-task.html')

    def test_constrained_search_reports_no_match_and_failed_fallback_separately(self):
        xml = '<rss><channel><item><title>Only docs</title><link>https://docs.qq.com/</link></item></channel></rss>'
        with patch('web_research.fetch_public', side_effect=[('', xml, 'application/rss+xml'), TimeoutError('fixture timeout')]):
            result = self.web.search('official docs', site='docs.python.org')
        self.assertEqual(result['results'], [])
        self.assertEqual([entry['status'] for entry in result['attempts']], ['no_match', 'error'])
        self.assertIn('timeout', result['attempts'][1]['error'])
        self.assertEqual(self.web.metadata(), [])
        self.assertIn('没有取得网页正文', self.web.scope_note())

    def test_search_provider_schema_is_available_through_main_agent_tool(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            settings = SimpleNamespace(data_home=root, output_dir=root/'notes', database_path=root/'chat.db',
                api_key='fixture', model='fixture', base_url='https://api.deepseek.com', max_parallel_tasks=2)
            chat = ChatService(settings.database_path, lambda: settings, Mock())
            web = Mock()
            try:
                registry = chat._registry(web, True)
                registry.call('search_web', {'query': 'TaskGroup', 'provider': 'duckduckgo', 'site': 'docs.python.org',
                                             'required_terms': ['asyncio']}, [])
                web.search.assert_called_once_with('TaskGroup', 6, 'duckduckgo', site='docs.python.org', required_terms=['asyncio'])
                with self.assertRaises(ValueError):
                    registry.call('search_web', {'query': 'TaskGroup', 'provider': 'unknown'}, [])
                self.assertEqual(web.search.call_count, 1)
            finally:
                chat.close()

    def test_main_reply_exposes_failed_read_even_if_model_omits_it(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            settings=SimpleNamespace(data_home=root,output_dir=root/'notes',database_path=root/'chat.db',
                api_key='fixture',model='fixture',base_url='https://api.deepseek.com',max_parallel_tasks=2)
            chat=ChatService(settings.database_path,lambda:settings,Mock())
            model=Mock()
            model.stream_chat.side_effect=[{'role':'assistant','content':'','tool_calls':[{'id':'read1',
                'function':{'name':'read_webpage','arguments':'{"url":"https://example.org/lesson#details"}'}}]},
                {'role':'assistant','content':'这是模型回答。'}]
            try:
                with patch('conversation.create_client',return_value=model),patch('web_research.fetch_public',side_effect=PublicFetchError(403)):
                    res=chat.send('查阅课程说明')
                reply=res['messages'][-1]
                self.assertIn('没有取得网页正文',reply['content'])
                source=next(a for a in reply['actions'] if a['type']=='source')
                self.assertEqual((source['kind'],source['url'],source['reason']),('read_failed',URL,'HTTP 403'))
                stored = [json.loads(row['message']) for row in chat._rows(res['conversation_id'])]
                tool = next(item for item in stored if item['role'] == 'tool')
                self.assertEqual(json.loads(tool['content'])['source_failure']['url'], URL)
            finally:chat.close()
