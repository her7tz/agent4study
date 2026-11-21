"""Validate a platform unread snapshot supplied by a local bridge extension."""

import re
import time

from qq_history import _belongs, _history_sequence, _message_text


UNREAD_ACTION = 'get_workbench_group_unread'
UNREAD_UNSUPPORTED_STATUS = 501
PAGE_SIZE = 100
MAX_PAGES = 10


def _identifier(value):
    return isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9._~=-]{1,128}', value)


def _sequence_field(data, field):
    value = data.get(field)
    return _history_sequence(data, field) if isinstance(value, str) else None


def read_group_unread(bridge, verify, error_type, account, group):
    def incomplete():
        return error_type('桥接未能证明本次 QQ 未读范围完整，请重新读取。', 502)

    snapshot = None
    cursor = None
    seen_cursors, seen_ids = set(), set()
    messages = {}
    previous_sequence = None
    deadline = time.monotonic() + 30
    for page in range(MAX_PAGES):
        if time.monotonic() >= deadline:
            break
        verify()
        request = {'self_id': account, 'group_id': group, 'count': PAGE_SIZE}
        if cursor is not None:
            request.update(snapshot_id=snapshot[0], cursor=cursor)
        try:
            data = bridge.call(UNREAD_ACTION, **request)
        except error_type as error:
            verify()
            if page == 0:
                if error.status == UNREAD_UNSUPPORTED_STATUS:
                    raise
                raise error_type('无法取得 QQ 平台未读快照。请核对桥接是否提供未读扩展、账号和接口状态。', 502) from None
            raise incomplete() from None
        verify()
        if time.monotonic() >= deadline:
            break
        if (not isinstance(data, dict) or type(data.get('version')) is not int
                or data['version'] != 1 or data.get('source') != 'qq_platform'):
            raise incomplete()
        if data.get('self_id') != account or data.get('group_id') != group:
            raise incomplete()
        snapshot_id, count = data.get('snapshot_id'), data.get('unread_count')
        first, last = data.get('first_unread_seq'), data.get('last_unread_seq')
        first_position = _sequence_field(data, 'first_unread_seq')
        last_position = _sequence_field(data, 'last_unread_seq')
        current = snapshot_id, count, first, last
        if (not _identifier(snapshot_id) or type(count) is not int or not 0 <= count <= PAGE_SIZE * MAX_PAGES
                or not {'first_unread_seq', 'last_unread_seq'} <= data.keys()):
            raise incomplete()
        if (count == 0 and (first is not None or last is not None)) or (
            count > 0 and (first_position is None or last_position is None or first_position > last_position)
        ):
            raise incomplete()
        if snapshot is not None and current != snapshot:
            raise incomplete()
        snapshot = current
        rows, cursor = data.get('messages'), data.get('next_cursor')
        if not isinstance(rows, list) or len(rows) > PAGE_SIZE or 'next_cursor' not in data:
            raise incomplete()
        for row in rows:
            if not _belongs(row, 'group', group, account) or row.get('self_id') != account:
                raise incomplete()
            mid, stamp, content = row.get('message_id'), row.get('time'), row.get('message')
            sequence = _history_sequence(row, 'message_seq')
            if (not _identifier(mid) or mid in seen_ids or count == 0 or sequence is None
                    or not first_position <= sequence <= last_position or type(stamp) is not int or stamp < 0
                    or not isinstance(content, (str, list))):
                raise incomplete()
            if (previous_sequence is None and sequence != first_position) or (
                previous_sequence is not None and sequence <= previous_sequence
            ):
                raise incomplete()
            previous_sequence = sequence
            seen_ids.add(mid)
            text, mentions = _message_text(content, account)
            if text or mentions:
                messages[mid] = {'id': mid, 'sender': str(row['user_id']), 'time': stamp,
                                 'text': text, 'mentions_me': mentions, 'sequence': str(sequence)}
        if cursor is None:
            if len(seen_ids) != count or (count and previous_sequence != last_position):
                raise incomplete()
            return messages, {'source': 'qq_platform', 'unread_count': count,
                              'first_unread_seq': first, 'last_unread_seq': last}
        if not _identifier(cursor) or cursor in seen_cursors or not rows or len(seen_ids) >= count:
            raise incomplete()
        seen_cursors.add(cursor)
    raise incomplete()
