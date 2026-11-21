"""Bounded knowledge retrieval with follow-up continuity and source pointers."""
import json
import re


_REFERENTIAL = re.compile(
    r"(?:这个|这点|这部分|刚才|上面|前面|那|它们|它)"
    r"(?:这个|那个|么|个|的|该|就|怎么|如何|为什么|能|可以|有|什么|作用|区别|条件|限制|"
    r"实现|应用|使用|解释|说明|展开|举个例子|举例|做|用|讲|说|再|详细|一点|一下|呢|吗|[，,。？！!?\s]){0,12}"
)
_CONTINUATION = re.compile(
    r"(?:再)?(?:继续|进一步|展开|接着)"
    r"(?:讲|说|解释|介绍|展开|分析|说明|一点|一下|详细|举个例子|举例|这部分|这点|这个|刚才|上面|呢|[，,。？！!?\s]){0,8}"
)
_TOPIC_SWITCH = re.compile(
    r"(?:^|[\r\n，,。.!！？?；;：:])\s*(?:(?:那我们|我们|现在)?(?:换(?:个|一个)?(?:话题|主题)|新话题)"
    r"|(?:let['’]s\s+)?switch topics(?:\s+to\b|(?=\s*(?:$|[,.:;!?])))|new topic(?=\s*(?:$|[,.:;!?])))",
    re.IGNORECASE,
)
_BARE_FOLLOWUP = re.compile(r"(?:为什么|怎么做|怎么用|如何应用|如何实现|举个例子|还有什么|还有吗)[吗呢呀]?|continue|go on|tell me more|why|how so|can you elaborate")
_ACKNOWLEDGMENTS = {"你好", "您好", "谢谢", "多谢", "感谢", "好的", "收到", "好", "ok", "thanks", "hello", "hi"}


def _normalized(text):
    return text.strip().strip("，,。？！!? ").casefold()


def _retrieval_topic(text):
    """Return a topic, an explicit reset (empty), or a turn without its own topic."""
    switches = list(_TOPIC_SWITCH.finditer(text))
    if switches:
        return text[switches[-1].end():].strip("，,。.!！？?；;：: \t\r\n")
    normalized = _normalized(text)
    if (not normalized or normalized in _ACKNOWLEDGMENTS or _REFERENTIAL.fullmatch(normalized)
            or _CONTINUATION.fullmatch(normalized) or _BARE_FOLLOWUP.fullmatch(normalized)):
        return None
    return text.strip()


def retrieve_context(store, message, history, budget=9000):
    current = message.strip()
    normalized = _normalized(current)
    if not normalized or normalized in _ACKNOWLEDGMENTS:
        return []
    query = _retrieval_topic(current)
    if query is None:
        recent = [item.get('content', '') for item in history if item.get('role') == 'user'][-5:]
        if recent and recent[-1].strip() == current:
            recent.pop()
        for text in reversed(recent[-4:]):
            query = _retrieval_topic(text)
            if query is not None:
                break
    if not query:
        return []
    result = []
    used = 2  # The surrounding JSON array brackets.
    for item in store.search(query[:500], limit=8):
        if item['score'] < 5:
            continue
        entry=store.get(item['id'])
        if entry.get('deleted_at'):continue
        value={'entry_id':entry['id'],'title':entry['title'],'revision':entry['revision'],'updated_at':entry['updated_at'],
            'content':entry['content'][:2400], 'sources':[{'title':s['title'],
                'source_type':s.get('source_type','conversation'),
                'source_url':s.get('source_url',''),
                'conversation_id':s['conversation_id'] if s.get('source_type','conversation')=='conversation' else None}
                for s in entry['sources'][:3]]}
        size = len(json.dumps(value, ensure_ascii=False)) + (2 if result else 0)
        if used+size>budget:continue
        result.append(value);used+=size
        if len(result)>=6:break
    return result
