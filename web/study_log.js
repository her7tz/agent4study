/* Only status and approval URLs enter the browser; the local service retains the credential. */
window.StudyLogConnection = (() => {
  let api, timer, current = {}, revision = 0, refreshRequest = null, pendingUntil = 0, pendingCode = '';
  let busy = false, failures = 0, pageActive = true, initialized = false;
  const node = id => document.getElementById(id);
  const siteOrigin = 'https://yanxibu-study-log.hz2nju.chatgpt.site';
  const retryDelays = [5000, 10000, 20000, 30000, 60000];
  const desktop = () => Boolean(window.webkit?.messageHandlers?.agentDesktop);
  const visible = () => pageActive && !document.hidden && !node('view-settings').hidden;

  function stop() { clearTimeout(timer); timer = null; }
  function safeSiteURL(value, approval = false) {
    try {
      const url = new URL(value);
      if (url.origin !== siteOrigin || url.username || url.password || url.pathname !== '/agent-access' || url.hash) return '';
      if (approval && (!/^[a-f0-9]{32}$/.test(url.searchParams.get('request') || '') || [...url.searchParams.keys()].some(key => key !== 'request'))) return '';
      if (!approval && url.search) return '';
      return url.href;
    } catch { return ''; }
  }
  function setBusy(value) {
    busy = value;
    for (const id of ['study-log-connect', 'study-log-disconnect', 'study-log-refresh']) node(id).disabled = value;
  }
  function pendingExpired() {
    return current.status === 'pending' && pendingUntil <= Date.now();
  }
  function schedule(delay = 5000) {
    stop();
    if (!visible() || busy || current.status !== 'pending') return;
    // The deadline schedules a check. Only the website can confirm expiry:
    // approval may have reached it before the deadline.
    const remaining = pendingUntil - Date.now();
    timer = setTimeout(refresh, remaining > 0 ? Math.min(delay, remaining) : delay);
  }
  function show(data) {
    current = data;
    if (data.status === 'pending') {
      const deadline = Number(data.request_expires_at);
      if (!pendingUntil || pendingCode !== data.code) pendingUntil = Date.now() + 15 * 60 * 1000;
      if (Number.isFinite(deadline) && deadline > 0) pendingUntil = Math.min(pendingUntil, deadline * 1000);
      pendingCode = data.code;
    } else { pendingUntil = 0; pendingCode = ''; }
    const labels = { disconnected: '尚未连接', pending: '等待你在 Study Log 确认授权', connected: '已连接 · 只读', expired: '授权已过期', revoked: '授权已撤销' };
    node('study-log-state').textContent = labels[data.status] || '尚未连接';
    const expiry = Number(data.expires_at);
    node('study-log-code').textContent = data.status === 'pending' ? '核对连接码：' + (data.code || '请检查连接') : data.status === 'connected' && Number.isFinite(expiry) && expiry > 0 ? '有效至 ' + new Date(expiry * 1000).toLocaleDateString('zh-CN', { timeZone: 'Asia/Shanghai' }) : '';
    node('study-log-connect').hidden = ['pending', 'connected'].includes(data.status);
    node('study-log-disconnect').hidden = !['pending', 'connected'].includes(data.status);
    node('study-log-disconnect').textContent = data.status === 'pending' ? '取消连接' : '撤销并断开';
    const approvalURL = data.status === 'pending' ? safeSiteURL(data.authorization_url, true) : '';
    node('study-log-approve').hidden = !approvalURL;
    if (approvalURL) node('study-log-approve').href = approvalURL;
    else node('study-log-approve').removeAttribute('href');
    node('study-log-manage').href = safeSiteURL(data.management_url) || siteOrigin + '/agent-access';
    node('study-log-hint').textContent = data.status === 'pending' ? '请在网站登录并确认只读授权。若授权页面未打开，点击“重新打开授权页面”。' : data.status === 'connected' ? '连接凭证已由本地服务保存，之后可直接在对话中查询记录、分析进度或生成周报。' : '连接时将自动打开 Study Log；请使用现有 ChatGPT 登录并确认一次只读授权。';
    stop();
    if (data.status === 'pending') schedule();
  }
  async function refresh() {
    stop();
    if (!api || !visible() || busy) return;
    if (refreshRequest) return refreshRequest.promise;
    const request = { version: revision, promise: null };
    refreshRequest = request;
    request.promise = (async () => {
      try {
        const data = await api('/api/study-log');
        if (request.version !== revision) return;
        failures = 0;
        node('study-log-error').textContent = '';
        show(data);
      } catch (error) {
        if (request.version !== revision) return;
        failures += 1;
        const unconfirmed = pendingExpired() ? ' 连接码可能已过期，尚未确认网站授权状态。' : '';
        node('study-log-error').textContent = error.message + unconfirmed + (current.status === 'pending' && failures <= retryDelays.length ? ' 将自动重试。' : ' 请点击“检查连接”重试。');
        if (failures <= retryDelays.length) schedule(retryDelays[failures - 1]);
      } finally { if (refreshRequest === request) refreshRequest = null; }
    })();
    return request.promise;
  }
  function reserveApprovalWindow() {
    if (desktop()) return null;
    // Reserve while handling the click so normal browser popup rules are respected.
    try {
      const popup = window.open('about:blank', '_blank');
      if (popup) popup.opener = null;
      return popup;
    } catch { return null; }
  }
  function openApproval(data, popup) {
    const url = safeSiteURL(data.authorization_url, true);
    if (data.status !== 'pending' || !url) { if (popup && !popup.closed) popup.close(); return; }
    if (popup && !popup.closed) { popup.location.replace(url); return; }
    // The desktop wrapper sends external target=_blank links to the system browser.
    if (desktop()) node('study-log-approve').click();
  }
  async function action(name, popup = null) {
    if (busy) { if (popup && !popup.closed) popup.close(); return; }
    stop();
    revision += 1;
    refreshRequest = null;
    setBusy(true);
    node('study-log-error').textContent = '';
    try {
      const data = await api('/api/study-log/' + name, 'POST', {});
      failures = 0;
      show(data);
      if (name === 'connect') openApproval(data, popup);
    } catch (error) {
      if (popup && !popup.closed) popup.close();
      node('study-log-error').textContent = error.message;
    } finally { setBusy(false); schedule(); }
  }
  function resume() {
    if (!visible()) { stop(); return; }
    failures = 0;
    refresh();
  }
  function init(request) {
    api = request;
    if (initialized) return;
    initialized = true;
    node('study-log-connect').addEventListener('click', () => action('connect', reserveApprovalWindow()));
    node('study-log-disconnect').addEventListener('click', () => action('disconnect'));
    node('study-log-refresh').addEventListener('click', () => { failures = 0; refresh(); });
    new MutationObserver(resume).observe(node('view-settings'), { attributes: true, attributeFilter: ['hidden'] });
    document.addEventListener('visibilitychange', resume);
    window.addEventListener('pagehide', () => { pageActive = false; stop(); });
    window.addEventListener('pageshow', () => { pageActive = true; resume(); });
  }
  return { init, refresh };
})();
