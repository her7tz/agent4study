"""Read only the selected QQ peer. A stopped page window is not an unread count."""
import re
import time


CQ_SEGMENT = re.compile(r'\[CQ:([A-Za-z0-9_]+)(?:,([^\[\]]*))?\]')
HISTORY_ACTIONS = {'friend': 'get_friend_msg_history', 'group': 'get_group_msg_history'}


def _belongs(row, kind, peer, account, *, icqq=False):
    if not isinstance(row, dict) or str(row.get('self_id', account)) != account:
        return False
    sender = str(row.get('user_id', ''))
    if not sender.isdigit():
        return False
    message_type = row.get('message_type')
    if kind == 'group':
        return (message_type == 'group' or (icqq and 'message_type' not in row)) and str(row.get('group_id')) == peer
    if row.get('sub_type') not in (None, 'friend'):
        return False
    if icqq:
        if row.get('group_id') is not None or (message_type != 'private' and 'message_type' in row):
            return False
    elif message_type != 'private':
        return False
    if sender == peer:
        return str(row.get('target_id', account)) == account
    return sender == account and str(row.get('target_id')) == peer


def _unaddressed_outgoing(row, account):
    return (isinstance(row, dict) and row.get('message_type') == 'private'
            and row.get('sub_type') == 'friend' and row.get('post_type') == 'message_sent'
            and str(row.get('self_id')) == str(row.get('user_id')) == account
            and row.get('target_id') is None)


def _history_sequence(row, field):
    value = row.get(field)
    if isinstance(value, str) and len(value) <= 20 and value.isascii() and value.isdecimal():
        value = int(value)
    return value if type(value) is int and value > 0 else None


def _cq_string_text(message, account):
    text = []
    cursor = 0
    mentions = False
    for segment in CQ_SEGMENT.finditer(message):
        text.append(message[cursor:segment.start()])
        cursor = segment.end()
        kind, parameters = segment.groups()
        if kind == 'at' and parameters:
            targets = [field[3:] for field in parameters.split(',') if field.startswith('qq=')]
            mentions = mentions or targets == [account]
    text.append(message[cursor:])
    # Parse tags before unescaping text so a typed CQ example remains literal.
    return ''.join(text).replace('&#91;', '[').replace('&#93;', ']').replace('&amp;', '&'), mentions


def _message_text(message, account):
    if isinstance(message, str):
        return _cq_string_text(message, account)
    if not isinstance(message, list):
        return '', False
    text, mentions = [], False
    for segment in message:
        if not isinstance(segment, dict) or not isinstance(segment.get('data'), dict):
            continue
        data = segment['data']
        if segment.get('type') == 'text' and isinstance(data.get('text'), str):
            text.append(data['text'])
        if segment.get('type') == 'at' and str(data.get('qq')) == account:
            mentions = True
    return ''.join(text), mentions


def read_window(bridge, check_identity, error_type, account, kind, peer, start, end, *, bridge_kind='napcat'):
    action = HISTORY_ACTIONS[kind]
    target = {'group_id' if kind == 'group' else 'user_id': int(peer)}
    icqq = bridge_kind == 'icqq'
    page_size, sequence_field = (20, 'message_seq') if icqq else (100, 'real_seq')
    history_options = {} if icqq else {'disable_get_url': True, 'parse_mult_msg': False}
    messages, seen, used_anchors = {}, set(), set()
    message_sequences = {}
    unaddressed_ids = set()
    cursor, earliest = None, None
    pages = 0
    deadline = time.monotonic() + 30
    reason = '达到本次读取上限，更早消息未核实。'
    for _ in range(10):
        if time.monotonic() >= deadline:
            reason = '达到读取时间上限，更早消息未核实。'
            break
        check_identity()
        try:
            data = bridge.call(action, **target, count=page_size, **history_options,
                               **({'message_seq': cursor} if cursor is not None else {}))
        except error_type:
            if not pages:
                raise
            check_identity()
            reason = '桥接服务未返回下一页，仅展示已取得的部分。'
            break
        check_identity()
        rows = data.get('messages') if isinstance(data,dict) else None
        if not isinstance(rows,list) or len(rows)>page_size:
            if not pages:
                raise error_type('历史消息格式或数量超出本次请求范围。',502)
            reason = f'下一页格式无效或超出每页 {page_size} 条上限，仅展示已取得的部分。'
            break
        pages += 1
        anchors, outgoing_anchors = [], []
        for row in rows:
            belongs = _belongs(row, kind, peer, account, icqq=icqq)
            unaddressed = kind == 'friend' and _unaddressed_outgoing(row, account)
            if not belongs and not unaddressed:
                continue
            stamp, mid = row.get('time'), row.get('message_id')
            if type(stamp) is not int or type(mid) not in (int,str) or not str(mid):
                continue
            mid = str(mid)
            sequence = _history_sequence(row, sequence_field)
            if unaddressed:
                outgoing_anchors.append((stamp, mid, sequence))
                if start <= stamp <= end:
                    unaddressed_ids.add(mid)
                continue
            anchors.append((stamp, mid, sequence))
            if mid in seen: continue
            seen.add(mid)
            if not start<=stamp<=end: continue
            text, mentions = _message_text(row.get('message'), account)
            if text or mentions:
                messages[mid] = {'id':mid,'sender':str(row['user_id']),'time':stamp,
                                 'text':text,'mentions_me':mentions}
                message_sequences[mid] = sequence
        # NapCat's scoped private-history response can omit the recipient on
        # self-sent rows. These can advance the same query, but don't establish
        # message ownership or that the requested date boundary was reached.
        candidates = anchors or outgoing_anchors
        if not candidates:
            reason = '桥接服务未返回更多可定位的本会话消息。'
            break
        if icqq:
            sequences = [item[2] for item in candidates if item[2] is not None]
            if not sequences:
                reason = '桥接未返回可用的分页序号，仅展示已取得的部分。'
                break
            anchor = min(sequences)
            if cursor is not None and anchor >= cursor:
                reason = '分页没有继续向前，已停止重复读取。'
                break
            if any(stamp < start for stamp, _, sequence in anchors if sequence is not None):
                reason = '已读到所选起点之前，已按起止时间筛选。'
                break
            cursor = anchor
            continue
        stamp = min(item[0] for item in candidates)
        boundary = [item for item in candidates if item[0] == stamp]
        # NapCat's raw sequence breaks same-second ties. The paging request
        # still takes the short message ID, which is a different field.
        oldest = min(boundary, key=lambda item: item[2]) if all(item[2] is not None for item in boundary) else boundary[0]
        anchor = oldest[1]
        if anchors and stamp < start:
            reason = '已读到所选起点之前，已按起止时间筛选。'
            break
        if anchor in used_anchors or (anchors and earliest is not None and stamp > earliest):
            reason = '分页没有继续向前，已停止重复读取。'
            break
        used_anchors.add(anchor)
        cursor = anchor
        if anchors:
            earliest = stamp
    scope = '已读取 %d 页。%s 仅含可读取的文字和直接 @；不代表 QQ 平台未读数。' % (pages,reason)
    unaddressed_count = len(unaddressed_ids - seen)
    if unaddressed_count:
        scope += ' 桥接返回的 %d 条本账号发出的私聊记录缺少收件人，未计入结果。' % unaddressed_count
    unsequenced_times = {message['time'] for mid, message in messages.items() if message_sequences[mid] is None}

    def message_order(item):
        mid, message = item
        stamp = message['time']
        return stamp, 0 if stamp in unsequenced_times else message_sequences[mid]

    return dict(sorted(messages.items(), key=message_order)), scope
