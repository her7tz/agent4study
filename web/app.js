/* The local workspace uses only the same-origin API. No credentials are cached here. */
(() => {
  'use strict';
  const $ = (id) => document.getElementById(id);
  const icons = (name) => `<svg class="icon" aria-hidden="true"><use href="#i-${name}"/></svg>`;
  const escapeHTML = (value) => String(value ?? '').replace(/[&<>"']/g, (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[char]));
  const state = { status: null, capabilities: [], tasks: [], reports: [], jobs: [], runs: [], view: 'chat', currentTask: null, currentTaskId: null, activeReport: null, historyFilter: 'all', pollTimer: null, polling: false, connected: false, submitting: false, loading: false, resultVersion: 0, resultSignature: '', resultDownloadUrl: null, exportReady: false, exporting: false, scheduleSignature: '', scheduleDrafts: {}, conversationId: null, messages: [], chatBusy: false };
  const activeStatuses = new Set(['queued', 'running']);
  const statusNames = { queued: '等待处理', running: '处理中', completed: '已完成', failed: '执行失败', interrupted: '已中断', cancelled: '已取消', missed: '已跳过', deleted: '已移入回收站' };
  Object.assign(state, {conversations: [], chatRunId: null, watchGeneration: 0, chatSources: [], conversationEdit: null});
  const dateFormat = new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false });
  const fullDateFormat = new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false });

  function formatTime(value, full = false) {
    if (!value || !Number.isFinite(Number(value))) return '—';
    return (full ? fullDateFormat : dateFormat).format(new Date(Number(value) * 1000));
  }
  function statusPill(status) {
    const kind = status === 'completed' ? 'is-success' : activeStatuses.has(status) ? 'is-active' : status === 'missed' ? 'is-warning' : 'is-failed';
    return `<span class="status-pill ${kind}">${escapeHTML(statusNames[status] || status || '未知')}</span>`;
  }
  function emptyState(title, detail, icon = 'leaf') {
    return `<div class="empty-state">${icons(icon)}<strong>${escapeHTML(title)}</strong><p>${escapeHTML(detail)}</p></div>`;
  }
  function toast(message, isError = false) {
    const notice = document.createElement('div');
    notice.className = `toast${isError ? ' is-error' : ''}`;
    notice.innerHTML = `${icons(isError ? 'alert' : 'check')}<span>${escapeHTML(message)}</span>`;
    $('toast-container').append(notice);
    setTimeout(() => notice.remove(), 4500);
  }
  function inlineError(id, message = '') {
    $(id).textContent = message;
    $(id).hidden = !message;
  }

  async function api(path, method = 'GET', body = undefined, timeoutMs = 20000) {
    if (method !== 'GET' && !state.status?.csrf_token) throw new Error('请先连接本地服务，再执行操作。');
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), timeoutMs);
    const options = { method, signal: controller.signal, headers: { Accept: 'application/json' }, credentials: 'same-origin', cache: 'no-store' };
    if (method !== 'GET') {
      options.headers['Content-Type'] = 'application/json';
      options.headers['X-Agent-Token'] = state.status.csrf_token;
      options.body = JSON.stringify(body ?? {});
    }
    try {
      const response = await fetch(path, options);
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(typeof payload.error === 'string' ? payload.error : `操作未成功（${response.status}），请稍后重试。`);
      return payload;
    } catch (error) {
      if (error.name === 'AbortError') throw new Error(path === '/api/chat' ? 'Agent 回复超时，请先查看对话与运行记录，确认任务是否已安排。' : '本地服务响应超时，请确认服务仍在运行。');
      if (error instanceof TypeError) throw new Error('无法连接本地服务，请确认服务仍在运行。');
      throw error;
    } finally {
      clearTimeout(timeout);
    }
  }

  function setConnection(connected, message = '') {
    state.connected = connected;
    $('connection-dot').className = `status-dot ${connected ? 'is-ready' : 'is-error'}`;
    $('connection-label').textContent = connected ? '本地已连接' : '连接中断';
    $('connection-error').hidden = connected;
    if (message) $('connection-error-message').textContent = message;
    updateSubmitState();
  }

  function renderStatus() {
    if (!state.status) return;
    const status = state.status;
    const dependencies = status.dependencies || {};
    const configured = Boolean(status.api_key_configured);
    $('sidebar-provider').textContent = status.provider ? `${status.provider === 'deepseek' ? 'DeepSeek' : status.provider} 已配置` : 'DeepSeek';
    if (!configured) $('sidebar-provider').textContent = '等待连接模型';
    $('sidebar-model').textContent = status.model || '尚未选择模型';
    $('model-dot').className = `status-dot ${configured ? 'is-ready' : 'is-warning'}`;
    $('composer-model').textContent = status.model || '尚未选择模型';
    $('api-endpoint').textContent = status.base_url || 'https://api.deepseek.com';
    $('key-status').className = `status-pill ${configured ? 'is-success' : 'is-warning'}`;
    $('key-status').textContent = configured ? '已配置凭证' : '待配置';
    $('key-action-caption').textContent = configured ? '留空则保留已保存的凭证' : '连接凭证';
    $('api-key').placeholder = configured ? '填写新密钥以更新，留空保留' : '填写你的 DeepSeek API Key';
    const models = [...new Set([...(Array.isArray(status.models) ? status.models : []), status.model].filter(Boolean))];
    const previousModel = $('model-select').value;
    $('model-select').replaceChildren(...models.map((model) => {
      const option = document.createElement('option');
      option.value = model;
      option.textContent = model;
      return option;
    }));
    $('model-select').value = models.includes(previousModel) && state.view === 'settings' ? previousModel : status.model || models[0] || '';
    const needsSetup = !configured || !dependencies.yt_dlp;
    $('setup-notice').hidden = !needsSetup;
    if (!configured) {
      $('setup-notice-title').textContent = '先连接模型，就可以开始整理';
      $('setup-notice-detail').textContent = '在模型设置中填写 DeepSeek API Key，凭证只保存在本机。';
    } else if (!dependencies.yt_dlp) {
      $('setup-notice-title').textContent = '还需要准备视频读取工具';
      $('setup-notice-detail').textContent = '当前设备缺少视频读取工具，请在本地准备 yt-dlp 后刷新页面。';
    }
    $('scheduler-status').textContent = status.scheduler_running ? '本地定时服务正在运行' : '本地定时服务尚未运行';
    const dependencyRows = [
      { name: '视频内容读取', description: '用于读取平台字幕与媒体', ready: dependencies.yt_dlp, missing: '缺少 yt-dlp' },
      { name: 'YouTube 平台支持', description: '用于解析 YouTube 视频内容', ready: dependencies.javascript_runtime, missing: '尚未准备运行时' },
      { name: '关键画面提取', description: '用于提取视频原图', ready: dependencies.ffmpeg, missing: '缺少 FFmpeg' },
      { name: '无字幕转录', description: status.dependency_errors?.asr || '字幕不可用时的后备方案', ready: dependencies.asr, missing: '尚未准备 ASR' },
      { name: '定时任务服务', description: '电脑与服务运行时执行', ready: status.scheduler_running, missing: '未运行' },
    ];
    $('dependency-list').innerHTML = dependencyRows.map((item) => `<div class="dependency-row"><div><strong>${escapeHTML(item.name)}</strong><small>${escapeHTML(item.description)}</small></div><span class="status-pill ${item.ready ? 'is-success' : 'is-warning'}">${escapeHTML(item.ready ? '已就绪' : item.missing)}</span></div>`).join('');
    updateSubmitState();
  }

  function updateSubmitState() {
    const ready = state.connected && state.status?.api_key_configured && state.status?.dependencies?.yt_dlp;
    $('summarize-button').disabled = state.submitting || !ready;
    $('summarize-button').querySelector('span').textContent = state.submitting ? '正在提交…' : '生成视频笔记';
    $('schedule-submit').disabled = !state.connected || !state.capabilities.length;
    $('settings-submit').disabled = !state.connected;
    $('chat-submit').disabled = !state.connected || !state.status?.api_key_configured || state.chatBusy;
    $('chat-submit').querySelector('span').textContent = state.chatBusy ? '正在处理…' : '发送';
    $('chat-new').disabled = !state.connected;
    $('chat-busy').hidden = !state.chatBusy;
    $('chat-stop').hidden = !state.chatBusy;
    $('chat-retry').disabled = state.chatBusy || !state.messages.some(message => message.role === 'user');
    $('conversation-delete').disabled = state.chatBusy || !state.conversationId;
    $('conversation-rename').disabled = !state.conversationId;
  }

  function capabilityInfo(id) {
    return state.capabilities.find((item) => item.id === id);
  }
  function capabilityTitle(item) {
    return item?.capability_title || capabilityInfo(item?.capability || 'video_summary')?.title || item?.capability || '视频总结';
  }
  function inputSummary(item) {
    const parameters = item?.parameters && typeof item.parameters === 'object' ? item.parameters : item?.url ? { url: item.url } : {};
    const properties = capabilityInfo(item?.capability || 'video_summary')?.input_schema?.properties || {};
    return Object.entries(parameters).filter(([name]) => !properties[name]?.writeOnly).map(([name, value]) => {
      const title = properties[name]?.title || name;
      const text = typeof value === 'boolean' ? value ? '开启' : '关闭' : typeof value === 'object' ? JSON.stringify(value) : String(value ?? '');
      return `${title}：${text}`;
    }).join(' · ');
  }
  function rememberScheduleDraft() {
    const id = $('schedule-parameters').dataset.capability || $('schedule-capability').value;
    if (!id || !$('schedule-parameters').children.length) return;
    state.scheduleDrafts[id] = Object.fromEntries([...$('schedule-parameters').querySelectorAll('[data-parameter]')].map((input) => [input.dataset.parameter, input.type === 'checkbox' ? input.checked : input.value]));
  }
  function renderScheduleParameters(force = false) {
    const id = $('schedule-capability').value;
    const capability = capabilityInfo(id);
    const schema = capability?.input_schema || {};
    const signature = JSON.stringify([id, schema]);
    $('schedule-capability-description').textContent = capability?.description || '';
    if (!force && signature === state.scheduleSignature) return;
    rememberScheduleDraft();
    state.scheduleSignature = signature;
    const container = $('schedule-parameters');
    container.replaceChildren();
    container.dataset.capability = id;
    if (!capability) {
      const notice = document.createElement('p');
      notice.className = 'field-help';
      notice.textContent = '没有可用功能，请确认本地服务已加载功能配置。';
      container.append(notice);
      return;
    }
    const required = new Set(schema.required || []);
    const draft = state.scheduleDrafts[id] || {};
    Object.entries(schema.properties || {}).forEach(([name, field], index) => {
      const wrapper = document.createElement('div');
      wrapper.className = `schedule-parameter-field${field.type === 'boolean' ? ' is-checkbox' : ''}`;
      const label = document.createElement('label');
      label.className = 'field-label';
      label.htmlFor = `schedule-parameter-${index}`;
      label.textContent = `${field.title || name}${required.has(name) ? '' : '（可选）'}`;
      let input;
      if (Array.isArray(field.enum)) {
        input = document.createElement('select');
        field.enum.forEach((value, enumIndex) => {
          const option = document.createElement('option');
          option.value = String(enumIndex);
          option.textContent = String(field.enumNames?.[enumIndex] ?? value);
          input.append(option);
        });
        const defaultIndex = field.enum.findIndex((value) => value === field.default);
        input.value = draft[name] ?? String(defaultIndex >= 0 ? defaultIndex : 0);
      } else if (field.type === 'object' || field.type === 'array') {
        input = document.createElement('textarea');
        input.rows = 3;
        input.value = draft[name] ?? (field.default !== undefined ? JSON.stringify(field.default, null, 2) : '');
      } else {
        input = document.createElement('input');
        input.type = field.type === 'boolean' ? 'checkbox' : ['number', 'integer'].includes(field.type) ? 'number' : field.writeOnly ? 'password' : field.format === 'uri' || field.format === 'url' ? 'url' : field.format === 'email' ? 'email' : 'text';
        if (field.type === 'boolean') input.checked = draft[name] ?? Boolean(field.default);
        else input.value = draft[name] ?? field.default ?? '';
        if (['number', 'integer'].includes(field.type)) {
          input.step = field.type === 'integer' ? '1' : 'any';
          if (field.minimum !== undefined) input.min = String(field.minimum);
          if (field.maximum !== undefined) input.max = String(field.maximum);
        }
        if (field.maxLength !== undefined) input.maxLength = field.maxLength;
        if (field.minLength !== undefined) input.minLength = field.minLength;
      }
      input.id = label.htmlFor;
      input.dataset.parameter = name;
      if (field.type !== 'boolean') input.className = 'text-input';
      input.required = required.has(name) && field.type !== 'boolean';
      input.autocomplete = 'off';
      input.placeholder = field.placeholder || (['object', 'array'].includes(field.type) ? '填写 JSON 格式的输入' : '');
      if (field.type === 'boolean') { label.prepend(input); wrapper.append(label); }
      else wrapper.append(label, input);
      if (field.description) {
        const help = document.createElement('p');
        help.id = `${input.id}-help`;
        help.className = 'field-help';
        help.textContent = field.description;
        input.setAttribute('aria-describedby', help.id);
        wrapper.append(help);
      }
      container.append(wrapper);
    });
  }
  function renderCapabilities() {
    const previous = $('schedule-capability').value;
    rememberScheduleDraft();
    $('schedule-capability').replaceChildren(...state.capabilities.map((capability) => {
      const option = document.createElement('option');
      option.value = capability.id;
      option.textContent = capability.title || capability.id;
      return option;
    }));
    if (!state.capabilities.length) {
      const option = document.createElement('option');
      option.value = '';
      option.textContent = '暂无可用功能';
      $('schedule-capability').append(option);
    } else $('schedule-capability').value = capabilityInfo(previous) ? previous : state.capabilities[0].id;
    $('chat-capabilities').textContent = state.capabilities.map((item) => item.title || item.id).join('、') || '暂无可用功能';
    renderScheduleParameters();
    updateSubmitState();
  }
  function readScheduleParameters() {
    const capability = capabilityInfo($('schedule-capability').value);
    if (!capability) throw new Error('请选择可用功能。');
    const parameters = {};
    const schema = capability.input_schema || {};
    const required = new Set(schema.required || []);
    for (const input of $('schedule-parameters').querySelectorAll('[data-parameter]')) {
      const name = input.dataset.parameter;
      const field = schema.properties[name];
      if (field.type === 'boolean') { parameters[name] = input.checked; continue; }
      if (!input.value.trim() && !required.has(name)) continue;
      if (Array.isArray(field.enum)) parameters[name] = field.enum[Number(input.value)];
      else if (['object', 'array'].includes(field.type)) {
        try { parameters[name] = JSON.parse(input.value); } catch { throw new Error(`${field.title || name}需要有效的 JSON 格式。`); }
      } else if (['number', 'integer'].includes(field.type)) {
        const value = Number(input.value);
        if (!input.value.trim() || !Number.isFinite(value) || field.type === 'integer' && !Number.isInteger(value)) throw new Error(`请为${field.title || name}填写有效数字。`);
        parameters[name] = value;
      } else parameters[name] = input.value.trim();
    }
    return parameters;
  }

  function chatMarkdown(text) {
    const template = document.createElement('template');
    if (!window.marked || !window.DOMPurify) { template.content.append(document.createTextNode(text)); return template.content; }
    try {
      const renderer = new window.marked.Renderer();
      renderer.html = token => escapeHTML(typeof token === 'string' ? token : token.text || '');
      template.innerHTML = window.DOMPurify.sanitize(window.marked.parse(text, {renderer, gfm:true, async:false}), {
        ALLOWED_TAGS:['p','br','h1','h2','h3','h4','h5','h6','ul','ol','li','strong','em','del','blockquote','pre','code','a','img','table','thead','tbody','tr','th','td','hr'],
        ALLOWED_ATTR:['href','src','alt','title','class','start'], ALLOW_DATA_ATTR:false});
      for (const link of template.content.querySelectorAll('a')) {
        try {
          const url = new URL(link.getAttribute('href'), location.origin);
          if (!['https:','http:'].includes(url.protocol)) throw new Error();
          link.href = url.href; link.target = '_blank'; link.rel = 'noopener noreferrer';
        } catch { link.replaceWith(document.createTextNode(link.textContent || '')); }
      }
      for (const img of template.content.querySelectorAll('img')) {
        const safe = outputUrl(img.getAttribute('src'));
        if (!safe || !/\.(png|jpe?g|webp|gif)$/i.test(new URL(safe, location.origin).pathname)) img.replaceWith(document.createTextNode(img.alt || '[图片不可用]'));
        else { img.src = safe; img.loading = 'lazy'; }
      }
    } catch { template.content.replaceChildren(document.createTextNode(text)); }
    return template.content;
  }

  let chatDiagramBusy = false;
  async function renderChatDiagrams() {
    if (!window.mermaid || state.chatBusy || chatDiagramBusy) return;
    chatDiagramBusy = true;
    try {
      window.mermaid.initialize({startOnLoad:false, securityLevel:'strict', suppressErrorRendering:true, theme:'neutral'});
      for (const code of [...$('chat-messages').querySelectorAll('pre > code.language-mermaid')].slice(0, 12)) {
        const source = code.textContent || '';
        if (source.length > 6000 || /https?:|ftp:|file:|data:|javascript:|url\s*\(|\bimg\s*:|%%\{|^\s*---|</i.test(source)) continue;
        const original = code.parentElement, diagram = document.createElement('div');
        diagram.className = 'mermaid'; diagram.textContent = source; original.replaceWith(diagram);
        try { await window.mermaid.run({nodes:[diagram]}); }
        catch { if (diagram.isConnected) diagram.replaceWith(original); }
      }
    } finally { chatDiagramBusy = false; }
  }

  async function copyMessage(text) {
    try {
      if (navigator.clipboard) await navigator.clipboard.writeText(text);
      else { const input = document.createElement('textarea'); input.value = text; document.body.append(input); input.select(); document.execCommand('copy'); input.remove(); }
      toast('已复制');
    } catch { toast('无法自动复制，请选择文本后复制', true); }
  }

  function renderConversations() {
    $('conversation-list').innerHTML = state.conversations.length ? state.conversations.map(item => `<button type="button" class="conversation-item${item.id === state.conversationId ? ' is-current' : ''}" data-conversation="${escapeHTML(item.id)}" aria-pressed="${item.id === state.conversationId}"><strong>${escapeHTML(item.title || '新对话')}</strong><small>${escapeHTML(formatTime(item.updated_at))}</small></button>`).join('') : '<p class="field-help">新对话会保存在这里。</p>';
  }

  async function refreshConversations() {
    const data = await api('/api/conversations'); state.conversations = data.conversations || []; renderConversations();
  }

  async function watchRun(runId, initialText='') {
    const generation = ++state.watchGeneration;
    state.chatRunId = runId; state.chatBusy = true; updateSubmitState();
    let cursor = 0;
    const draft = {role:'assistant', content:initialText, streaming:true, actions:[]};
    state.messages.push(draft); renderChat();
    while (generation === state.watchGeneration) {
      try {
        const data = await api(`/api/chat/runs/${encodeURIComponent(runId)}?after=${cursor}`);
        if (generation !== state.watchGeneration) return;
        for (const event of data.events || []) {
          cursor = Math.max(cursor, event.sequence);
          if (event.type === 'round') draft.content = '';
          if (event.type === 'delta') draft.content += event.text;
          if (event.type === 'status') $('chat-stage').textContent = event.text;
          if (event.type === 'sources') draft.actions = (event.sources || []).map(source => ({type:'source', ...source}));
        }
        renderChat();
        if (data.conversation) {
          applyConversation(data.conversation);
          if (data.run.error) inlineError('chat-error', data.run.error);
          await Promise.allSettled([refreshAll(), refreshLibrary(), refreshConversations()]);
          return;
        }
        await new Promise(resolve => setTimeout(resolve, 400));
      } catch (error) {
        if (generation !== state.watchGeneration) return;
        inlineError('chat-error', '回复仍在后台处理，连接恢复后可刷新查看。' + error.message);
        state.chatBusy = false; updateSubmitState(); return;
      }
    }
  }

  async function submitChat(message, retry=false) {
    if (state.chatBusy) return;
    inlineError('chat-error');
    state.chatBusy = true; updateSubmitState();
    const conversationId = state.conversationId;
    try {
      const response = await api('/api/chat/start', 'POST', {message,conversation_id:conversationId,retry,web_enabled:$('chat-web').checked});
      if (state.conversationId !== conversationId) return;
      if (!retry && $('chat-input').value.trim() === message) $('chat-input').value = '';
      applyConversation(response.conversation);
      if (!response.conversation.active_run) watchRun(response.run_id);
      refreshConversations().catch(error => toast(error.message, true));
    } catch (error) { inlineError('chat-error', error.message); state.chatBusy = false; updateSubmitState(); }
  }

  function renderChat() {
    const container = $('chat-messages');
    const wasAtBottom = container.scrollHeight - container.scrollTop - container.clientHeight < 80;
    container.replaceChildren();
    if (!state.messages.length) {
      container.innerHTML = emptyState('从一句话开始', '解释知识、搜索资料、研究问题，或让 Agent 完成一个任务。', 'spark');
      return;
    }
    for (const message of state.messages) {
      if (!['user', 'assistant'].includes(message.role)) continue;
      const article = document.createElement('article');
      article.className = `chat-message is-${message.role}`;
      const label = document.createElement('strong');
      label.className = 'chat-message-label';
      label.textContent = message.role === 'user' ? '你' : 'Agent';
      const content = document.createElement('div');
      content.className = 'chat-message-content' + (message.role === 'assistant' ? ' markdown-content' : '');
      if (message.role === 'assistant') content.append(chatMarkdown(message.content || ''));
      else content.textContent = message.content || '';
      article.append(label, content);
      if (message.status && message.status !== 'completed') {
        const status = document.createElement('small');
        status.className = 'message-status';
        status.textContent = statusNames[message.status] || '回复未完成';
        article.append(status);
      }
      if (message.role === 'assistant' && !message.streaming) {
        const copy = document.createElement('button');
        copy.type = 'button'; copy.className = 'text-button message-copy'; copy.textContent = '复制';
        copy.addEventListener('click', () => copyMessage(message.content || ''));
        article.append(copy);
      }
      if (Array.isArray(message.actions) && message.actions.length) {
        const actions = document.createElement('div');
        actions.className = 'chat-actions';
        for (const action of message.actions) {
          if (action.type === 'source') {
            try {
              const url = new URL(action.url);
              if (!['http:', 'https:'].includes(url.protocol)) continue;
              const link = document.createElement('a'); link.className = 'source-link';
              link.href = url.href; link.target = '_blank'; link.rel = 'noopener noreferrer';
              link.textContent = `${action.kind === 'page' ? '已阅读' : '搜索线索'} · ${action.title || url.hostname}`;
              actions.append(link);
            } catch {}
            continue;
          }
          if (!['task', 'schedule', 'note', 'deleted'].includes(action.type)) continue;
          const button = document.createElement('button');
          button.type = 'button';
          button.className = 'button button-secondary button-small';
          const title = action.title || capabilityInfo(action.capability)?.title || '任务';
          if (action.type === 'task' && action.task_id) {
            button.dataset.openTask = action.task_id;
            const task = state.tasks.find((item) => item.id === action.task_id);
            button.textContent = `${title} · ${task ? statusNames[task.status] || task.status : '查看进度'}`;
          } else if (action.type === 'schedule' && action.job_id) {
            button.dataset.go = 'schedules';
            button.textContent = `${title} · 查看定时任务`;
          } else if (action.type === 'note' && action.artifact_id) {
            button.dataset.artifactOpen = action.artifact_id;
            button.textContent = `${title} · 打开内容`;
          } else if (action.type === 'deleted' && action.trash_id) {
            button.dataset.artifactRestore = action.trash_id;
            button.textContent = `${title} · 恢复内容`;
          } else continue;
          actions.append(button);
          if (action.type === 'task' && activeStatuses.has(state.tasks.find(item => item.id === action.task_id)?.status)) {
            const cancel = document.createElement('button'); cancel.className = 'text-button'; cancel.type = 'button';
            cancel.dataset.cancelTask = action.task_id; cancel.textContent = '取消任务'; actions.append(cancel);
          }
        }
        article.append(actions);
      }
      container.append(article);
    }
    if (wasAtBottom || state.chatBusy) container.scrollTop = container.scrollHeight;
    renderChatDiagrams();
  }
  function applyConversation(data) {
    state.watchGeneration += 1;
    state.conversationId = data.conversation_id || null;
    state.messages = Array.isArray(data.messages) ? data.messages : [];
    state.chatBusy = Boolean(data.active_run);
    state.chatRunId = data.active_run?.id || null;
    $('conversation-title').textContent = data.title || '新对话';
    renderChat();
    renderConversations();
    updateSubmitState();
    if (data.active_run) watchRun(data.active_run.id, data.active_run.partial || '');
  }
  async function refreshConversation() {
    if (state.chatBusy) return;
    const data = await api('/api/conversations/current');
    if (!state.chatBusy) applyConversation(data);
  }

  async function refreshLibrary() {
    const [data, trash] = await Promise.all([api('/api/artifacts'), api('/api/artifacts/trash')]);
    $('library-directory').textContent = `保存位置：${data.directory || state.status?.output_dir || ''}`;
    const items = data.artifacts || [];
    $('library-list').innerHTML = items.length ? items.map((item) => `<div class="library-row"><div><strong>${escapeHTML(item.title)}</strong><small>${escapeHTML(formatTime(item.modified_at, true))}</small></div><div class="library-actions"><button class="text-button" data-artifact-open="${escapeHTML(item.id)}">打开</button><button class="text-button" data-artifact-delete="${escapeHTML(item.id)}">移入回收站</button></div></div>`).join('') : '<p class="field-help">内容库还没有文本。完成的任务会自动保存在这里。</p>';
    $('library-trash').innerHTML = (trash.artifacts || []).map((item) => `<div class="library-row"><span>${escapeHTML(item.title)}</span><button class="text-button" data-artifact-restore="${escapeHTML(item.trash_id)}">恢复</button></div>`).join('') || '<p class="field-help">回收站为空。</p>';
  }

  function placeResult(inConversation) {
    if (inConversation) {
      $('result-dialog-slot').append($('result-card'));
      if (!$('result-dialog').open) $('result-dialog').showModal();
    } else {
      if ($('result-dialog').open) $('result-dialog').close();
      $('video-result-home').after($('result-card'));
    }
  }

  async function openArtifact(id) {
    const { artifact } = await api(`/api/artifacts/${encodeURIComponent(id)}`);
    if (!artifact) throw new Error('内容已不存在，请刷新内容库。');
    await openReport({url: artifact.url, title: artifact.title, modified_at: artifact.modified_at});
  }

  function renderReports() {
    const reports = state.reports.slice(0, 6);
    $('report-count').textContent = state.reports.length ? `${state.reports.length} 份` : '';
    if (!reports.length) {
      $('recent-reports').innerHTML = emptyState('第一份笔记，从一个链接开始', '生成后的笔记会出现在这里，随时预览或下载。');
      return;
    }
    $('recent-reports').innerHTML = reports.map((report, index) => `<button class="report-card" data-report-index="${index}"><span class="report-card-head"><span class="report-icon">${icons('file')}</span>${icons('arrow')}</span><h3>${escapeHTML(report.title || report.name || '视频笔记')}</h3><span class="report-card-meta"><span>${escapeHTML(formatTime(report.modified_at))}</span><span>Markdown · ${escapeHTML(formatSize(report.size))}</span></span></button>`).join('');
  }
  function formatSize(size) {
    if (!Number.isFinite(Number(size))) return '笔记';
    return Number(size) < 1024 ? `${size} B` : `${Math.max(1, Math.round(Number(size) / 1024))} KB`;
  }

  function renderProgress() {
    const task = state.currentTask;
    const card = $('progress-card');
    card.hidden = !task;
    if (!task) return;
    const isActive = activeStatuses.has(task.status);
    const failed = ['failed', 'interrupted'].includes(task.status);
    const elapsed = Math.max(0, Math.floor(((task.finished_at || Date.now() / 1000) - Number(task.created_at))));
    const elapsedText = Number.isFinite(elapsed) ? elapsed < 60 ? `${elapsed} 秒` : `${Math.floor(elapsed / 60)} 分 ${elapsed % 60} 秒` : '';
    const title = isActive ? '正在为你整理视频' : task.status === 'completed' ? '这份视频笔记，已经整理好了' : task.status === 'interrupted' ? '任务已中断' : '这次整理没有完成';
    const description = isActive ? '可切换页面，任务会继续处理。' : task.status === 'completed' ? `${Number(task.sections) || 0} 个内容分段 · ${Number(task.frames) || 0} 张关键画面` : task.error || '请查看本地运行情况后重试。';
    card.className = `card progress-card${failed ? ' is-failed' : ''}`;
    card.innerHTML = `<div class="progress-icon">${isActive ? '<span class="spinner" aria-hidden="true"></span>' : icons(failed ? 'alert' : 'check')}</div><div class="progress-content"><div class="progress-topline"><strong>${escapeHTML(title)}</strong><span class="progress-time">${escapeHTML(elapsedText)}</span></div><p>${escapeHTML(description)}</p>${isActive ? `<p class="progress-detail">${escapeHTML(task.stage || statusNames[task.status])}</p>` : ''}${failed ? '<button class="text-button" data-retry-task>重新填写并尝试 ' + icons('arrow') + '</button>' : ''}</div>`;
  }

  function outputUrl(value) {
    if (typeof value !== 'string' || !value) return null;
    let candidate = value;
    const base = state.status?.output_dir;
    if (base && candidate.startsWith(`${base}/`)) candidate = candidate.slice(base.length + 1);
    if (!candidate.startsWith('/outputs/') && !/^[a-zA-Z][a-zA-Z\d+.-]*:/.test(candidate) && !candidate.startsWith('/')) {
      candidate = '/outputs/' + candidate.split('/').map(encodeURIComponent).join('/');
    }
    try {
      const url = new URL(candidate, location.origin);
      const decodedPath = decodeURIComponent(url.pathname);
      if (url.origin !== location.origin || !url.pathname.startsWith('/outputs/') || decodedPath.split('/').some((part) => part === '..' || part === '.') || decodedPath.includes('\\')) return null;
      return url.pathname;
    } catch { return null; }
  }

  async function renderMarkdown(markdown, baseUrl) {
    const version = ++state.resultVersion;
    const container = $('result-body');
    container.replaceChildren();
    if (!window.marked || !window.DOMPurify) {
      const notice = document.createElement('p');
      notice.className = 'markdown-notice';
      notice.textContent = '已使用纯文本显示笔记。可下载包含图片的 Markdown 压缩包。';
      const pre = document.createElement('pre');
      pre.className = 'plain-markdown';
      pre.textContent = markdown;
      container.append(notice, pre);
      return false;
    }
    let rendered;
    try {
      const renderer = new window.marked.Renderer();
      renderer.html = (token) => escapeHTML(typeof token === 'string' ? token : token.text || '');
      rendered = window.marked.parse(markdown, { renderer, gfm: true, breaks: false, async: false });
      const sanitized = window.DOMPurify.sanitize(rendered, {
        ALLOWED_TAGS: ['p', 'br', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'ul', 'ol', 'li', 'strong', 'em', 'del', 'blockquote', 'pre', 'code', 'a', 'img', 'table', 'thead', 'tbody', 'tr', 'th', 'td', 'hr'],
        ALLOWED_ATTR: ['href', 'src', 'alt', 'title', 'class', 'start'],
        ALLOW_DATA_ATTR: false,
      });
      const template = document.createElement('template');
      template.innerHTML = sanitized;
      for (const link of template.content.querySelectorAll('a')) {
        try {
          const href = link.getAttribute('href') || '';
          const url = new URL(href, new URL(baseUrl || '/outputs/', location.origin));
          if (!['https:', 'http:'].includes(url.protocol)) throw new Error('Unsupported link');
          link.href = url.href;
          link.target = '_blank';
          link.rel = 'noopener noreferrer';
        } catch {
          link.replaceWith(document.createTextNode(link.textContent || ''));
        }
      }
      for (const img of template.content.querySelectorAll('img')) {
        try {
          const imageUrl = new URL(img.getAttribute('src') || '', new URL(baseUrl || '/outputs/', location.origin));
          const safe = outputUrl(imageUrl.href);
          if (!safe || !/\.(png|jpe?g|webp|gif)$/i.test(imageUrl.pathname)) throw new Error('Unsupported image');
          img.src = safe;
          img.loading = 'lazy';
          img.decoding = 'async';
          img.referrerPolicy = 'no-referrer';
          if (!img.alt) img.alt = '视频关键画面';
        } catch {
          const note = document.createElement('span');
          note.textContent = img.alt ? `[图片：${img.alt}]` : '[图片不可用]';
          img.replaceWith(note);
        }
      }
      if (version !== state.resultVersion) return;
      container.append(template.content);
      if (window.mermaid) {
        window.mermaid.initialize({ startOnLoad: false, securityLevel: 'strict', suppressErrorRendering: true, theme: 'base', themeVariables: { fontFamily: '-apple-system, BlinkMacSystemFont, PingFang SC, sans-serif', fontSize: '13px', primaryColor: '#edf3e7', primaryTextColor: '#4d6844', primaryBorderColor: '#b7caa5', lineColor: '#9bb789', secondaryColor: '#f6f8f0', tertiaryColor: '#ffffff' } });
        const diagrams = [];
        for (const code of container.querySelectorAll('pre > code.language-mermaid')) {
          // Diagrams stay self-contained; custom directives, HTML and image URLs
          // are retained as code rather than allowed to request remote resources.
          const source = code.textContent || '';
          if (/https?:|ftp:|file:|data:|javascript:|url\s*\(|\bimg\s*:|%%\{|^\s*---|</i.test(source)) continue;
          const original = code.parentElement;
          const diagram = document.createElement('div');
          diagram.className = 'mermaid';
          diagram.textContent = source;
          original.replaceWith(diagram);
          diagrams.push({ diagram, original });
        }
        for (const { diagram, original } of diagrams) {
          try {
            if (version !== state.resultVersion) return;
            await window.mermaid.run({ nodes: [diagram] });
          } catch {
            if (diagram.isConnected) diagram.replaceWith(original);
          }
        }
      }
      return true;
    } catch {
      container.replaceChildren();
      const pre = document.createElement('pre');
      pre.className = 'plain-markdown';
      pre.textContent = markdown;
      container.append(pre);
      return false;
    }
  }

  function updateDownloadState() {
    $('result-download').disabled = !state.exportReady || state.exporting;
    $('result-download-label').textContent = state.exporting ? '正在准备下载…' : '下载笔记';
  }

  const offlineNoteStyles = `
    :root{color-scheme:light}*{box-sizing:border-box}body{margin:0;background:#f6f8f2;color:#344336;font:16px/1.85 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}
    article{max-width:960px;margin:40px auto;padding:40px 48px;background:#fff;border:1px solid #e5eadf;border-radius:16px;overflow-wrap:anywhere}
    h1,h2,h3,h4,h5,h6{color:#294d3b;line-height:1.5}h1{font-size:30px;margin:0 0 28px}h2{font-size:23px;margin:36px 0 18px;padding-bottom:10px;border-bottom:1px solid #e5eadf}h3{font-size:19px;margin:28px 0 14px}
    p{margin:14px 0}a{color:#27644b}li{margin:6px 0}img{display:block;max-width:100%;height:auto;margin:22px auto;border-radius:8px}blockquote{margin:22px 0;padding:8px 20px;background:#f3f6ee;border-left:4px solid #9db68b}
    pre{padding:18px;overflow:auto;background:#f3f5ef;border-radius:8px;font-size:14px;line-height:1.6}code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace}p code,li code{padding:2px 5px;background:#f3f5ef;border-radius:4px}
    table{border-collapse:collapse;width:100%;margin:20px 0}th,td{border:1px solid #dce4d5;padding:10px 14px;text-align:left}th{background:#f3f6ee}hr{border:0;border-top:1px solid #e5eadf;margin:30px 0}
    .mermaid{margin:24px 0;text-align:center;overflow:auto}.mermaid svg{max-width:100%;height:auto}.mermaid .nodeLabel p{margin:0}.mermaid foreignObject{overflow:visible}
    @media(max-width:640px){article{margin:0;padding:24px 20px;border:0;border-radius:0}h1{font-size:25px}body{font-size:15px}}
    @media print{body{background:#fff}article{margin:0;padding:0;max-width:none;border:0}img,svg{break-inside:avoid}h2,h3{break-after:avoid}}
  `;

  function blobDataUrl(blob) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(reader.result);
      reader.onerror = () => reject(new Error('图片读取失败，请重试下载。'));
      reader.onabort = () => reject(new Error('图片读取已中止，请重试下载。'));
      reader.readAsDataURL(blob);
    });
  }

  async function downloadOfflineNote() {
    if (!state.exportReady || state.exporting || !state.resultDownloadUrl) return;
    const signature = state.resultSignature;
    const downloadUrl = state.resultDownloadUrl;
    const title = $('result-title').textContent || '视频笔记';
    const note = $('result-body').cloneNode(true);
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 45000);
    let totalBytes = 0;
    state.exporting = true;
    updateDownloadState();
    try {
      // Copy the rendered note so diagrams remain visible without scripts.
      // Local images are embedded; the saved document needs no running server.
      await Promise.all([...note.querySelectorAll('img')].map(async (img) => {
        const url = outputUrl(img.getAttribute('src') || '');
        if (!url) throw new Error('图片路径无效，请重新打开笔记后下载。');
        const response = await fetch(url, { signal: controller.signal, credentials: 'same-origin', cache: 'no-store' });
        if (!response.ok) throw new Error('部分图片无法读取，请检查原图是否仍在本机。');
        const blob = await response.blob();
        if (!/^image\/(png|jpeg|webp|gif)$/i.test(blob.type)) throw new Error('图片格式无效，无法完成下载。');
        totalBytes += blob.size;
        if (blob.size > 32 * 1024 * 1024 || totalBytes > 64 * 1024 * 1024) throw new Error('图片总量过大，请下载 Markdown 压缩包。');
        img.src = await blobDataUrl(blob);
        img.removeAttribute('srcset');
        img.removeAttribute('loading');
        img.removeAttribute('decoding');
        img.removeAttribute('referrerpolicy');
      }));
      if (signature !== state.resultSignature) throw new Error('当前笔记已切换，请重新下载。');
      for (const element of note.querySelectorAll('script,iframe,object,embed,form')) element.remove();
      for (const element of note.querySelectorAll('*')) {
        for (const attribute of [...element.attributes]) {
          if (/^on/i.test(attribute.name)) element.removeAttribute(attribute.name);
        }
      }
      const documentHTML = `<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src data:; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<title>${escapeHTML(title)}</title><style>${offlineNoteStyles}</style></head><body><article>${note.innerHTML}</article></body></html>`;
      const objectUrl = URL.createObjectURL(new Blob([documentHTML], { type: 'text/html;charset=utf-8' }));
      const link = document.createElement('a');
      link.href = objectUrl;
      link.download = decodeURIComponent(downloadUrl.split('/').pop() || '视频笔记.md').replace(/\.md$/i, '') + '.html';
      document.body.append(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(objectUrl), 60000);
      toast('笔记已准备下载，图片和已显示的图表已包含');
    } catch (error) {
      controller.abort();
      toast(error.name === 'AbortError' ? '图片读取超时，请重试下载。' : error.message, true);
    } finally {
      clearTimeout(timeout);
      state.exporting = false;
      updateDownloadState();
    }
  }

  async function showResult(markdown, title, metadata, download, signature) {
    if (state.resultSignature === signature) return;
    state.resultSignature = signature;
    $('result-title').textContent = title || '视频笔记';
    $('result-meta').textContent = metadata || '';
    const safeUrl = outputUrl(download);
    state.resultDownloadUrl = safeUrl;
    state.exportReady = false;
    updateDownloadState();
    $('result-download').hidden = !safeUrl;
    $('result-download-bundle').hidden = !safeUrl || !/\.md$/i.test(safeUrl);
    if (safeUrl) {
      const relativeFile = decodeURIComponent(safeUrl.slice('/outputs/'.length));
      $('result-download-bundle').href = '/api/export?file=' + encodeURIComponent(relativeFile);
      $('result-download-bundle').download = decodeURIComponent(safeUrl.split('/').pop() || '视频笔记.md').replace(/\.md$/i, '') + '.zip';
    } else $('result-download-bundle').removeAttribute('href');
    $('result-card').hidden = false;
    const ready = await renderMarkdown(String(markdown || ''), safeUrl);
    if (signature === state.resultSignature) {
      state.exportReady = Boolean(ready && safeUrl);
      updateDownloadState();
    }
  }

  async function openTask(id, shouldScroll = false) {
    const { task } = await api(`/api/tasks/${encodeURIComponent(id)}`);
    if (!task) throw new Error('没有找到这项任务。');
    state.currentTaskId = id;
    state.currentTask = task;
    state.activeReport = null;
    renderProgress();
    if (state.view === 'chat' && !shouldScroll) return;
    if (task.status === 'completed' && task.markdown) {
      placeResult(state.view === 'chat');
      await showResult(task.markdown, task.title, `${formatTime(task.finished_at, true)} · ${task.model || 'DeepSeek'} · 保存在本机`, task.output_file, `task:${task.id}:${task.finished_at}`);
    } else {
      state.resultSignature = '';
      $('result-card').hidden = true;
    }
    if (shouldScroll) {
      if (state.view === 'chat') {
        if (task.status !== 'completed') toast(task.status === 'deleted' ? '这份内容已移入回收站，可在内容库恢复。' : task.stage || statusNames[task.status]);
        return;
      }
      await setView('video', false);
      ($('result-card').hidden ? $('progress-card') : $('result-card')).scrollIntoView({ behavior: 'smooth', block: 'start' });
    }
  }

  async function openReport(report) {
    const url = outputUrl(report?.url || report?.name);
    if (!url) throw new Error('这份笔记的文件地址不可用。');
    const response = await fetch(url, { credentials: 'same-origin', cache: 'no-store' });
    if (!response.ok) throw new Error('未能读取笔记文件，请刷新后重试。');
    const markdown = await response.text();
    state.activeReport = report;
    const inConversation = state.view === 'chat';
    placeResult(inConversation);
    await setView(inConversation ? 'chat' : 'video', false);
    await showResult(markdown, report.title || report.name, `${formatTime(report.modified_at, true)} · Markdown · 保存在本机`, url, `report:${url}:${report.modified_at}`);
    if (!inConversation) $('result-card').scrollIntoView({ behavior: 'smooth', block: 'start' });
  }

  function renderTasks() {
    const active = state.tasks.filter((task) => activeStatuses.has(task.status));
    $('active-task-count').hidden = !active.length;
    $('active-task-count').textContent = active.length;
    renderHistory();
    renderChat();
    if (state.currentTaskId) {
      const latest = state.tasks.find((task) => task.id === state.currentTaskId);
      if (latest && state.currentTask) {
        state.currentTask = { ...state.currentTask, ...latest };
        renderProgress();
      }
    }
    ensurePolling();
  }
  function ensurePolling() {
    const hasActive = state.tasks.some((task) => activeStatuses.has(task.status)) || state.runs.some((run) => activeStatuses.has(run.status));
    const hasScheduled = state.jobs.some((job) => job.enabled);
    if (!hasActive && !hasScheduled) {
      clearTimeout(state.pollTimer);
      state.pollTimer = null;
      return;
    }
    if (!state.pollTimer && !state.polling) state.pollTimer = setTimeout(pollTasks, hasActive ? 3000 : 15000);
  }
  async function pollTasks() {
    state.pollTimer = null;
    state.polling = true;
    try {
      const before = new Set(state.tasks.filter((task) => activeStatuses.has(task.status)).map((task) => task.id));
      const previousRuns = new Map(state.runs.map((run) => [run.id, run.status]));
      const [data, schedules] = await Promise.all([api('/api/tasks'), api('/api/schedules')]);
      state.jobs = schedules.jobs || [];
      state.runs = schedules.runs || [];
      renderSchedules();
      state.tasks = Array.isArray(data.tasks) ? data.tasks : [];
      const completed = state.tasks.filter((task) => before.has(task.id) && !activeStatuses.has(task.status));
      if (state.currentTaskId && before.has(state.currentTaskId) && !state.activeReport) await openTask(state.currentTaskId);
      renderTasks();
      if (completed.length || state.runs.some((run) => run.status === 'completed' && previousRuns.get(run.id) !== 'completed')) {
        const reports = await api('/api/reports');
        state.reports = reports.reports || [];
        renderReports();
        await refreshLibrary();
      }
      if (!state.connected) setConnection(true);
    } catch (error) {
      setConnection(false, error.message);
    } finally {
      state.polling = false;
      ensurePolling();
    }
  }

  function renderSchedules() {
    const jobs = state.jobs;
    $('schedule-count').textContent = jobs.length ? `${jobs.filter((job) => job.enabled).length} 项待执行` : '';
    if (!jobs.length) {
      $('schedule-list').innerHTML = emptyState('给下一项任务留个时间', '选择一个功能并安排时间，下次执行计划会显示在这里。', 'clock');
      return;
    }
    $('schedule-list').innerHTML = jobs.map((job) => `<article class="card schedule-item${job.enabled ? '' : ' is-disabled'}"><div class="schedule-item-icon">${icons('clock')}</div><div class="schedule-item-content"><div class="schedule-item-heading"><strong>${escapeHTML(capabilityTitle(job))}</strong><span class="status-pill ${job.enabled ? 'is-success' : ''}">${job.enabled ? '已安排' : '已结束 / 停用'}</span></div><span class="schedule-item-url" title="${escapeHTML(inputSummary(job))}">${escapeHTML(inputSummary(job) || '无需输入参数')}</span><div class="schedule-item-meta"><span>${escapeHTML(job.cadence === 'daily' ? `每天 ${job.daily_time || ''}` : '只执行一次')}</span><span>${job.enabled ? '下次执行' : '计划时间'} · ${escapeHTML(formatTime(job.due_at, true))}</span><span>任务 ${escapeHTML(job.id)}</span></div></div>${job.enabled ? `<button class="icon-button" data-disable-job="${escapeHTML(job.id)}" aria-label="停用${escapeHTML(capabilityTitle(job))}任务 ${escapeHTML(job.id)}" title="停用任务">${icons('trash')}</button>` : ''}</article>`).join('');
  }

  function historyItems() {
    const tasks = state.tasks.map((task) => ({ ...task, source: '即时任务', kind: 'task', timestamp: task.created_at, detail: task.error || `${task.stage || ''}${inputSummary(task) ? ` · ${inputSummary(task)}` : ''}`, capability_title: capabilityTitle(task) }));
    const runs = state.runs.map((run) => {
      const job = state.jobs.find((item) => item.id === run.job_id);
      const metadata = { ...job, ...run };
      return { ...run, title: capabilityTitle(metadata), capability_title: capabilityTitle(metadata), detail: `${inputSummary(metadata)}${run.detail ? ` · ${run.detail}` : ''}`, source: '定时任务', kind: 'schedule', timestamp: run.scheduled_at, output_file: run.output_path, error: ['failed', 'missed'].includes(run.status) ? run.detail : null };
    });
    return [...tasks, ...runs].sort((a, b) => Number(b.timestamp) - Number(a.timestamp));
  }
  function renderHistory() {
    const items = historyItems();
    const completed = items.filter((item) => item.status === 'completed').length;
    const active = items.filter((item) => activeStatuses.has(item.status)).length;
    const attention = items.filter((item) => ['failed', 'interrupted', 'missed'].includes(item.status)).length;
    $('history-stats').innerHTML = [
      { label: '已完成的任务', value: completed, icon: 'check' },
      { label: '正在处理', value: active, icon: 'clock' },
      { label: '需要留意', value: attention, icon: 'alert' },
    ].map((item) => `<div class="stat-card"><div class="section-icon">${icons(item.icon)}</div><div><strong>${item.value}</strong><span>${escapeHTML(item.label)}</span></div></div>`).join('');
    const visible = items.filter((item) => state.historyFilter === 'all' || state.historyFilter === 'active' && activeStatuses.has(item.status) || state.historyFilter === 'completed' && item.status === 'completed' || state.historyFilter === 'failed' && ['failed', 'interrupted', 'missed'].includes(item.status));
    if (!visible.length) {
      $('history-list').innerHTML = emptyState(items.length ? '这个分类暂时没有记录' : '这里会记住每一次执行', items.length ? '切换分类，查看其他运行记录。' : '通过对话执行功能，或安排一个定时任务后，记录会显示在这里。', 'history');
      return;
    }
    $('history-list').innerHTML = `<div class="history-table-wrap"><table class="history-table"><thead><tr><th scope="col">任务内容</th><th scope="col">来源</th><th scope="col">状态</th><th scope="col">开始时间</th><th scope="col">操作</th></tr></thead><tbody>${visible.map((item) => {
      const title = item.kind === 'task' && item.title && item.title !== item.capability_title ? `${item.capability_title} · ${item.title}` : item.title || item.capability_title || '任务';
      const file = outputUrl(item.output_file);
      const report = state.reports.find((entry) => outputUrl(entry.url) === file);
      let action = item.kind === 'task' ? `<button class="text-button" data-open-task="${escapeHTML(item.id)}">${item.status === 'completed' ? '查看结果' : '查看详情'}${icons('arrow')}</button>` : file ? `<button class="text-button" data-open-run-file="${escapeHTML(file)}" data-run-title="${escapeHTML(report?.title || item.capability_title || '定时任务结果')}" data-run-time="${Number(item.finished_at || item.timestamp) || ''}">查看结果${icons('arrow')}</button>` : item.error ? `<button class="text-button" data-run-detail="${escapeHTML(item.error)}" data-run-title="${escapeHTML(statusNames[item.status] || '运行情况')}">查看原因${icons('arrow')}</button>` : '—';
      if (activeStatuses.has(item.status)) action += `<button class="text-button" ${item.kind === 'task' ? 'data-cancel-task' : 'data-cancel-run'}="${escapeHTML(item.id)}">取消执行</button>`;
      return `<tr><td><span class="history-task-title" title="${escapeHTML(title)}">${escapeHTML(title)}</span><span class="history-task-detail" title="${escapeHTML(item.detail || '')}">${escapeHTML(item.detail || '')}</span></td><td>${escapeHTML(item.source)}</td><td>${statusPill(item.status)}</td><td>${escapeHTML(formatTime(item.timestamp))}</td><td>${action}</td></tr>`;
    }).join('')}</tbody></table></div>`;
  }

  async function refreshSchedules() {
    const [data, capabilities] = await Promise.all([api('/api/schedules'), api('/api/capabilities')]);
    state.capabilities = Array.isArray(capabilities.capabilities) ? capabilities.capabilities : [];
    renderCapabilities();
    state.jobs = Array.isArray(data.jobs) ? data.jobs : [];
    state.runs = Array.isArray(data.runs) ? data.runs : [];
    renderSchedules();
    renderHistory();
  }

  async function refreshAll(showSuccess = false) {
    if (state.loading) return;
    state.loading = true;
    $('refresh-button').disabled = true;
    $('refresh-button').querySelector('.icon').classList.add('refreshing');
    try {
      const status = await api('/api/status');
      state.status = status;
      setConnection(true);
      renderStatus();
      const results = await Promise.allSettled([api('/api/tasks'), api('/api/reports'), api('/api/schedules'), api('/api/capabilities'), state.chatBusy ? Promise.resolve(null) : api('/api/conversations/current')]);
      if (results[0].status === 'fulfilled') state.tasks = results[0].value.tasks || [];
      if (results[1].status === 'fulfilled') state.reports = results[1].value.reports || [];
      if (results[2].status === 'fulfilled') {
        state.jobs = results[2].value.jobs || [];
        state.runs = results[2].value.runs || [];
      }
      if (results[3].status === 'fulfilled') {
        state.capabilities = results[3].value.capabilities || [];
        renderCapabilities();
      }
      if (results[4].status === 'fulfilled' && results[4].value && !state.chatBusy) applyConversation(results[4].value);
      renderReports();
      renderSchedules();
      renderTasks();
      if (!state.currentTaskId) {
        const activeTask = state.tasks.find((task) => activeStatuses.has(task.status));
        if (activeTask) await openTask(activeTask.id);
      } else if (!state.activeReport) await openTask(state.currentTaskId);
      const failed = results.find((result) => result.status === 'rejected');
      if (failed) toast(`部分记录暂时无法读取：${failed.reason.message}`, true);
      else if (showSuccess) toast('已刷新本地状态');
    } catch (error) {
      setConnection(false, error.message);
      if (!state.reports.length) $('recent-reports').innerHTML = emptyState('等待连接本地工作空间', '连接恢复后，你保存的笔记会显示在这里。', 'monitor');
    } finally {
      state.loading = false;
      $('refresh-button').disabled = false;
      $('refresh-button').querySelector('.icon').classList.remove('refreshing');
      ensurePolling();
    }
  }

  async function setView(view, reload = true) {
    if (!['chat', 'video', 'schedules', 'history', 'settings'].includes(view)) view = 'chat';
    state.view = view;
    const labels = { chat: '与 Agent 对话', video: '视频总结', schedules: '定时任务', history: '运行记录', settings: '模型设置' };
    document.querySelectorAll('.view').forEach((element) => { element.hidden = element.id !== `view-${view}`; });
    document.querySelectorAll('.nav-item').forEach((element) => {
      const selected = element.dataset.view === view;
      element.classList.toggle('is-active', selected);
      if (selected) element.setAttribute('aria-current', 'page'); else element.removeAttribute('aria-current');
    });
    $('breadcrumb-page').textContent = labels[view];
    document.title = `${labels[view]} · Agent 工作台`;
    history.replaceState(null, '', `#${view}`);
    const heading = $(`view-${view}`).querySelector('h1');
    heading?.focus({ preventScroll: true });
    if (reload) {
      try {
        const status = await api('/api/status');
        state.status = status;
        setConnection(true);
        renderStatus();
        if (view === 'schedules' || view === 'history') await refreshSchedules();
        if (view === 'chat') await Promise.all([refreshConversation(), refreshLibrary()]);
        if (view === 'history' || view === 'video') {
          const [tasks, reports] = await Promise.all([api('/api/tasks'), api('/api/reports')]);
          state.tasks = tasks.tasks || [];
          state.reports = reports.reports || [];
          renderReports();
          renderTasks();
        }
      } catch (error) { setConnection(false, error.message); }
    }
  }

  function validateVideoLink(input) {
    const value = String(input || '').trim();
    let url;
    try { url = new URL(value); } catch { throw new Error('请输入完整的视频链接，包含 https://。'); }
    const host = url.hostname.toLowerCase();
    const allowed = ['bilibili.com', 'www.bilibili.com', 'm.bilibili.com', 'b23.tv', 'youtube.com', 'www.youtube.com', 'm.youtube.com', 'youtu.be'];
    if (!['https:', 'http:'].includes(url.protocol) || !allowed.includes(host) || url.username || url.password) throw new Error('当前支持 B 站和 YouTube 的公开视频链接。');
    return value;
  }

  $('summary-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    inlineError('summary-error');
    try {
      placeResult(false);
      const url = validateVideoLink($('video-url').value);
      state.submitting = true;
      updateSubmitState();
      const data = await api('/api/summarize', 'POST', { url, include_frames: $('include-frames').checked });
      state.currentTaskId = data.task.id;
      state.currentTask = data.task;
      state.activeReport = null;
      state.tasks = [data.task, ...state.tasks.filter((task) => task.id !== data.task.id)];
      state.resultSignature = '';
      $('result-card').hidden = true;
      renderProgress();
      renderTasks();
      toast('已开始整理，处理进度会显示在下方');
    } catch (error) { inlineError('summary-error', error.message); }
    finally { state.submitting = false; updateSubmitState(); }
  });
  $('video-url').addEventListener('input', () => { $('clear-url').hidden = !$('video-url').value; inlineError('summary-error'); });
  $('clear-url').addEventListener('click', () => { $('video-url').value = ''; $('clear-url').hidden = true; $('video-url').focus(); });

  $('settings-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    inlineError('settings-error');
    $('settings-saved').textContent = '';
    const key = $('api-key').value.trim();
    if (!key && !state.status?.api_key_configured) { inlineError('settings-error', '请先填写 DeepSeek API Key。'); $('api-key').focus(); return; }
    const payload = { model: $('model-select').value };
    if (key) payload.api_key = key;
    const button = $('settings-submit');
    button.disabled = true;
    try {
      const status = await api('/api/config', 'POST', payload);
      state.status = status;
      $('api-key').value = '';
      $('api-key').type = 'password';
      $('toggle-key-visibility').setAttribute('aria-pressed', 'false');
      $('toggle-key-visibility').setAttribute('aria-label', '显示 API Key');
      $('toggle-key-visibility').innerHTML = icons('eye');
      renderStatus();
      $('model-select').value = status.model;
      $('settings-saved').textContent = '已保存到本机';
      toast('设置已保存，新任务会使用所选模型');
    } catch (error) { inlineError('settings-error', error.message); }
    finally { button.disabled = !state.connected; }
  });
  $('toggle-key-visibility').addEventListener('click', () => {
    const visible = $('api-key').type === 'password';
    $('api-key').type = visible ? 'text' : 'password';
    $('toggle-key-visibility').setAttribute('aria-label', visible ? '隐藏 API Key' : '显示 API Key');
    $('toggle-key-visibility').setAttribute('aria-pressed', String(visible));
    $('toggle-key-visibility').innerHTML = icons(visible ? 'eye-off' : 'eye');
  });

  function updateScheduleTime() {
    const daily = $('schedule-cadence').value === 'daily';
    $('schedule-time').type = daily ? 'time' : 'datetime-local';
    $('schedule-time-label').textContent = daily ? '每日执行时间' : '执行日期与时间';
    if (daily) $('schedule-time').value = '09:00';
    else {
      const tomorrow = new Date(Date.now() + 24 * 60 * 60 * 1000);
      const parts = new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit' }).formatToParts(tomorrow);
      const values = Object.fromEntries(parts.map((part) => [part.type, part.value]));
      $('schedule-time').value = `${values.year}-${values.month}-${values.day}T09:00`;
    }
  }
  $('schedule-cadence').addEventListener('change', updateScheduleTime);
  $('schedule-capability').addEventListener('change', () => { renderScheduleParameters(); inlineError('schedule-error'); });
  $('schedule-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    inlineError('schedule-error');
    const button = $('schedule-submit');
    try {
      const capability = $('schedule-capability').value;
      const parameters = readScheduleParameters();
      const cadence = $('schedule-cadence').value;
      const time = $('schedule-time').value.replace('T', ' ');
      if (!time) throw new Error('请选择执行时间。');
      button.disabled = true;
      await api('/api/schedules', 'POST', { capability, parameters, cadence, time });
      await refreshSchedules();
      toast('定时任务已安排');
    } catch (error) { inlineError('schedule-error', error.message); }
    finally { button.disabled = !state.connected; }
  });

  document.addEventListener('click', async (event) => {
    const nav = event.target.closest('[data-view], [data-go]');
    if (nav) { await setView(nav.dataset.view || nav.dataset.go); return; }
    const filter = event.target.closest('[data-filter]');
    if (filter) {
      state.historyFilter = filter.dataset.filter;
      document.querySelectorAll('[data-filter]').forEach((button) => { const active = button === filter; button.classList.toggle('is-active', active); button.setAttribute('aria-pressed', String(active)); });
      renderHistory();
      return;
    }
    const reportButton = event.target.closest('[data-report-index]');
    const taskButton = event.target.closest('[data-open-task]');
    const runButton = event.target.closest('[data-open-run-file]');
    const detailButton = event.target.closest('[data-run-detail]');
    const disableButton = event.target.closest('[data-disable-job]');
    try {
      const artifactOpen = event.target.closest('[data-artifact-open]');
      const artifactDelete = event.target.closest('[data-artifact-delete]');
      const artifactRestore = event.target.closest('[data-artifact-restore]');
      const conversation = event.target.closest('[data-conversation]');
      if (conversation) { applyConversation(await api(`/api/conversations/${encodeURIComponent(conversation.dataset.conversation)}/select`, 'POST', {})); inlineError('chat-error'); return; }
      const cancelTask = event.target.closest('[data-cancel-task]');
      if (cancelTask) { await api(`/api/tasks/${encodeURIComponent(cancelTask.dataset.cancelTask)}/cancel`, 'POST', {}); await refreshAll(); toast('任务已取消'); return; }
      const cancelRun = event.target.closest('[data-cancel-run]');
      if (cancelRun) { await api(`/api/schedule-runs/${encodeURIComponent(cancelRun.dataset.cancelRun)}/cancel`, 'POST', {}); await refreshAll(); toast('已取消本次执行，后续计划保留'); return; }
      if (artifactOpen) { await openArtifact(artifactOpen.dataset.artifactOpen); return; }
      if (artifactDelete || artifactRestore) {
        const button = artifactDelete || artifactRestore;
        button.disabled = true;
        try {
          await api(artifactDelete ? '/api/artifacts/delete' : '/api/artifacts/restore', 'POST', artifactDelete ? {artifact_ids:[button.dataset.artifactDelete]} : {trash_ids:[button.dataset.artifactRestore]});
          await Promise.all([refreshLibrary(), refreshAll()]);
          toast(artifactDelete ? '已移入回收站，可以恢复' : '内容已恢复');
        } finally { button.disabled = false; }
        return;
      }
      if (event.target.closest('[data-close-result]')) { $('result-dialog').close(); return; }
      if (reportButton) { await openReport(state.reports[Number(reportButton.dataset.reportIndex)]); return; }
      if (taskButton) { await openTask(taskButton.dataset.openTask, true); return; }
      if (runButton) { await openReport({ url: runButton.dataset.openRunFile, title: runButton.dataset.runTitle, modified_at: Number(runButton.dataset.runTime) }); return; }
      if (detailButton) {
        $('run-detail-title').textContent = detailButton.dataset.runTitle || '运行情况';
        $('run-detail-content').textContent = detailButton.dataset.runDetail;
        $('run-detail-dialog').showModal();
        return;
      }
      if (event.target.closest('[data-close-detail]')) { $('run-detail-dialog').close(); return; }
      if (disableButton) {
        disableButton.disabled = true;
        await api(`/api/schedules/${encodeURIComponent(disableButton.dataset.disableJob)}`, 'DELETE', {});
        await refreshSchedules();
        toast('任务已停用，已有记录仍可查看');
        return;
      }
      if (event.target.closest('[data-retry-task]') && state.currentTask) {
        $('video-url').value = state.currentTask.url || '';
        $('include-frames').checked = Boolean(state.currentTask.include_frames);
        $('clear-url').hidden = !$('video-url').value;
        $('video-url').focus();
        $('summary-form').scrollIntoView({ behavior: 'smooth', block: 'center' });
      }
    } catch (error) {
      toast(error.message, true);
      if (disableButton) disableButton.disabled = false;
    }
  });
  document.querySelector('.brand').addEventListener('click', (event) => { event.preventDefault(); setView('chat'); });
  $('library-refresh').addEventListener('click', () => refreshLibrary().catch(error => toast(error.message, true)));
  $('result-dialog').addEventListener('close', () => $('video-result-home').after($('result-card')));
  $('chat-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    if (state.chatBusy) return;
    const message = $('chat-input').value.trim();
    if (!message) return;
    await submitChat(message);
  });
  $('chat-new').addEventListener('click', async () => {
    try { applyConversation(await api('/api/conversations/new', 'POST', {})); await refreshConversations(); inlineError('chat-error'); }
    catch (error) { inlineError('chat-error', error.message); }
  });
  $('chat-stop').addEventListener('click', async () => {
    if (!state.chatRunId) return;
    try { await api(`/api/chat/runs/${encodeURIComponent(state.chatRunId)}/stop`, 'POST', {}); $('chat-stage').textContent = '正在停止…'; }
    catch (error) { inlineError('chat-error', error.message); }
  });
  $('chat-retry').addEventListener('click', () => submitChat(null, true));
  for (const action of ['rename','delete']) $('conversation-' + action).addEventListener('click', () => {
    state.conversationEdit = {action, id:state.conversationId};
    $('conversation-dialog-title').textContent = action === 'rename' ? '重命名对话' : '删除这段对话？';
    $('conversation-dialog-help').textContent = action === 'delete' ? '聊天记录将删除。已生成的报告和后台任务会保留。' : '为这段对话起一个方便查找的名称。';
    $('conversation-name').hidden = action === 'delete'; $('conversation-name').required = action === 'rename';
    $('conversation-name').value = $('conversation-title').textContent;
    inlineError('conversation-edit-error'); $('conversation-dialog').showModal();
  });
  $('conversation-edit-cancel').addEventListener('click', () => $('conversation-dialog').close());
  $('conversation-edit-form').addEventListener('submit', async event => {
    event.preventDefault(); const edit = state.conversationEdit;
    try {
      const data = await api(`/api/conversations/${encodeURIComponent(edit.id)}${edit.action === 'rename' ? '/rename' : ''}`, edit.action === 'rename' ? 'POST' : 'DELETE', edit.action === 'rename' ? {title:$('conversation-name').value} : {});
      applyConversation(data); await refreshConversations(); $('conversation-dialog').close();
    } catch (error) { inlineError('conversation-edit-error', error.message); }
  });
  $('chat-input').addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) { event.preventDefault(); $('chat-form').requestSubmit(); }
  });
  $('refresh-button').addEventListener('click', () => refreshAll(true));
  $('result-download').addEventListener('click', downloadOfflineNote);
  $('reconnect-button').addEventListener('click', () => refreshAll());
  window.addEventListener('hashchange', () => setView(location.hash.slice(1)));
  window.addEventListener('pagehide', () => { clearTimeout(state.pollTimer); state.pollTimer = null; });
  window.addEventListener('pageshow', ensurePolling);
  updateScheduleTime();
  updateSubmitState();
  setView(location.hash.slice(1) || 'chat', false);
  refreshAll().then(() => Promise.all([refreshLibrary(),refreshConversations()])).catch(error => toast(error.message, true));
})();
