import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest.mock import Mock, patch
from qq_channel import QQChannel, QQError, QQBridge

class QQBoundary(unittest.TestCase):
    def setUp(self):
        self.sent=[]
        self.account='123'
        self.unknown=False
        self.rows=[
            {'message_type':'group','group_id':9,'user_id':22,'message_id':1,'time':55,'message':[
                {'type':'text','data':{'text':'讨论一道题'}},{'type':'at','data':{'qq':'123'}}]},
            {'message_type':'group','group_id':10,'user_id':4,'message_id':2,'time':55,'message':[{'type':'text','data':{'text':'别的群'}}]}]
        def call(action,**args):
            if action=='get_login_info':return {'user_id':self.account}
            if action=='get_friend_list':return [{'user_id':22,'nickname':'同学'}]
            if action=='get_group_list':return [{'group_id':9,'group_name':'学习群'}]
            if action=='get_group_msg_history':return {'messages': self.rows + self.rows}
            if action=='get_friend_msg_history':return {'messages':self.rows}
            if action.startswith('send_'):
                self.sent.append((action,args))
                if self.unknown:raise QQError('连接中断')
                return {'message_id':88}
            raise AssertionError(action)
        self.bridge=Mock();self.bridge.call.side_effect=call
        self.q=QQChannel(lambda:object(),lambda *_:self.bridge)
        self.session=self.q.handle('connect',{})['session']
        self.go('select')

    def go(self,action,**kw):
        return self.q.handle(action,{'peer':'group:9','session':self.session,**kw})

    def read(self):return self.go('read',**{'from':0,'to':100})['messages']

    def test_connection_keeps_available_list_when_other_list_fails(self):
        original = self.bridge.call.side_effect
        cases = [('get_friend_list', QQError('好友列表不可用', 502), 'group:9', 'friend'),
                 ('get_group_list', {'malformed': True}, 'friend:22', 'group')]
        for action, failure, peer, absent_kind in cases:
            with self.subTest(action=action):
                def list_response(name, **params):
                    if name == action:
                        if isinstance(failure, QQError):
                            raise failure
                        return failure
                    return original(name, **params)

                self.bridge.call.side_effect = list_response
                connected = self.q.handle('connect', {})
                self.assertEqual(set(connected['peers']), {peer})
                self.assertEqual(connected['peer_lists'][absent_kind]['status'], 'unavailable')
                self.session = connected['session']
                self.assertEqual(self.go('select', peer=peer), {'selected': peer})
                if peer == 'group:9':
                    self.assertEqual([item['id'] for item in self.read()], ['1'])

    def test_connection_fails_when_neither_list_is_available(self):
        original = self.bridge.call.side_effect
        self.bridge.call.side_effect = lambda action, **params: (
            None if action.endswith('_list') else original(action, **params))
        with self.assertRaisesRegex(QQError, '未能读取好友或群聊列表'):
            self.q.handle('connect', {})
        self.assertIsNone(self.q.session)
        self.assertEqual(self.q.peers, {})

    def test_account_change_during_partial_discovery_discards_all_peers(self):
        original = self.bridge.call.side_effect

        def switched_account(action, **params):
            if action == 'get_friend_list':
                self.account = '444'
                raise QQError('好友列表不可用', 502)
            return original(action, **params)

        self.bridge.call.side_effect = switched_account
        with self.assertRaisesRegex(QQError, '登录身份已变化'):
            self.q.handle('connect', {})
        self.assertIsNone(self.q.session)
        self.assertEqual(self.q.peers, {})

    def preview(self):
        self.read();return self.go('preview',message_id='1',text='好的 [CQ:at,qq=all]')

    def test_scope_time_dedup_mention(self):
        result = self.go('read', **{'from': 0, 'to': 100})
        self.assertEqual([row['id'] for row in result['messages']], ['1'])
        self.assertTrue(result['messages'][0]['mentions_me'])
        self.assertEqual(result['coverage'], {'peer': 'group:9', 'name': '学习群', 'from': 0, 'to': 100,
                                              'first': 55, 'last': 55})
        empty = self.go('read', **{'from': 60, 'to': 100})
        self.assertEqual(empty['messages'], [])
        self.assertIsNone(empty['coverage']['first'])
        self.assertIsNone(empty['coverage']['last'])
        self.go('select',peer='friend:22')
        with self.assertRaises(QQError):self.read()

    def test_one_confirm_exact_target_and_plain_text(self):
        t=self.preview();self.assertFalse(self.sent)
        self.assertEqual(self.go('confirm',ticket=t['id'],text='替换')['status'],'confirmed')
        self.assertEqual(self.sent,[('send_group_msg',{'group_id':9,'message':[{'type':'text','data':{'text':t['text']}}]})])
        with self.assertRaises(QQError):self.go('confirm',ticket=t['id'])
        self.assertEqual(len(self.sent),1)

    def test_failed_unread_discards_previous_confirmation_and_scope(self):
        ticket = self.preview()
        call = self.bridge.call.side_effect

        def fail_unread(action, **params):
            if action == 'get_workbench_group_unread':
                raise QQError('临时错误', 502)
            return call(action, **params)

        self.bridge.call.side_effect = fail_unread
        with self.assertRaisesRegex(QQError, '无法取得 QQ 平台未读快照'):
            self.go('read-unread')
        self.assertEqual(self.q.messages, {})
        self.assertIsNone(self.q.scope)
        self.assertIsNone(self.q.coverage)
        with self.assertRaisesRegex(QQError, '确认已失效'):
            self.go('confirm', ticket=ticket['id'])
        self.assertFalse(self.sent)

    def test_unknown_is_not_replayed(self):
        t=self.preview();self.unknown=True
        self.assertEqual(self.go('confirm',ticket=t['id'])['status'],'unknown')
        with self.assertRaises(QQError):self.go('confirm',ticket=t['id'])
        self.assertEqual(len(self.sent),1)

    def test_cancel_disconnect_identity_change_and_stale_session(self):
        t=self.preview();self.go('cancel',ticket=t['id'])
        with self.assertRaises(QQError):self.go('confirm',ticket=t['id'])
        t=self.preview();self.account='222'
        with self.assertRaises(QQError):self.go('confirm',ticket=t['id'])
        self.assertFalse(self.sent)
        self.q.handle('connect',{})
        with self.assertRaises(QQError):self.go('select')
        self.session=self.q.session;self.go('disconnect')
        with self.assertRaises(QQError):self.read()

    def test_old_preview_cancellation_does_not_invalidate_new_preview(self):
        previous = self.preview()
        current = self.go('preview', message_id='1', text='第二份正文')
        with self.assertRaisesRegex(QQError, '确认已失效'):
            self.go('cancel', ticket=previous['id'])
        self.assertEqual(self.sent, [])
        self.assertEqual(self.go('confirm', ticket=current['id'])['status'], 'confirmed')
        self.assertEqual(self.sent[0][1]['message'], [{'type': 'text', 'data': {'text': '第二份正文'}}])

    def test_malformed_receipt_stays_unknown_and_cannot_be_replayed(self):
        original = self.bridge.call.side_effect
        for message_id in (True, 3.5, {'id': 88}, 'ok'):
            with self.subTest(message_id=message_id):
                def call(action, **params):
                    response = original(action, **params)
                    return {'message_id': message_id} if action.startswith('send_') else response

                self.bridge.call.side_effect = call
                ticket = self.preview()
                before = len(self.sent)
                self.assertEqual(self.go('confirm', ticket=ticket['id'])['status'], 'unknown')
                with self.assertRaises(QQError):
                    self.go('confirm', ticket=ticket['id'])
                self.assertEqual(len(self.sent), before+1)

    def test_disconnect_stops_paging_without_waiting_for_inflight_response(self):
        first_page, release = Event(), Event()
        original = self.bridge.call.side_effect
        requested = []

        def bridge_call(action, **params):
            if action == 'get_group_msg_history':
                requested.append(params)
                first_page.set()
                if not release.wait(5):
                    raise QQError('fixture timeout')
                return {'messages': [{**self.rows[0], 'time': index, 'message_id': index}
                                     for index in range(1, 101)]}
            return original(action, **params)

        self.bridge.call.side_effect = bridge_call
        with ThreadPoolExecutor(2) as workers:
            reading = workers.submit(self.read)
            try:
                self.assertTrue(first_page.wait(3))
                stopped = workers.submit(self.go, 'disconnect')
                self.assertEqual(stopped.result(timeout=2), {'connected': False})
                self.assertFalse(reading.done())
            finally:
                release.set()
            with self.assertRaises(QQError):
                reading.result(timeout=3)
        self.assertEqual(len(requested), 1)
        self.assertEqual(self.q.messages, {})

    def test_summary_and_suggestion_do_not_have_tools(self):
        reading = self.go('read', **{'from': 0, 'to': 100})
        model = Mock()
        model.json_chat.return_value = {
            'summary': [{'text': '讨论一道题', 'ids': ['1', '1']}],
            'todos': [{'text': '下次继续讨论', 'ids': ['1']}],
        }
        model.chat.return_value = {'content': '基于 #1 的合成摘要'}
        with patch('agent_profiles.create_client',return_value=model),patch('agent_profiles.resolve_agent'):
            result = self.go('summarize')
            self.assertIn('#1', result['text'])
            self.assertEqual(result['references'], ['1'])
            self.assertEqual(result['sections'], [
                {'label': '摘要', 'items': [{'text': '讨论一道题', 'ids': ['1']}]},
                {'label': '待办', 'items': [{'text': '下次继续讨论', 'ids': ['1']}]},
            ])
            self.assertEqual(result['mentions'], ['1'])
            self.assertEqual(result['coverage'], reading['coverage'])
            self.go('suggest',message_id='1')
            model.json_chat.return_value={'summary':[], 'todos':[{'text':'假的依据','ids':['2']}]}
            with self.assertRaises(QQError):self.go('summarize')
        for args,kwargs in model.chat.call_args_list:
            self.assertEqual(kwargs,{})
            self.assertIn('讨论一道题',args[0][1]['content'])
            self.assertNotIn('别的群',args[0][1]['content'])
        self.assertFalse(self.sent)

    def test_disconnect_during_summary_rejects_late_model_result(self):
        self.read()
        started, release = Event(), Event()

        def summarize(*args):
            started.set()
            if not release.wait(5):
                raise AssertionError('model response was not released')
            return {'summary': [{'text': '旧会话摘要', 'ids': ['1']}], 'todos': []}

        model = Mock()
        model.json_chat.side_effect = summarize
        with patch('agent_profiles.create_client', return_value=model), patch('agent_profiles.resolve_agent'), ThreadPoolExecutor(2) as workers:
            pending = workers.submit(self.go, 'summarize')
            try:
                self.assertTrue(started.wait(2))
                stopped = workers.submit(self.go, 'disconnect')
                self.assertEqual(stopped.result(timeout=1), {'connected': False})
                self.assertFalse(pending.done())
            finally:
                release.set()
            with self.assertRaises(QQError) as error:
                pending.result(timeout=2)
            self.assertTrue(error.exception.reconnect)
        self.assertEqual(self.q.messages, {})
        self.assertFalse(self.sent)

    def test_switching_conversation_discards_late_suggested_reply(self):
        self.read()
        started, release = Event(), Event()

        def suggest(*args):
            started.set()
            if not release.wait(5):
                raise AssertionError('model response was not released')
            return {'content': '旧会话草稿'}

        model = Mock()
        model.chat.side_effect = suggest
        with patch('agent_profiles.create_client', return_value=model), patch('agent_profiles.resolve_agent'), ThreadPoolExecutor(2) as workers:
            pending = workers.submit(self.go, 'suggest', message_id='1')
            try:
                self.assertTrue(started.wait(2))
                selected = workers.submit(self.go, 'select', peer='friend:22')
                self.assertEqual(selected.result(timeout=1), {'selected': 'friend:22'})
                self.assertFalse(pending.done())
            finally:
                release.set()
            with self.assertRaisesRegex(QQError, '范围已变化'):
                pending.result(timeout=2)
        self.assertFalse(self.sent)

    def test_qq_client_account_switch_discards_summary_and_draft(self):
        def switch_account(answer):
            def respond(*args, **kwargs):
                self.account = '222'
                return answer
            return respond

        model = Mock()
        model.json_chat.side_effect = switch_account({'summary': [{'text': '旧账号摘要', 'ids': ['1']}], 'todos': []})
        model.chat.side_effect = switch_account({'content': '旧账号回复草稿'})
        with patch('agent_profiles.create_client', return_value=model), patch('agent_profiles.resolve_agent'):
            for action, parameters in [('summarize', {}), ('suggest', {'message_id': '1'})]:
                with self.subTest(action=action):
                    self.account = '123'
                    self.session = self.q.handle('connect', {})['session']
                    self.go('select')
                    self.read()
                    with self.assertRaisesRegex(QQError, '登录身份已变化') as error:
                        self.go(action, **parameters)
                    self.assertTrue(error.exception.reconnect)
                    self.assertIsNone(self.q.session)
                    self.assertEqual(self.q.messages, {})
        self.assertFalse(self.sent)

    def test_delayed_old_identity_check_leaves_new_connection_intact(self):
        self.read()
        returning, checking, release = Event(), Event(), Event()
        original = self.bridge.call.side_effect

        def call(action, **parameters):
            if action == 'get_login_info' and returning.is_set() and not checking.is_set():
                checking.set()
                if not release.wait(5):
                    raise AssertionError('identity response was not released')
                return {'user_id': '222'}
            return original(action, **parameters)

        def summarize(*args):
            returning.set()
            return {'summary': [{'text': '旧账号摘要', 'ids': ['1']}], 'todos': []}

        self.bridge.call.side_effect = call
        model = Mock()
        model.json_chat.side_effect = summarize
        with patch('agent_profiles.create_client', return_value=model), patch('agent_profiles.resolve_agent'), ThreadPoolExecutor(2) as workers:
            pending = workers.submit(self.go, 'summarize')
            try:
                self.assertTrue(checking.wait(2))
                self.account = '333'
                current = workers.submit(self.q.handle, 'connect', {}).result(timeout=1)
                self.assertEqual(current['account'], '333')
                self.assertFalse(pending.done())
            finally:
                release.set()
            with self.assertRaisesRegex(QQError, '连接已变化'):
                pending.result(timeout=2)
        self.assertEqual(self.q.session, current['session'])
        self.assertEqual(self.q.account, '333')
        self.assertFalse(self.sent)

    def test_bridge_does_not_accept_arbitrary_url_or_blank_token(self):
        with self.assertRaises(QQError):QQBridge('https://example.invalid','token')
        with self.assertRaises(QQError):QQBridge(3000,'')
