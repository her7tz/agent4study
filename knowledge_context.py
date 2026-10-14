"""Bounded knowledge retrieval with follow-up continuity and source pointers."""
import json
import re


def retrieve_context(store, message, history, budget=9000):
    recent=[m.get('content','') for m in history if m.get('role')=='user'][-4:]
    followup=bool(re.search(r'^(继续|进一步|展开|这个|它|那|刚才|上面|为什么|还有|如何应用)',message.strip())) or len(message.strip())<12
    queries=[message[:500]]
    if followup:
        queries.extend(text[:400] for text in reversed(recent) if text!=message)
    scores={}
    for index,query in enumerate(queries):
        for item in store.search(query,limit=8):
            if item['score']<5:continue
            weight=item['score']*(2 if index==0 else 1/(index+1))
            previous=scores.get(item['id'])
            if previous is None or weight>previous[0]:scores[item['id']]=(weight,item)
    result=[];used=0
    for _,item in sorted(scores.values(),key=lambda v:v[0],reverse=True):
        entry=store.get(item['id'])
        if entry.get('deleted_at'):continue
        value={'entry_id':entry['id'],'title':entry['title'],'revision':entry['revision'],'updated_at':entry['updated_at'],
            'content':entry['content'][:2400], 'sources':[{'title':s['title'],'conversation_id':s['conversation_id']} for s in entry['sources'][:3]]}
        size=len(json.dumps(value,ensure_ascii=False))
        if used+size>budget:continue
        result.append(value);used+=size
        if len(result)>=6:break
    return result
