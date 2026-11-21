"""Cursor paging and observed coverage using synthetic OneBot records."""
import unittest
from unittest.mock import Mock
from qq_channel import QQError
from qq_history import read_window


def row(n, group=9, time=None):
    return {'time': n if time is None else time, 'message_id': -n, 'message_seq':987654,
            'self_id':123, 'user_id':55, 'message_type':'group','group_id':group,
            'message':[{'type':'text','data':{'text':str(n)}}]}


class QQPaging(unittest.TestCase):
    def run_window(self,pages, start=0,end=9000,check=None):
        self.bridge=Mock()
        self.bridge.call.side_effect=[{'messages':page} if isinstance(page,list) else page for page in pages]
        return read_window(self.bridge,check or Mock(),QQError,'123','group','9',start,end)

    def test_older_selected_date_and_overlapping_boundary(self):
        latest=[row(i) for i in range(101,201)]
        older=[row(i) for i in range(2,102)]
        messages,scope=self.run_window([latest,older],start=5,end=130)
        self.assertEqual(len(messages),126)
        self.assertEqual(next(iter(messages)), '-5')
        self.assertIn('已读到所选起点之前',scope)
        self.assertEqual(self.bridge.call.call_args.kwargs['message_seq'],'-101')

    def test_foreign_rows_do_not_choose_cursor_or_claim_date_coverage(self):
        page=[row(i) for i in range(100,199)] + [row(1,group=99)]
        messages,scope=self.run_window([page,[]],start=4)
        self.assertEqual(len(messages),99)
        self.assertNotIn('已读到所选起点之前',scope)
        self.assertEqual(self.bridge.call.call_args.kwargs['message_seq'],'-100')

    def test_short_pages_can_still_contain_more_selected_history(self):
        latest = [row(10), row(9)]
        older = [row(9), row(8)]
        boundary = [row(8), row(7), row(6)]
        messages, scope = self.run_window([latest, older, boundary], start=7)
        self.assertEqual(list(messages), ['-7', '-8', '-9', '-10'])
        self.assertIn('已读取 3 页', scope)
        self.assertIn('已读到所选起点之前', scope)
        calls = [call.kwargs for call in self.bridge.call.call_args_list]
        self.assertEqual([call.get('message_seq') for call in calls], [None, '-9', '-8'])
        for call in calls:
            self.assertTrue(call['disable_get_url'])
            self.assertFalse(call['parse_mult_msg'])

    def test_private_history_can_page_past_unaddressed_own_messages(self):
        own = {'self_id': 123, 'user_id': 123, 'time': 12, 'message_id': -12, 'real_seq': '112',
               'message_type': 'private', 'sub_type': 'friend', 'post_type': 'message_sent',
               'message': '无法核对归属的私聊内容'}
        earlier_own = dict(own, time=11, message_id=-11, real_seq='111')
        received = {'self_id': 123, 'user_id': 55, 'time': 10, 'message_id': -10,
                    'message_type': 'private', 'sub_type': 'friend', 'post_type': 'message',
                    'message': '实际收到的消息'}
        other_peer = dict(received, user_id=66, message_id=-8, message='别的好友')
        explicit_target = dict(earlier_own, target_id=55, time=9, message_id=-9, message='确认收件人的回复')
        before_start = dict(received, time=7, message_id=-7)
        bridge = Mock()
        bridge.call.side_effect = [{'messages': [own, earlier_own]},
                                  {'messages': [earlier_own, received, other_peer, explicit_target, before_start]}]

        messages, scope = read_window(bridge, Mock(), QQError, '123', 'friend', '55', 8, 20)

        self.assertEqual(list(messages), ['-9', '-10'])
        self.assertEqual(bridge.call.call_args.kwargs['message_seq'], '-11')
        self.assertEqual(bridge.call.call_args.kwargs['user_id'], 55)
        self.assertIn('2 条本账号发出的私聊记录缺少收件人', scope)
        self.assertIn('已读到所选起点之前', scope)

        bridge = Mock()
        bridge.call.side_effect = [{'messages': [own, earlier_own]}, {'messages': []}]
        messages, scope = read_window(bridge, Mock(), QQError, '123', 'friend', '55', 15, 20)
        self.assertEqual(messages, {})
        self.assertEqual(bridge.call.call_count, 2)
        self.assertNotIn('已读到所选起点之前', scope)
        self.assertNotIn('缺少收件人', scope)

    def test_equal_second_uses_bridge_sequence_to_page_and_order_messages(self):
        latest = [dict(row(i, time=10), real_seq=str(i + 160)) for i in (44, 43, 42)]
        middle = [latest[-1], dict(row(41, time=10), real_seq='201')]
        older = [middle[-1], dict(row(40, time=9), real_seq='200'), row(39, time=8)]
        pages = {None: latest, '-42': middle, '-41': older}
        bridge = Mock()
        bridge.call.side_effect = lambda _action, **params: {'messages': pages.get(params.get('message_seq'), latest)}

        messages, scope = read_window(bridge, Mock(), QQError, '123', 'group', '9', 9, 15)

        self.assertEqual(list(messages), ['-40', '-41', '-42', '-43', '-44'])
        self.assertEqual([call.kwargs.get('message_seq') for call in bridge.call.call_args_list],
                         [None, '-42', '-41'])
        self.assertIn('已读到所选起点之前', scope)

    def test_stalled_page_and_rejected_cursor_are_partial(self):
        page=[row(i) for i in range(1,101)]
        for second,note in [(page,'没有继续向前'),(QQError('拒绝'),'部分')]:
            with self.subTest(note=note):
                messages,scope=self.run_window([page,second])
                self.assertEqual(len(messages),100)
                self.assertIn(note,scope)

    def test_cq_strings_keep_text_and_only_real_direct_mentions(self):
        page = [
            dict(row(10), message='甲&amp;乙&#91;x&#93; [CQ:image,file=private.png,url=https://ignored.invalid]结束'),
            dict(row(11), message='&#91;CQ:at,qq=123&#93; &amp;#91;'),
            dict(row(12), message='[CQ:at,qq=123,name=合成&#44;用户]'),
            dict(row(13), message='[CQ:at,qq=all][CQ:at,qq=55]'),
            dict(row(14, group=99), message='另一个群的文字'),
        ]
        messages, _ = self.run_window([page, []])
        self.assertEqual(list(messages), ['-10', '-11', '-12'])
        self.assertEqual(messages['-10']['text'], '甲&乙[x] 结束')
        self.assertEqual(messages['-11']['text'], '[CQ:at,qq=123] &#91;')
        self.assertFalse(messages['-11']['mentions_me'])
        self.assertEqual((messages['-12']['text'], messages['-12']['mentions_me']), ('', True))

    def test_malformed_older_page_preserves_only_validated_pages(self):
        for malformed in ({'messages': 'invalid'}, [row(i) for i in range(101)]):
            with self.subTest(malformed=type(malformed).__name__):
                messages, scope = self.run_window([[row(20)], malformed])
                self.assertEqual(list(messages), ['-20'])
                self.assertIn('已读取 1 页', scope)
                self.assertIn('仅展示已取得的部分', scope)
                with self.assertRaises(QQError) as failed:
                    self.run_window([malformed])
                self.assertEqual(failed.exception.status, 502)

    def test_account_switch_aborts_without_publishing_first_page(self):
        check=Mock(side_effect=[None,None,QQError('账号变化')])
        with self.assertRaises(QQError):self.run_window([[row(i) for i in range(1,101)]],check=check)
        self.assertEqual(self.bridge.call.call_count,1)

    def test_page_budget_without_guessing_unread(self):
        pages=[[row(i) for i in range(100*(10-n),100*(11-n))] for n in range(10)]
        messages,scope=self.run_window(pages)
        self.assertEqual(len(messages),1000)
        self.assertIn('达到本次读取上限',scope)
        self.assertIn('不代表 QQ 平台未读数',scope)
