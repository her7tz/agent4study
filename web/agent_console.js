/* Subagent workspaces share runtime/configuration, with separate activity views. */
window.AgentConsole=(()=>{
  const $=id=>document.getElementById(id);
  const escape=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const aliases={video_summary:'video_learning',paper_summary:'paper_reading',conversation_knowledge:'knowledge_archivist'};
  const statuses={queued:'排队',running:'运行',waiting_user:'等待材料',completed:'完成',failed:'失败',cancelled:'取消',interrupted:'中断'};
  let state,api,setView,toast,knowledgeJobHTML;
  function owner(task){return task.agent_id||aliases[task.capability]||task.capability;}
  function name(id){return (state.agents||[]).find(agent=>agent.id===id)?.title||id;}
  function taskLine(task){return `<div class="agent-task-line"><span class="agent-task-state">${escape(statuses[task.status]||task.status)}</span><div><strong>${escape(name(owner(task)))} · ${escape(task.id.slice(0,8))}</strong><p>${escape(task.error||task.stage||'')}</p>${task.group_id?`<small>批次 ${escape(task.group_id.slice(0,8))}</small>`:''}</div><button type="button" class="text-button" data-open-task="${escape(task.id)}">${task.status==='completed'?'打开成果':'查看'}</button>${['queued','running','waiting_user'].includes(task.status)?`<button type="button" class="text-button" data-cancel-task="${escape(task.id)}">取消</button>`:''}</div>`;}
  function ensurePages(agents){
    for(const agent of agents){
      if($('view-'+agent.page))continue;
      const section=document.createElement('section');section.id='view-'+agent.page;section.className='view';section.hidden=true;
      section.innerHTML=`<div class="page-heading"><div><span class="eyebrow">SUBAGENT</span><h1 tabindex="-1">${escape(agent.title)}</h1><p>独立配置、任务进度与成果。</p></div></div><section class="card subagent-overview" id="agent-${escape(agent.id)}-overview"></section><section id="agent-${escape(agent.id)}-profile" class="card subagent-profile" hidden></section><section class="card subagent-task-panel"><h2>此 Agent 的任务与执行记录</h2><div id="agent-${escape(agent.id)}-tasks"></div></section><button type="button" class="button button-secondary" data-go="chat">前往主控制台委派任务</button>`;
      $('main-content').append(section);
      const nav=document.createElement('button');nav.type='button';nav.className='nav-item';nav.dataset.view=agent.page;nav.textContent=agent.title;
      ($('subagent-nav-items')||document.querySelector('.nav-list')).append(nav);
    }
  }
  function render(){
    const agents=state.agents||[];
    ensurePages(agents);
    $('console-parallel').textContent=`最多并行 ${state.parallel?.limit||4}`;
    const current=state.tasks.filter(task=>task.conversation_id===state.conversationId&&['queued','running','waiting_user'].includes(task.status));
    $('console-task-count').textContent=String(current.length);
    $('console-task-log').innerHTML=current.map(taskLine).join('');$('console-task-log').hidden=!current.length;
    const groups=new Map();for(const task of state.tasks.filter(t=>t.group_id&&t.conversation_id===state.conversationId)){const list=groups.get(task.group_id)||[];list.push(task);groups.set(task.group_id,list);}
    $('console-groups').innerHTML=[...groups].slice(0,5).map(([id,tasks])=>`<div class="terminal-group">批次 ${escape(id.slice(0,8))} · ${tasks.filter(t=>t.status==='completed').length}/${tasks.length} 完成 · ${tasks.filter(t=>t.status==='failed').length} 失败 · ${tasks.filter(t=>t.status==='waiting_user').length} 等待材料</div>`).join('');
    for(const agent of agents){
      const overview=$('agent-'+agent.id+'-overview'),log=$('agent-'+agent.id+'-tasks');
      if(overview)overview.innerHTML=`<p class="agent-purpose">${escape(agent.description)}</p><div class="agent-owned-tools"><div><strong>拥有的能力</strong>${agent.operations.map(op=>`<p>${escape(op.title)}</p>`).join('')}</div><div><strong>${agent.delegatable?'执行工具':'整理步骤'}</strong><p>${(agent.delegatable?agent.tools:agent.workflow_steps||[]).map(escape).join(' · ')}</p></div></div><button type="button" class="button button-secondary" data-owner-config="${escape(agent.id)}">配置此 Agent 的 API、模型与 Skills</button>`;
      if(log){
        if(agent.id==='knowledge_archivist')log.innerHTML=state.knowledgeJobs.length?state.knowledgeJobs.map(knowledgeJobHTML).join(''):'<p class="field-help">从 ChatGPT 分享整理知识，或将本地对话归档并整理知识后，任务会显示在这里。</p>';
        else {const tasks=state.tasks.filter(task=>owner(task)===agent.id);log.innerHTML=tasks.length?tasks.map(taskLine).join(''):'<p class="field-help">此 Agent 还没有任务。</p>';}
      }
    }
  }
  async function command(message){
    const parts=message.trim().split(/\s+/),name=parts[0];
    if(!name.startsWith('/'))return false;
    const pages={'/tasks':'history','/notes':'library','/knowledge':'knowledge','/schedule':'schedules','/settings':'settings','/chat':'chat'};
    if(pages[name]&&parts.length===1){await setView(pages[name]);return true;}
    if(name==='/agents'&&parts.length===1){$('console-task-details').open=true;$('console-agent-list').hidden=false;$('console-agent-list').innerHTML=(state.agents||[]).map(agent=>`<button type="button" class="text-button" data-go="${escape(agent.page)}">${escape(agent.id)} — ${escape(agent.title)}</button>`).join('');return true;}
    if(name==='/agent'&&parts.length===2){const agent=(state.agents||[]).find(a=>a.id===parts[1]);if(!agent)throw Error('没有注册该 Subagent。');await setView(agent.page);return true;}
    if(name==='/help'&&parts.length===1){$('console-command-help').open=true;return true;}
    throw Error('未知命令。输入 /help 查看控制台命令，或直接用自然语言描述任务。');
  }
  function init(options){({state,api,setView,toast,knowledgeJobHTML}=options);document.addEventListener('click',async event=>{const button=event.target.closest('[data-owner-config]');if(!button)return;try{const role=button.dataset.ownerConfig,host=$('agent-'+role+'-profile');host.hidden=false;await AgentControls.profiles(role,host,true);}catch(error){toast(error.message,true);}});}
  return {init,render,command};
})();
