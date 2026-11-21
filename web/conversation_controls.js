/* Private credentials stay in these same-origin controls, outside message history. */
window.AgentControls = (() => {
  const $ = id => document.getElementById(id);
  const escape = value => String(value ?? '').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  let api, state, refresh, toast, role='global', agents=[], available=[];
  let borrowed=null, placeholder=null, standalone=null, host=null, roleLocked=false;
  let panelVersion=0;
  function mount(title, target=null) {
    panelVersion += 1;
    restore();
    if(standalone){standalone.replaceChildren();standalone=null;}
    if(target){standalone=target;host=target;host.replaceChildren();return;}
    host=$('chat-panel-body'); $('chat-control-panel').hidden=false; $('chat-panel-title').textContent=title;
    $('chat-panel-body').replaceChildren();
  }
  function restore() {
    if(borrowed&&placeholder){borrowed.hidden=true;borrowed.classList.remove('conversation-panel');placeholder.replaceWith(borrowed);borrowed=null;placeholder=null;}
  }
  function close() { panelVersion+=1;restore();if(standalone){standalone.replaceChildren();standalone=null;}$('chat-control-panel').hidden=true;$('chat-panel-body').replaceChildren(); }
  function rememberProfiles(data) {
    agents=data.agents||[];
    available=data.available_skills||[];
  }
  async function profiles(selected='conversation', target=null, fixedRole=false) {
    const opening=++panelVersion;
    const data=await api('/api/agent-profiles');
    if(opening!==panelVersion)return;
    rememberProfiles(data);
    roleLocked=fixedRole;role=agents.some(a=>a.role===selected)?selected:'global';mount('配置 Agent',target);renderProfile();
  }
  function renderProfile() {
    const selectedRole=role, agent=agents.find(a=>a.role===selectedRole),global=selectedRole==='global';
    const skillHelp=global?'修改保存在本机。没有私有规则正文的角色会在下次加载此 Skill 时采用这份正文。':'修改保存在本机。当前角色下次加载此 Skill 时会采用这份私有正文。';
    host.innerHTML=`<form id="agent-profile-form" class="profile-fields">
      <div><label class="field-label" for="profile-role">配置对象</label><select id="profile-role" class="text-input">${agents.map(a=>`<option value="${escape(a.role)}"${a.role===role?' selected':''}>${escape(a.title)}</option>`).join('')}</select></div>
      <div><div class="profile-field-head"><strong>模型服务</strong>${global?'':'<label><input id="inherit-provider" type="checkbox"> 使用默认</label>'}</div><select id="profile-provider" class="text-input"><option value="deepseek">DeepSeek · 官方地址</option></select></div>
      <div>
        <div class="profile-field-head"><label class="field-label" for="profile-model">模型</label>${global?'':'<label><input id="inherit-model" type="checkbox"> 使用默认</label>'}</div>
        <input id="profile-model" class="text-input" maxlength="100" value="${escape(agent.model)}" required>
        <small class="field-help">${agent.sources.model==='independent'?'当前使用独立配置':'当前使用默认配置'}。排队中的任务在开始时读取配置。</small>
        <button id="profile-model-query" class="text-button" type="button"${agent.api_key_configured?'':' disabled'}>查询可用模型</button>
        <select id="profile-model-options" class="text-input" aria-label="当前凭证可用的模型" hidden></select>
        <p id="profile-model-status" class="field-help" role="status">${agent.api_key_configured?'使用当前角色已保存的凭证查询。':'保存 API Key 后可查询。'}</p>
      </div>
      <div><div class="profile-field-head"><label class="field-label" for="profile-key">API Key</label>${global?'':'<label><input id="inherit-api_key" type="checkbox"> 使用默认凭证</label>'}</div><input id="profile-key" class="text-input" type="password" autocomplete="new-password" spellcheck="false" placeholder="${agent.api_key_configured?'已配置，留空保留':'填写连接凭证'}"><p class="field-help">仅保存在本机，不进入聊天记录；页面不会返回已保存的密钥。</p></div>
      <div><div class="profile-field-head"><strong>Skills</strong><label><input id="inherit-skills" type="checkbox"> ${global?'使用业务默认列表':'使用默认列表'}</label></div><div class="profile-skills">${available.map(name=>`<label><input type="checkbox" data-profile-skill="${escape(name)}"${agent.skills.includes(name)?' checked':''}>${escape(name)}</label>`).join('')}</div><p class="field-help">可独立选择要加载的 Skill。关闭继承并取消全部选项表示不加载 Skill。私有规则正文可在下方编辑。</p></div>
      <p id="profile-error" class="inline-error" hidden></p><div><button class="button button-primary" type="submit">保存配置</button></div>
      <details class="profile-skill-editor"><summary>${global?'编辑默认 Skill 规则':'编辑此 Agent 的 Skill 规则'}</summary><label class="field-label" for="profile-skill-name">规则</label><select id="profile-skill-name" class="text-input">${available.map(name=>`<option>${escape(name)}</option>`).join('')}</select><button id="profile-skill-read" class="text-button" type="button">读取规则</button><textarea id="profile-skill-text" class="text-input" maxlength="30000" aria-label="Skill 规则" placeholder="读取规则后编辑…"></textarea><p class="field-help">${skillHelp}</p><button id="profile-skill-save" class="button button-secondary" type="button" disabled>保存规则</button></details>
    </form>`;
    const form=host.querySelector('#agent-profile-form');
    const field=id=>form.querySelector('#'+id);
    const picker=field('profile-role');
    const mounted=panelVersion;
    const isCurrent=()=>form.isConnected && mounted===panelVersion;
    const fields=['provider','model','api_key','skills'];
    for(const name of fields){const checkbox=field('inherit-'+name);if(!checkbox)continue;
      checkbox.checked=global?name==='skills'&&agent.skill_source!=='independent':agent.sources[name]!=='independent';
      const update=()=>{if(name==='skills')form.querySelectorAll('[data-profile-skill]').forEach(input=>input.disabled=checkbox.checked);
        else field(name==='api_key'?'profile-key':'profile-'+name).disabled=checkbox.checked;};
      checkbox.addEventListener('change',update);update();
    }
    picker.disabled=roleLocked;
    picker.addEventListener('change',()=>{role=picker.value;renderProfile();});
    const modelInput=field('profile-model');
    const modelQuery=field('profile-model-query');
    const modelOptions=field('profile-model-options');
    const modelStatus=field('profile-model-status');
    const modelInherit=field('inherit-model');
    modelInput.addEventListener('input',()=>{modelOptions.value='';});
    modelOptions.addEventListener('change',()=>{
      if(!modelOptions.value)return;
      if(modelInherit?.checked)modelInherit.click();
      modelInput.value=modelOptions.value;
      modelStatus.textContent='保存配置后使用所选模型。';
    });
    modelQuery.addEventListener('click',async()=>{
      modelQuery.disabled=true;
      modelOptions.hidden=true;
      modelStatus.textContent='正在向模型服务查询…';
      try{
        const {models}=await api('/api/agent-profiles/'+encodeURIComponent(selectedRole)+'/models','POST',{});
        if(!isCurrent())return;
        modelOptions.replaceChildren(new Option('选择一个可用模型',''), ...models.map(model=>new Option(model,model)));
        modelOptions.hidden=models.length===0;
        modelStatus.textContent=models.length?`查到 ${models.length} 个可用模型。选择后保存配置。`:'服务未返回可用模型，可手动填写模型名称。';
      }catch(error){
        if(isCurrent())modelStatus.textContent=error.message;
      }finally{
        if(isCurrent())modelQuery.disabled=false;
      }
    });
    form.addEventListener('submit',async event=>{
      event.preventDefault();
      const button=form.querySelector('button[type=submit]');
      if(button.disabled)return;
      button.disabled=true;
      picker.disabled=true;
      const errorBox=field('profile-error');
      errorBox.hidden=true;
      try{
        const body={inherit:[]};
        for(const name of fields){
          if(field('inherit-'+name)?.checked){body.inherit.push(name);continue;}
          if(name==='skills')body.skills=[...form.querySelectorAll('[data-profile-skill]:checked')].map(i=>i.dataset.profileSkill);
          else body[name]=field(name==='api_key'?'profile-key':'profile-'+name).value.trim();
        }
        await api('/api/agent-profiles/'+encodeURIComponent(selectedRole),'POST',body);
        field('profile-key').value='';
        if(!isCurrent())return;
        await refresh();
        if(!isCurrent())return;
        const data=await api('/api/agent-profiles');
        if(!isCurrent())return;
        rememberProfiles(data);
        renderProfile();
        toast('配置已保存，任务开始时使用更新后的配置');
      }catch(error){
        if(isCurrent()){errorBox.textContent=error.message;errorBox.hidden=false;}
      }finally{
        button.disabled=false;
        if(isCurrent())picker.disabled=roleLocked;
      }
    });
    const skillName=field('profile-skill-name'), skillText=field('profile-skill-text'), saveSkill=field('profile-skill-save');
    let skillVersion=0;
    const currentSkill=(version,name)=>isCurrent() && version===skillVersion && skillName.value===name;
    field('profile-skill-read').addEventListener('click',async()=>{
      const name=skillName.value, version=++skillVersion;
      saveSkill.disabled=true;
      try{
        const data=await api('/api/skills/'+encodeURIComponent(name)+'?role='+encodeURIComponent(selectedRole));
        if(!currentSkill(version,name))return;
        skillText.value=data.content;
        saveSkill.disabled=false;
      }catch(error){if(currentSkill(version,name))toast(error.message,true);}
    });
    skillName.addEventListener('change',()=>{skillVersion+=1;saveSkill.disabled=true;skillText.value='';});
    saveSkill.addEventListener('click',async()=>{
      if(saveSkill.disabled)return;
      const name=skillName.value, version=skillVersion;
      saveSkill.disabled=true;
      try{
        await api('/api/skills/'+encodeURIComponent(name),'POST',{role:selectedRole,content:skillText.value});
        if(currentSkill(version,name))toast(global?'默认规则已保存':'此 Agent 的私有规则已保存');
      }catch(error){if(currentSkill(version,name))toast(error.message,true);}
      finally{if(currentSkill(version,name))saveSkill.disabled=false;}
    });
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
    const opening=++panelVersion;
    const data=await api('/api/site-sessions/'+encodeURIComponent(id));
    if(opening===panelVersion)renderSession(data.session);
  }
  function renderSession(item) {
    mount('账号与论文材料');
    host.innerHTML=`<div class="session-card"><strong>${escape(item.origin)}</strong><p>${escape(item.note||'请打开专用窗口，自行完成登录与验证。')}</p><p class="field-help">${item.automatic?'此适配器支持登录后读取页面提供的 PDF。':'此站点采用人工下载与导入。'} 密码和会话仅留在本机，不交给模型。</p><div><button id="session-open" class="button button-primary" type="button">打开专用登录窗口</button> <button id="session-clear" class="text-button" type="button">退出并清除会话</button> <button id="session-refresh" class="text-button" type="button">刷新状态</button></div>${item.task_id?`<button class="text-button" data-task-material="${escape(item.task_id)}" type="button">上传已下载的 PDF，继续原任务</button>`:''}<p id="session-error" class="inline-error" hidden></p></div>`;
    const error = message => { $('session-error').textContent=message; $('session-error').hidden=false; };
    const desktop = window.webkit?.messageHandlers?.agentDesktop;
    const bridge=(action,id=item.id)=>{
      if(!desktop){error('专用登录窗口需要桌面客户端。也可以手动下载后上传 PDF。');return;}
      desktop.postMessage({action,session_id:id});
    };
    const cleared = item.status==='cleared';
    const reopen = cleared && item.task_id;
    const openButton = $('session-open');
    openButton.disabled = item.status==='clear_pending' || (cleared && !reopen);
    if(reopen)openButton.textContent='重新打开专用登录窗口';
    openButton.addEventListener('click', async()=>{
      if(!reopen || !desktop){bridge('open_account');return;}
      const opening=panelVersion;
      openButton.disabled=true;
      try {
        const data=await api('/api/tasks/'+encodeURIComponent(item.task_id)+'/account-session','POST',{});
        if(opening!==panelVersion)return;
        renderSession(data.session);
        bridge('open_account',data.session.id);
      } catch(e) {
        if(opening===panelVersion){error(e.message);openButton.disabled=false;}
      }
    });
    $('session-clear').disabled=cleared;
    $('session-clear').addEventListener('click',()=>bridge('clear_account'));
    $('session-refresh').addEventListener('click',()=>session(item.id).catch(e=>toast(e.message,true)));
    if(item.status==='clear_pending')bridge('clear_account');
  }
  function init(options){({api,state,refresh,toast}=options);$('chat-panel-close').addEventListener('click',close);$('chat-config').addEventListener('click',()=>profiles().catch(e=>toast(e.message,true)));}
  return {init,profiles,panel,session,close};
})();
