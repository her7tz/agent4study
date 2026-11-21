'use strict';
(() => {
  const $ = id => document.getElementById(id);
  let csrf, session, rows=[], ticket, busy=false, operation=0, historySupport={};
  const say = text => { $('notice').textContent=text; };
  const showHistory = text => { $('history-state').textContent = text; };
  const historyUnchecked = '历史读取尚未验证。请选择会话和日期。';
  const clear = () => { rows=[]; ticket=null; $('material').hidden=true; $('message').replaceChildren(); $('reply').value=''; $('report').textContent=''; $('source').textContent=''; showReferences([]); };
  function resetConnection() {
    if ($('confirmBox').open) closeDialog();
    clear();
    session=null;
    $('unread-controls').hidden=true;
    historySupport={};
    $('peer').replaceChildren();
    $('peer-availability').textContent='';
    $('history-support').textContent='';
    $('account').textContent='';
    showHistory('');
    $('workspace').hidden=true;
    $('stop').hidden=true;
  }
  async function call(action,body={}) {
    const requestOperation=operation;
    if(action==='connect' || !csrf) {
      const response=await fetch('/api/status',{cache:'no-store'});
      const status=await response.json().catch(()=>({}));
      if(!response.ok || !status.csrf_token) throw Error('无法连接本地工作台，请确认服务正在运行。');
      csrf=status.csrf_token;
    }
    const res=await fetch('/api/qq/'+action,{method:'POST',headers:{'Content-Type':'application/json','X-Agent-Token':csrf},body:JSON.stringify({peer:$('peer').value,session,...body})});
    const data=await res.json().catch(()=>({}));
    if(requestOperation!==operation) throw Error('本次操作已结束。');
    if(res.status===403 || data.code==='qq_session_changed') {
      csrf=null;
      resetConnection();
      throw Error(res.status===403?'工作台会话已失效，请重新连接本机 QQ。':data.error);
    }
    if(!res.ok) throw Error(data.error || '请求失败');
    return data;
  }
  function setBusy(value, canDisconnect=false) {
    busy=value;
    document.querySelectorAll('button, select, input, textarea').forEach(control=>{control.disabled=value;});
    if(canDisconnect) $('stop').disabled=false;
  }
  function run(id,fn) {
    $(id).onclick=async()=>{if(busy)return;const currentOperation=++operation;setBusy(true,['read','read-unread','summary','suggest'].includes(id));
      try{await fn();}catch(e){if(currentOperation===operation)say(e.message);}
      finally{if(currentOperation===operation)setBusy(false);}};
  }
  function current(){const row=rows.find(x=>x.id===$('message').value); if(!row) throw Error('请先选择消息。');return row;}
  const formatTime = stamp => new Date(stamp*1000).toLocaleString();
  const actualRange = ({first, last}) => first === null ? '未取得范围内的文字或直接 @'
    : `实际取得的首末消息 ${formatTime(first)} 至 ${formatTime(last)}`;
  const messageLabel = row => `消息 #${row.id} · ${formatTime(row.time)} · 发送者 ${row.sender}`
    + (row.sequence ? ` · 平台序号 ${row.sequence}` : '');
  const messageText = row => (row.mentions_me ? '@我 ' : '') + row.text;
  function display(){const row=current();$('source').textContent=messageLabel(row)+'\n'+messageText(row);$('reply').value='';}
  function showReferences(ids) {
    const list = $('qq-reference-list');
    list.replaceChildren();
    const messages = new Map(rows.map(row => [row.id, row]));
    const sources = new Map();
    for (const id of ids) {
      const row = messages.get(id);
      if (!row) continue;
      const details = document.createElement('details');
      details.id = `qq-reference-${sources.size + 1}`;
      sources.set(id, details);
      const title = document.createElement('summary');
      const source = document.createElement('pre');
      title.textContent = messageLabel(row);
      source.textContent = messageText(row);
      details.append(title, source);
      list.append(details);
    }
    $('qq-references').hidden = !list.childElementCount;
    return sources;
  }
  function referenceLink(id, source) {
    const link = document.createElement('a');
    link.href = '#' + source.id;
    link.textContent = '#' + id;
    link.addEventListener('click', event => {
      event.preventDefault();
      source.open = true;
      const title = source.querySelector('summary');
      title.scrollIntoView({block:'nearest'});
      title.focus();
    });
    return link;
  }
  function showSummary(report, preface, {text, sections, mentions, scope}, sources) {
    report.textContent = preface.join('\n');
    if (!Array.isArray(sections) || !Array.isArray(mentions)) {
      report.append('\n', text);
      return;
    }
    const result = document.createDocumentFragment();
    const coverage = document.createElement('p');
    coverage.textContent = scope;
    result.append(coverage);
    const groups = [...sections, {
      label:'提及我的消息', items:mentions.length ? [{text:'直接 @', ids:mentions}] : [],
      emptyText:'所读文字中未发现直接 @。',
    }];
    for (const {label, items, emptyText='未提取到明确事项。'} of groups) {
      const heading = document.createElement('h3');
      heading.textContent = label;
      const list = document.createElement('ul');
      for (const {text, ids} of items) {
        const item = document.createElement('li');
        item.append(text + ' ');
        ids.filter(id => sources.has(id)).forEach((id, index) => {
          if (index) item.append('、');
          item.append(referenceLink(id, sources.get(id)));
        });
        list.append(item);
      }
      if (!list.childElementCount) {
        const empty = document.createElement('li');
        empty.textContent = emptyText;
        list.append(empty);
      }
      result.append(heading, list);
    }
    report.append(result);
  }
  function closeDialog(){ticket=null;$('confirmBox').close();$('confirmText').textContent='';}
  function localTime(t){return new Date(t-new Date(t).getTimezoneOffset()*60000).toISOString().slice(0,16);}
  $('start').value=localTime(Date.now()-86400000);$('end').value=localTime(Date.now());
  $('bridge-kind').onchange=()=>{ $('port').value=$('bridge-kind').value==='icqq'?'5700':'3000'; };
  $('test-mode').onchange=()=>{ $('test-fields').hidden=!$('test-mode').checked; };
  run('connect',async()=>{resetConnection();
    const testMode = $('test-mode').checked;
    const testId = $('test-peer').value.trim();
    const account = $('expected-account').value.trim();
    if (testMode && (!/^[0-9]{1,20}$/.test(testId) || !/^[0-9]+$/.test(account))) {
      throw Error('只读联调需要填写预期 QQ 号和有效的测试会话编号。');
    }
    const data=await call('connect',{
      port:Number($('port').value),token:$('secret').value,expected_account:account,
      bridge_kind:$('bridge-kind').value,
      test_peer:testMode ? $('test-kind').value + ':' + testId : '',
    });
    $('secret').value='';session=data.session;
    historySupport=data.history_support ?? {};
    $('page-size').textContent=data.bridge_kind==='icqq'?'每页最多 20 条':'每页最多 100 条';
    $('account').textContent='QQ 登录账号：'+data.account;$('peer').replaceChildren(new Option('请选择会话',''));
    for(const [key,name] of Object.entries(data.peers)) $('peer').add(new Option((key.startsWith('group:')?'群聊':'好友')+' · '+name+' · '+key.split(':')[1],key));
    $('preview').hidden=Boolean(data.read_only);
    if (data.read_only) {
      $('peer').selectedIndex=1;
      $('account').append(' · 只读联调');
    }
    $('peer-availability').textContent = [['friend','好友'],['group','群聊']].map(([kind,label]) => {
      const list = data.peer_lists?.[kind];
      if (!list) return data.read_only ? `${label}未查询` : `${label}列表状态未验证`;
      return list.status === 'available' ? `${label}列表取得 ${list.count} 项` : `${label}列表未能读取`;
    }).join('，') + '。';
    $('history-support').textContent = [['friend','好友'],['group','群聊']].map(([kind,label]) => {
      const supported = historySupport[kind];
      if (supported === true) return `${label}历史接口可用`;
      if (supported === false) return `${label}历史接口未提供`;
      return `${label}历史接口待验证`;
    }).join('，') + '。';
    $('unread-controls').hidden=!$('peer').value.startsWith('group:');
    showHistory(historyUnchecked);
    $('stop').hidden=false;$('workspace').hidden=false;say('已连接，尚未读取消息。');});
  $('stop').onclick=async()=>{
    if(!session)return;
    ++operation;
    setBusy(true);
    try{await call('disconnect');resetConnection();say('已断开。');}
    catch(e){say(e.message);}
    finally{setBusy(false);}
  };
  const selectPeerNotice = '请选择要读取的好友或群聊。';
  const noHistory = '当前桥接未提供此类会话的历史查询接口。';
  const historyUnavailable = () => historySupport[$('peer').value.split(':')[0]] === false;
  function rangeChanged() {
    clear();
    $('unread-controls').hidden=!$('peer').value.startsWith('group:');
    showHistory(historyUnavailable() ? noHistory : historyUnchecked);
    say($('peer').value ? '阅读范围已更改，请重新读取所选会话。' : selectPeerNotice);
  }
  $('peer').onchange=rangeChanged;$('start').oninput=rangeChanged;$('end').oninput=rangeChanged;
  async function readMessages(action, bounds={}) {
    const unread = action === 'read-unread';
    clear();
    say(unread ? '正在读取平台未读快照…' : '正在读取所选会话…');
    showHistory(unread ? '正在核对 QQ 平台未读范围。' : '正在查询所选会话的历史。');
    const currentOperation = operation;
    let data;
    try {
      await call('select');
      data = await call(action,bounds);
    } catch (error) {
      if (currentOperation === operation && session) showHistory(unread ? '本次未能验证平台未读范围。' : '本次未能读取历史，请查看错误提示。');
      throw error;
    }
    rows=data.messages;for(const r of rows)$('message').add(new Option('#'+r.id+' · '+r.sender+' · '+r.text.slice(0,55),r.id));
    showHistory(unread ? unreadRange(data.coverage) : rows.length ? '已取得所选范围的历史消息。' : '本次未取得范围内的可用消息。');
    if(rows.length){$('material').hidden=false;display();}say(`收到 ${rows.length} 条。${actualRange(data.coverage)}。${data.scope}`);
  }
  function unreadRange(coverage) {
    return coverage.unread_count
      ? `平台未读 ${coverage.unread_count} 条，首末序号 ${coverage.first_unread_seq} 至 ${coverage.last_unread_seq}`
      : 'QQ 平台报告该群聊无未读消息';
  }
  run('read',async()=>{
    if (!$('peer').value) throw Error(selectPeerNotice);
    if (historyUnavailable()) throw Error(noHistory);
    const from = Date.parse($('start').value)/1000;
    const to = Date.parse($('end').value)/1000+59;
    if (!Number.isInteger(from) || !Number.isInteger(to) || from < 0 || from > to) {
      throw Error('请选择有效的起止时间。');
    }
    await readMessages('read',{from,to});
  });
  run('read-unread',async()=>{
    if (!$('peer').value.startsWith('group:')) throw Error('请选择要读取未读消息的群聊。');
    await readMessages('read-unread');
  });
  $('message').onchange=display;
  run('summary',async()=>{
    const result = await call('summarize');
    const {references, coverage} = result;
    const kind = coverage.peer.startsWith('group:') ? '群聊' : '好友';
    const peerId = coverage.peer.split(':')[1];
    showSummary($('report'), [
      `${kind} ${coverage.name}（${peerId}）`,
      coverage.source==='qq_platform' ? unreadRange(coverage)
        : `请求范围 ${formatTime(coverage.from)} 至 ${formatTime(coverage.to)}`,
      actualRange(coverage),
    ], result, showReferences(references));
  });
  run('suggest',async()=>{$('reply').value=(await call('suggest',{message_id:current().id})).text;});
  run('preview',async()=>{const d=await call('preview',{message_id:current().id,text:$('reply').value});ticket=d.id;
    $('confirmText').textContent='发送账号：'+d.account+'\n发送到：'+d.name+' ('+d.peer+')\n针对消息 #'+d.message_id+'\n\n'+d.text;
    $('confirmBox').showModal();});
  run('cancel',async()=>{const id=ticket;closeDialog();await call('cancel',{ticket:id});say('已取消，未发送。');});
  $('confirmBox').oncancel=e=>{e.preventDefault();$('cancel').click();};
  run('send',async()=>{const id=ticket;closeDialog();try{const r=await call('confirm',{ticket:id});say(r.notice||'桥接已确认提交，消息编号 '+r.message_id);}
    catch(e){say(session ? e.message+' 请先在 QQ 中核对，未自动重发。' : e.message);}});
})();
