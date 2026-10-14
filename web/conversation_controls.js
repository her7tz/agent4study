/* Private credentials stay in these same-origin controls, outside message history. */
window.AgentControls = (() => {
  const $ = id => document.getElementById(id);
  const escape = value => String(value ?? '').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  let api, state, refresh, toast, role='global', agents=[], available=[];
  let borrowed=null, placeholder=null;
  function mount(title) {
    restore(); $('chat-control-panel').hidden=false; $('chat-panel-title').textContent=title;
    $('chat-panel-body').replaceChildren();
  }
  function restore() {
    if(borrowed&&placeholder){borrowed.hidden=true;placeholder.replaceWith(borrowed);borrowed=null;placeholder=null;}
  }
  function close() { restore();$('chat-control-panel').hidden=true;$('chat-panel-body').replaceChildren(); }
  async function profiles(selected='global') {
    const data=await api('/api/agent-profiles');agents=data.agents||[];available=data.available_skills||[];
    role=agents.some(a=>a.role===selected)?selected:'global';mount('配置 Agent');renderProfile();
  }
  function renderProfile() {
    const agent=agents.find(a=>a.role===role),global=role==='global';
    $('chat-panel-body').innerHTML=`<form id="agent-profile-form" class="profile-fields">
      <div><label class="field-label" for="profile-role">配置对象</label><select id="profile-role" class="text-input">${agents.map(a=>`<option value="${escape(a.role)}"${a.role===role?' selected':''}>${escape(a.title)}</option>`).join('')}</select></div>
      <div><div class="profile-field-head"><strong>模型服务</strong>${global?'':'<label><input id="inherit-provider" type="checkbox"> 使用默认</label>'}</div><select id="profile-provider" class="text-input"><option value="deepseek">DeepSeek · 官方地址</option></select></div>
      <div><div class="profile-field-head"><label class="field-label" for="profile-model">模型</label>${global?'':'<label><input id="inherit-model" type="checkbox"> 使用默认</label>'}</div><input id="profile-model" class="text-input" maxlength="100" value="${escape(agent.model)}" required><small class="field-help">${agent.sources.model==='independent'?'当前使用独立配置':'当前使用默认配置'}；修改只影响新任务。</small></div>
      <div><div class="profile-field-head"><label class="field-label" for="profile-key">API Key</label>${global?'':'<label><input id="inherit-api_key" type="checkbox"> 使用默认凭证</label>'}</div><input id="profile-key" class="text-input" type="password" autocomplete="new-password" spellcheck="false" placeholder="${agent.api_key_configured?'已配置，留空保留':'填写连接凭证'}"><p class="field-help">仅保存在本机，不进入聊天记录；页面不会返回已保存的密钥。</p></div>
      <div><div class="profile-field-head"><strong>Skills</strong><label><input id="inherit-skills" type="checkbox"> ${global?'使用业务默认规则':'使用默认规则'}</label></div><div class="profile-skills">${available.map(name=>`<label><input type="checkbox" data-profile-skill="${escape(name)}"${agent.skills.includes(name)?' checked':''}>${escape(name)}</label>`).join('')}</div><p class="field-help">可独立选择规则。全部取消并关闭继承，表示不加载 Skill。</p></div>
      <p id="profile-error" class="inline-error" hidden></p><div><button class="button button-primary" type="submit">保存配置</button></div>
      <details class="profile-skill-editor"><summary>编辑此 Agent 的 Skill 规则</summary><label class="field-label" for="profile-skill-name">规则</label><select id="profile-skill-name" class="text-input">${available.map(name=>`<option>${escape(name)}</option>`).join('')}</select><button id="profile-skill-read" class="text-button" type="button">读取规则</button><textarea id="profile-skill-text" class="text-input" maxlength="30000" aria-label="Skill 规则" placeholder="读取规则后编辑…"></textarea><p class="field-help">修改保存在本机，只覆盖当前 Agent 的规则，正在执行的任务沿用原规则。</p><button id="profile-skill-save" class="button button-secondary" type="button" disabled>保存规则</button></details>
    </form>`;
    const fields=['provider','model','api_key','skills'];
    for(const field of fields){const checkbox=$('inherit-'+field);if(!checkbox)continue;
      checkbox.checked=global?field==='skills'&&agent.skill_source!=='independent':agent.sources[field]!=='independent';
      const update=()=>{if(field==='skills')document.querySelectorAll('[data-profile-skill]').forEach(input=>input.disabled=checkbox.checked);
        else $(field==='api_key'?'profile-key':'profile-'+field).disabled=checkbox.checked;};
      checkbox.addEventListener('change',update);update();
    }
    $('profile-role').addEventListener('change',()=>{role=$('profile-role').value;renderProfile();});
    $('agent-profile-form').addEventListener('submit',async event=>{
      event.preventDefault();const button=event.submitter;button.disabled=true;$('profile-error').hidden=true;
      try{const body={inherit:[]};for(const field of fields){
        if($('inherit-'+field)?.checked){body.inherit.push(field);continue;}
        if(field==='skills')body.skills=[...document.querySelectorAll('[data-profile-skill]:checked')].map(i=>i.dataset.profileSkill);
        else body[field]=$(field==='api_key'?'profile-key':'profile-'+field).value.trim();
      }
      await api('/api/agent-profiles/'+encodeURIComponent(role),'POST',body);$('profile-key').value='';await refresh();await profiles(role);toast('配置已保存，新任务使用更新后的配置');}
      catch(error){$('profile-error').textContent=error.message;$('profile-error').hidden=false;}
      finally{button.disabled=false;}
    });
    let loaded=null;
    $('profile-skill-read').addEventListener('click',async()=>{try{const name=$('profile-skill-name').value;
      const data=await api('/api/skills/'+encodeURIComponent(name)+'?role='+encodeURIComponent(role));$('profile-skill-text').value=data.content;loaded=name;$('profile-skill-save').disabled=false;
    }catch(error){toast(error.message,true);}});
    $('profile-skill-name').addEventListener('change',()=>{$('profile-skill-save').disabled=true;loaded=null;$('profile-skill-text').value='';});
    $('profile-skill-save').addEventListener('click',async()=>{if(!loaded)return;try{await api('/api/skills/'+encodeURIComponent(loaded),'POST',{role,content:$('profile-skill-text').value});toast('此 Agent 的规则已保存');}catch(error){toast(error.message,true);}});
  }
  async function panel(name) {
    if(name==='settings'){await profiles();return;}
    const node=name==='library'?$('library-card'):$('view-'+name);
    if(!node||name==='chat')return;
    mount(({knowledge:'个人知识库',schedules:'安排定时任务',history:'任务运行记录',library:'生成内容',video:'视频材料',paper:'论文材料'})[name]||'对话操作');
    placeholder=document.createElement('span');node.before(placeholder);borrowed=node;node.hidden=false;node.classList.add('conversation-panel');$('chat-panel-body').append(node);
    if(name==='library')await refresh('library');else await refresh(name);
  }
  async function session(id) {
    const data=await api('/api/site-sessions/'+encodeURIComponent(id)),item=data.session;mount('账号与论文材料');
    $('chat-panel-body').innerHTML=`<div class="session-card"><strong>${escape(item.origin)}</strong><p>${escape(item.note||'请打开专用窗口，自行完成登录与验证。')}</p><p class="field-help">${item.automatic?'此适配器支持登录后读取页面提供的 PDF。':'此站点采用人工下载与导入。'} 密码和会话仅留在本机，不交给模型。</p><div><button id="session-open" class="button button-primary" type="button">打开专用登录窗口</button> <button id="session-clear" class="text-button" type="button">退出并清除会话</button> <button id="session-refresh" class="text-button" type="button">刷新状态</button></div>${item.task_id?`<button class="text-button" data-task-material="${escape(item.task_id)}" type="button">上传已下载的 PDF，继续原任务</button>`:''}<p id="session-error" class="inline-error" hidden></p></div>`;
    const bridge=(action)=>{
      if(!window.webkit?.messageHandlers?.agentDesktop){$('session-error').textContent='专用登录窗口需要桌面客户端；也可以手动下载后上传 PDF。';$('session-error').hidden=false;return;}
      window.webkit.messageHandlers.agentDesktop.postMessage({action,session_id:id});
    };
    $('session-open').disabled=['cleared','clear_pending'].includes(item.status);
    $('session-open').addEventListener('click',()=>bridge('open_account'));
    $('session-clear').addEventListener('click',()=>bridge('clear_account'));
    $('session-refresh').addEventListener('click',()=>session(id).catch(e=>toast(e.message,true)));
    if(item.status==='clear_pending')bridge('clear_account');
  }
  function init(options){({api,state,refresh,toast}=options);$('chat-panel-close').addEventListener('click',close);$('chat-config').addEventListener('click',()=>profiles().catch(e=>toast(e.message,true)));}
  return {init,profiles,panel,session,close};
})();
