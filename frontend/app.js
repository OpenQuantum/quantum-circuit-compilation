import {ACTIVE, TASKS, STATUS, escapeHTML as esc, score, bytes, timestamp, badge, approvalAllowed, submissionAllowed, artifactURL, errorMessage, orderedMessages, captureChatContext, chatContextMatches} from './model.js';

const $ = id => document.getElementById(id);
const H = '/api/human/v1';
const Q = '/api/quantum/v1';
const state = {user: null, csrf: '', summary: null, view: 'experiments', lists: {experiments: [], candidates: [], packages: []}, selected: null, detail: null, interaction: null, run: null, conversations: [], conversationCursor: null, agents: [], generation: 0, refreshing: false, sending: false, pendingInput: null, timer: null, chatTimer: null, chatRefreshing: false, chatVersion: 0, logName: 'verify.stdout', logOffset: 0, logText: '', logLoaded: false, dialogPackage: null, uncertainSubmissions: new Set(), approving: false, submitting: false};
const viewText = {experiments: ['实验记录', '从候选电路到独立验证，跟踪每一步结果。', '全部实验'], candidates: ['候选电路', '查看 Agent 创建的不可变源码快照与实验关联。', '全部候选'], packages: ['提交审核', '检查本地验证结果，批准并提交确定的文件。', '全部提交包']};
let authConfig = null;

async function api(path, options = {}) {
  const generation = state.generation;
  const headers = {'Accept': 'application/json', ...(options.body ? {'Content-Type': 'application/json', 'X-CSRF-Token': state.csrf} : {}), ...options.headers};
  let response;
  try { response = await fetch(path, {credentials: 'same-origin', signal: AbortSignal.timeout(options.timeout || 20000), ...options, headers}); }
  catch (error) { throw new Error(error.name === 'TimeoutError' ? '请求超时。操作结果可能尚未返回，请刷新核对状态。' : '连接中断，请检查本地服务。'); }
  let data;
  try { data = await response.json(); } catch { throw new Error(`服务返回了非 JSON 响应（${response.status}）。`); }
  if (!response.ok) {
    const error = new Error(errorMessage(data, `请求失败（${response.status}）`));
    error.status = response.status;
    if (response.status === 401 && state.user && generation === state.generation) resetSession('登录已过期，请重新登录。');
    throw error;
  }
  return data;
}
function post(path, body, options) { return api(path, {method: 'POST', body: JSON.stringify(body), ...options}); }
function showMessage(id, text) { $(id).textContent = text || ''; $(id).hidden = !text; }
function notify(text) { showMessage('notice', text); }
function resetSession(message = '') {
  state.generation++; state.chatVersion++; clearTimeout(state.timer); clearTimeout(state.chatTimer);
  state.user = null; state.csrf = ''; state.interaction = null; state.run = null; state.pendingInput = null;
  state.summary = null; state.selected = null; state.detail = null; state.conversations = []; state.conversationCursor = null; state.agents = [];
  state.lists = {experiments: [], candidates: [], packages: []};
  state.dialogPackage = null; state.uncertainSubmissions.clear(); state.refreshing = false; state.chatRefreshing = false;
  $('approval-dialog').close(); $('submit-dialog').close();
  $('workspace').hidden = true; $('login-view').hidden = false; $('logout').hidden = true;
  $('account-name').textContent = ''; $('chat-input').value = '';
  showMessage('login-message', message);
}
async function init() {
  $('retry-init').hidden = true;
  try {
    authConfig = await api(`${H}/client-config`);
    $('login-form').hidden = !authConfig.developmentLoginEnabled;
    $('oidc-login').hidden = !!authConfig.developmentLoginEnabled;
    $('access-code-field').hidden = !authConfig.developmentLoginRequiresCode;
    $('access-code').required = !!authConfig.developmentLoginRequiresCode;
    try { await signedIn(await api(`${H}/me`)); }
    catch (error) { if (error.status === 401) showMessage('login-message', authConfig.developmentLoginEnabled ? '登录后，控制台将显示你有权访问的实验。' : '使用统一登录后自动返回工作台。'); else throw error; }
  } catch (error) { showMessage('login-message', error.message); $('retry-init').hidden = false; }
}
async function signedIn(me) {
  state.user = me.user; state.csrf = me.csrfToken; state.generation++;
  $('account-name').textContent = me.user.displayName || me.user.userId;
  $('logout').hidden = false; $('login-view').hidden = true; $('workspace').hidden = false;
  showMessage('world-error', ''); showMessage('notice', ''); showMessage('chat-error', '');
  renderMetrics(); renderRecords(false); renderMessages([]);
  await Promise.allSettled([refresh(), loadChatDirectory()]);
}
$('login-form').addEventListener('submit', async event => {
  event.preventDefault(); const button = event.submitter; button.disabled = true;
  try {
    const userId = $('login-user').value.trim();
    await post(`${H}/auth/development`, {userId, displayName: userId, ...(authConfig?.developmentLoginRequiresCode ? {accessCode: $('access-code').value} : {})});
    $('access-code').value = ''; await signedIn(await api(`${H}/me`));
  } catch (error) { showMessage('login-message', error.message); }
  finally { button.disabled = false; }
});
$('logout').addEventListener('click', async () => {
  try { await post(`${H}/auth/logout`, {}); resetSession('已退出。后台已接收的任务继续执行。'); }
  catch (error) { notify(error.message); }
});
$('retry-init').addEventListener('click', init);

function renderMetrics() {
  const s = state.summary;
  const running = Object.entries(s?.experiments_by_status || {}).filter(([status]) => ['queued', 'running', 'cancelling'].includes(status)).reduce((sum, [, value]) => sum + value, 0);
  const waiting = s?.packages_by_status?.awaiting_approval || 0;
  const metrics = [['本地最佳得分', score(s?.best_local_score), '已构建提交包 · 本地估算'], ['对照最高分', score(s?.configured_best), '配置参考值 · 非实时榜单'], ['执行中实验', s ? running : '—', `${s?.counts?.experiments ?? '—'} 个实验记录`], ['待人工审核', s ? waiting : '—', '批准后需单独发起提交']];
  $('metrics').innerHTML = metrics.map(([label, value, hint]) => `<div class="metric"><div class="metric-label">${label}</div><div class="metric-value">${esc(value)}</div><div class="metric-hint">${esc(hint)}</div></div>`).join('');
  $('approval-count').textContent = waiting; $('approval-count').hidden = !waiting;
  if (s) { $('world-name').textContent = s.world_id || 'Quantum World'; $('demo-label').hidden = s.transport_mode !== 'demo'; }
}
function renderRecords(loaded = true) {
  const view = state.view, task = $('task-filter').value;
  const items = state.lists[view].filter(item => task === 'all' || item.task === task);
  $('record-count').textContent = loaded ? `${items.length} 条记录` : '等待数据';
  if (!items.length) {
    const titles = {experiments: '还没有实验记录', candidates: '还没有候选电路', packages: '还没有待审提交包'};
    const descriptions = {experiments: '向右侧 Agent 描述你的目标，开始创建并验证候选电路。', candidates: 'Agent 创建候选后，源码快照与文件指纹会出现在这里。', packages: '完成三项完整验证并构建提交包后，即可在此审核。'};
    $('records').innerHTML = `<div class="empty"><div class="empty-mark" aria-hidden="true">${view === 'packages' ? '▣' : '⌘'}</div><h3>${loaded ? task === 'all' ? titles[view] : '没有符合筛选条件的记录' : '正在连接 Quantum World'}</h3><p>${loaded ? descriptions[view] : '数据会从你的实验环境加载。'}</p>${loaded && view === 'experiments' ? '<button class="text-button" data-prompt="请查看当前比赛任务与已有候选，先给出可验证的优化方案。">让 Agent 分析任务 →</button>' : ''}</div>`;
    return;
  }
  const columns = view === 'experiments' ? ['任务 / 实验', '状态', '验证模式', '任务成本', '创建时间'] : view === 'candidates' ? ['任务 / 候选', '入口文件', '文件数', '创建时间'] : ['提交包', '状态', '本地估分', '大小', '创建时间'];
  $('records').innerHTML = `<div class="table-scroll"><table><thead><tr>${columns.map(text => `<th scope="col">${text}</th>`).join('')}</tr></thead><tbody>${items.map(item => {
    const title = view === 'packages' ? 'Submission package' : TASKS[item.task] || item.task;
    const first = `<td><button class="record-title" data-select="${esc(item.id)}">${esc(title)} <span aria-hidden="true">↗</span><span class="record-sub record-id">${esc(item.id)}</span></button></td>`;
    const rest = view === 'experiments' ? `<td>${badge(item.status)}</td><td>${item.mode === 'full' ? '完整验证' : '快速诊断'}</td><td class="mono">${score(item.result?.task_cost)}${item.mode !== 'full' ? '<span class="record-sub">不可用于提交</span>' : ''}</td>` : view === 'candidates' ? `<td class="mono">${esc(item.entrypoint)}</td><td>${Object.keys(item.files || {}).length}</td>` : `<td>${badge(item.status)}</td><td class="mono">${score(item.local_score)}</td><td>${bytes(item.bytes)}</td>`;
    return `<tr class="${state.selected === item.id ? 'selected' : ''}">${first}${rest}<td>${timestamp(item.created_at)}</td></tr>`;
  }).join('')}</tbody></table></div>`;
}
function field(label, value) { return `<div><span class="field-label">${esc(label)}</span><span class="field-value">${esc(value ?? '—')}</span></div>`; }
function artifactsHTML(artifacts = []) {
  if (!artifacts.length) return '<p class="muted small">暂无可下载产物。</p>';
  return `<ul class="artifacts">${artifacts.map(a => `<li>${artifactURL(a) ? `<a href="${esc(artifactURL(a))}" download>${esc(a.name)} ↓</a>` : `<span>${esc(a.name)}</span>`}<span class="muted">${bytes(a.size_bytes)}</span></li>`).join('')}</ul>`;
}
function renderDetail() {
  const item = state.detail;
  $('detail').hidden = !item;
  if (!item) return;
  const view = state.view;
  let content = `<div class="detail-heading"><div><h2>${view === 'packages' ? '提交包详情' : view === 'candidates' ? '候选源码快照' : '实验详情'} ${item.status ? badge(item.status) : ''}</h2><span class="record-id">${esc(item.id)}</span></div><button class="icon-button" data-close-detail aria-label="关闭详情">×</button></div>`;
  if (view === 'experiments') {
    content += `<div class="detail-grid">${field('任务', TASKS[item.task] || item.task)}${field('验证模式', item.mode === 'full' ? '完整验证' : '快速诊断（不可提交）')}${field('任务成本', score(item.result?.task_cost))}${field('候选', item.candidate_id)}${field('创建时间', timestamp(item.created_at))}${field('执行端', item.sender_id)}</div>`;
    if (item.error) content += `<p class="error-text">${esc(item.error.message || item.error.code || JSON.stringify(item.error))}</p>`;
    if (['queued', 'running'].includes(item.status)) content += `<div class="detail-actions"><button class="button secondary" data-cancel-experiment ${state.summary?.read_only ? 'disabled' : ''}>取消此实验</button>${state.summary?.read_only ? '<span class="muted small">当前为只读预览</span>' : ''}</div>`;
    content += '<h3>实验产物</h3>' + artifactsHTML(item.artifacts);
    content += `<div class="log-toolbar"><h3>运行日志</h3><label class="sr-only" for="log-name">日志文件</label><select id="log-name">${['verify.stdout','verify.stderr','generator.stdout','generator.stderr','preflight.stdout','preflight.stderr'].map(name => `<option ${name === state.logName ? 'selected' : ''}>${name}</option>`).join('')}</select></div><pre id="log-text" class="logs"></pre><p id="log-note" class="log-note"></p>`;
  } else if (view === 'candidates') {
    content += `<div class="detail-grid">${field('任务', TASKS[item.task] || item.task)}${field('入口文件', item.entrypoint)}${field('创建时间', timestamp(item.created_at))}</div><p class="detail-note">${esc(item.description || '未提供候选说明。')}</p><h3>源码文件</h3>${artifactsHTML(item.artifacts)}`;
    if (item.files) content += `<details><summary>查看文件指纹</summary><pre class="detail-json">${esc(JSON.stringify(item.files, null, 2))}</pre></details>`;
  } else {
    content += `<div class="detail-grid">${field('本地估分', score(item.local_score))}${field('构建时对照分', score(item.configured_best))}${field('提交包大小', bytes(item.bytes))}${field('创建时间', timestamp(item.created_at))}${field('审批人', item.approval?.operator)}${field('审批到期', timestamp(item.approval?.expires_at))}</div><span class="field-label">SHA-256 · 审批绑定此文件指纹</span><code class="hash-block">${esc(item.sha256)}</code>`;
    content += `<h3>关联实验</h3><ul class="artifacts">${(item.experiments || []).map(id => `<li><button class="text-button mono" data-open-experiment="${esc(id)}">${esc(id)} ↗</button></li>`).join('')}</ul>${artifactsHTML(item.artifacts)}`;
    if (item.status === 'awaiting_approval') content += `<div class="detail-actions"><button class="primary" data-review ${approvalAllowed(state.summary, item) ? '' : 'disabled'}>审核此提交包 →</button></div>${!state.summary?.can_approve ? '<p class="muted small">当前账号没有审批权限。</p>' : ''}`;
    if (item.status === 'approved') content += `<div class="detail-actions"><button class="primary" data-submit ${submissionAllowed(state.summary, item) && !state.uncertainSubmissions.has(item.id) ? '' : 'disabled'}>提交到 Kaggle →</button></div><p class="muted small">${submissionAllowed(state.summary, item) ? '提交前将再次核对实时最高分与文件指纹。' : '审批已过期或当前账号无提交权限，请重新核对。'}</p>`;
    if (state.uncertainSubmissions.has(item.id)) content += '<p class="error-text">上次提交请求未返回确认结果。请先核对服务端状态，勿直接重复提交。</p><button class="button secondary" data-reconcile-package>核对提交状态</button>';
    if (item.status === 'unknown') content += '<p class="error-text">上传结果尚未确认。请先到 Kaggle 核对记录，系统不会自动重试。</p>';
    if (item.status === 'submitting') content += '<p class="detail-note">提交请求正在处理，请等待状态更新。</p>';
    if (item.receipt) content += `<details open><summary>提交回执</summary><pre class="detail-json">${esc(JSON.stringify(item.receipt, null, 2))}</pre></details>`;
    if (item.error) content += `<p class="error-text">${esc(typeof item.error === 'string' ? item.error : item.error.message)}</p>`;
  }
  $('detail').innerHTML = content;
  if (view === 'experiments') renderLog();
}
function renderLog() {
  if (!$('log-text')) return;
  $('log-text').textContent = state.logText || (state.logLoaded ? '日志为空，等待实验产生输出。' : '正在读取日志…');
  $('log-note').textContent = `已读取 ${bytes(state.logOffset)} · 自动继续读取新增内容 · 页面最多保留末尾 128 KiB`;
}
async function updateLog(generation = state.generation) {
  if (state.view !== 'experiments' || !state.selected || !state.user) return;
  const id = state.selected, name = state.logName, offset = state.logOffset;
  try {
    const data = await api(`${Q}/experiments/${encodeURIComponent(id)}/logs?name=${encodeURIComponent(name)}&offset=${offset}&limit=16384`);
    if (generation !== state.generation || state.selected !== id || state.logName !== name) return;
    if (state.logOffset === offset) { state.logText = (state.logText + data.text).slice(-131072); state.logOffset = data.next_offset; state.logLoaded = true; renderLog(); }
  } catch (error) { if (generation === state.generation && state.selected === id && $('log-text')) { $('log-text').textContent = error.status === 404 ? '此日志尚未生成。' : error.message; } }
}
async function refresh() {
  if (!state.user || state.refreshing) return;
  const generation = state.generation;
  clearTimeout(state.timer); state.refreshing = true; $('refresh').disabled = true;
  try {
    const results = await Promise.all([api(`${Q}/summary`), ...['experiments', 'candidates', 'packages'].map(type => api(`${Q}/${type}`))]);
    if (generation !== state.generation) return;
    state.summary = results[0];
    ['experiments', 'candidates', 'packages'].forEach((type, i) => { state.lists[type] = results[i + 1].items || []; });
    renderMetrics(); renderRecords(); showMessage('world-error', '');
    $('refresh-status').textContent = `已同步 ${new Date().toLocaleTimeString('zh-CN', {hour12: false})}`;
    if (state.selected) {
      const record = state.lists[state.view].find(item => item.id === state.selected);
      state.detail = record || null; renderDetail(); await updateLog(generation);
    }
  } catch (error) {
    if (generation === state.generation) { showMessage('world-error', `Quantum World：${error.message}${state.summary ? ' 当前显示上次同步的数据。' : ''}`); $('refresh-status').textContent = '连接待恢复'; }
  } finally {
    if (generation === state.generation) { state.refreshing = false; $('refresh').disabled = false; if (state.user && !document.hidden) state.timer = setTimeout(refresh, 4000); }
  }
}
function changeView(view) {
  if (!viewText[view]) return;
  state.view = view; state.selected = null; state.detail = null; state.logText = ''; state.logOffset = 0;
  document.querySelectorAll('[data-view]').forEach(button => { button.classList.toggle('active', button.dataset.view === view); button.setAttribute('aria-current', button.dataset.view === view ? 'page' : 'false'); });
  const [title, description, list] = viewText[view]; $('page-title').textContent = title; $('page-description').textContent = description; $('list-title').textContent = list;
  $('task-filter').hidden = view === 'packages'; $('task-filter').value = 'all'; renderRecords(!!state.summary); renderDetail();
}
async function selectRecord(id) {
  state.selected = id; state.detail = state.lists[state.view].find(item => item.id === id) || null;
  state.logOffset = 0; state.logText = ''; state.logLoaded = false;
  renderRecords(); renderDetail();
  if (!state.detail) return;
  const view = state.view, generation = state.generation;
  if (view !== 'candidates') {
    try { const record = await api(`${Q}/${view}/${encodeURIComponent(id)}`); if (state.selected === id && state.view === view && generation === state.generation) { state.detail = record; renderDetail(); } }
    catch (error) { if (generation === state.generation) notify(error.message); }
  }
  await updateLog(generation);
}
document.querySelectorAll('[data-view]').forEach(button => button.addEventListener('click', () => changeView(button.dataset.view)));
$('refresh').addEventListener('click', refresh);
$('task-filter').addEventListener('change', () => renderRecords(!!state.summary));
$('records').addEventListener('click', event => { const item = event.target.closest('[data-select]'); if (item) selectRecord(item.dataset.select); const prompt = event.target.closest('[data-prompt]'); if (prompt) fillPrompt(prompt.dataset.prompt); });
$('detail').addEventListener('change', event => { if (event.target.id === 'log-name') { state.logName = event.target.value; state.logOffset = 0; state.logText = ''; state.logLoaded = false; renderLog(); updateLog(); } });
$('detail').addEventListener('click', async event => {
  if (event.target.closest('[data-close-detail]')) { state.selected = null; state.detail = null; renderRecords(); renderDetail(); }
  const experiment = event.target.closest('[data-open-experiment]');
  if (experiment) { changeView('experiments'); selectRecord(experiment.dataset.openExperiment); }
  const cancel = event.target.closest('[data-cancel-experiment]');
  if (cancel && state.detail && !state.summary?.read_only) {
    cancel.disabled = true;
    try { await post(`${Q}/experiments/${encodeURIComponent(state.detail.id)}/cancel`, {}); notify('已请求取消实验，最终状态以 World 返回为准。'); await refresh(); }
    catch (error) { notify(error.message); cancel.disabled = false; }
  }
  const reconcile = event.target.closest('[data-reconcile-package]');
  if (reconcile && state.detail) {
    reconcile.disabled = true; const id = state.detail.id;
    try {
      const pkg = await api(`${Q}/packages/${encodeURIComponent(id)}`);
      state.uncertainSubmissions.delete(id);
      if (state.detail?.id === id) { state.detail = pkg; renderDetail(); }
      notify(pkg.status === 'approved' ? '服务端仍显示已批准、未提交。请核对错误原因后，再手动决定是否提交。' : `服务端状态：${STATUS[pkg.status] || pkg.status}。`);
    } catch (error) { notify(error.message); reconcile.disabled = false; }
  }
  if (event.target.closest('[data-review]') && approvalAllowed(state.summary, state.detail)) openApproval(state.detail);
  if (event.target.closest('[data-submit]') && submissionAllowed(state.summary, state.detail) && !state.uncertainSubmissions.has(state.detail.id)) {
    state.dialogPackage = {...state.detail}; $('confirm-submit').disabled = false; $('submit-sha').textContent = state.detail.sha256; showMessage('submit-error', ''); $('submit-dialog').showModal();
  }
});
function openApproval(pkg) {
  state.dialogPackage = {...pkg}; $('approval-confirm').checked = false; $('confirm-approval').disabled = true;
  $('approval-content').innerHTML = `<p class="muted small record-id">${esc(pkg.id)}</p><div class="review-scores"><div><span>本地估分</span><strong>${score(pkg.local_score)}</strong></div><div><span>构建时配置参考分</span><strong>${score(pkg.configured_best)}</strong></div></div><p>审核绑定下方文件。请先检查关联实验及下载的产物，本地估分不等于 Kaggle 官方成绩。</p><code class="hash-block">${esc(pkg.sha256)}</code>`;
  showMessage('approval-error', ''); $('approval-dialog').showModal();
}
$('approval-confirm').addEventListener('change', () => { $('confirm-approval').disabled = state.approving || !$('approval-confirm').checked; });
$('close-approval').addEventListener('click', () => $('approval-dialog').close());
$('close-submit').addEventListener('click', () => $('submit-dialog').close());
$('confirm-approval').addEventListener('click', async () => {
  const pkg = state.dialogPackage;
  if (state.approving || !$('approval-confirm').checked || !approvalAllowed(state.summary, pkg)) return;
  state.approving = true; $('confirm-approval').disabled = true;
  try { await post(`${Q}/packages/${encodeURIComponent(pkg.id)}/approve`, {sha256: pkg.sha256}); $('approval-dialog').close(); notify('此提交包已批准。需要你再次点击「提交到 Kaggle」才能上传。'); await refresh(); }
  catch (error) { showMessage('approval-error', error.message); }
  finally { state.approving = false; $('confirm-approval').disabled = !$('approval-confirm').checked; }
});
$('confirm-submit').addEventListener('click', async () => {
  const pkg = state.dialogPackage;
  if (state.submitting || !submissionAllowed(state.summary, pkg) || state.uncertainSubmissions.has(pkg.id)) return;
  state.submitting = true; $('confirm-submit').disabled = true; $('confirm-submit').textContent = '正在提交…';
  try { await post(`${Q}/packages/${encodeURIComponent(pkg.id)}/submit`, {sha256: pkg.sha256}, {timeout: 120000}); $('submit-dialog').close(); notify('提交请求已处理，请查看提交包状态与回执。'); }
  catch (error) { state.uncertainSubmissions.add(pkg.id); notify(`${error.message} 请点击「核对提交状态」，不要重复上传。`); }
  finally { state.submitting = false; $('confirm-submit').textContent = '确认提交到 Kaggle'; $('submit-dialog').close(); await refresh();  }
});

function fillPrompt(text) { $('chat-input').value = text; $('chat-input').focus(); }
function renderMessages(messages) {
  const box = $('chat-messages'), nearBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 100;
  if (!messages.length) {
    box.innerHTML = '<div class="chat-empty"><h3>从一个研究目标开始</h3><p>把问题交给 Agent，实验与验证结果会同步到左侧工作台。</p><button class="suggestion" data-prompt="请分析当前比赛的三个任务与现有实现，给出下一轮优化计划。">分析三个比赛任务，寻找优化方向 ↗</button><button class="suggestion" data-prompt="请查看我的实验记录，比较已完成的完整验证结果，说明下一步最值得尝试的改进。">比较已完成实验，规划下一轮搜索 ↗</button></div>';
    return;
  }
  const markup = messages.map(message => `<article class="message ${message.role === 'user' ? 'user' : 'agent'}"><div class="message-label"><b>${message.role === 'user' ? '你' : 'Agent'}</b><span>${timestamp(message.createdAt)}${message.status === 'streaming' ? ' · 回复中' : ''}</span></div><div class="message-text">${(message.parts || []).map(part => part.type === 'text' ? esc(part.text) : `<span class="message-resource">附件：${esc(part.resourceId || '文件')}</span>`).join('\n')}</div>${message.error ? `<p class="error-text">${esc(message.error.message || '消息处理失败')}</p>` : ''}</article>`).join('');
  if (box.innerHTML !== markup) { box.innerHTML = markup; if (nearBottom) box.scrollTop = box.scrollHeight; }
}
function renderConversations() {
  $('conversation-select').innerHTML = '<option value="">新对话</option>' + state.conversations.map(row => `<option value="${esc(row.interactionSessionId)}" ${row.interactionSessionId === state.interaction?.interactionSessionId ? 'selected' : ''}>${esc(row.title || '未命名对话')}</option>`).join('');
  $('more-conversations').hidden = !state.conversationCursor;
}
async function loadConversations(more = false) {
  const generation = state.generation;
  const data = await api(`${H}/interactions?mode=conversation&limit=20${more && state.conversationCursor ? `&cursor=${encodeURIComponent(state.conversationCursor)}` : ''}`);
  if (generation !== state.generation) return;
  state.conversations = more ? [...new Map([...state.conversations, ...data.items].map(item => [item.interactionSessionId, item])).values()] : data.items;
  state.conversationCursor = data.nextCursor; renderConversations();
}
async function loadChatDirectory() {
  const generation = state.generation, chatVersion = state.chatVersion;
  try {
    const [agents] = await Promise.all([api(`${H}/agents`), loadConversations()]);
    if (generation !== state.generation) return;
    state.agents = agents.items || [];
    $('agent-select').innerHTML = '<option value="">自动选择</option>' + state.agents.map(agent => `<option value="${esc(agent.agentId)}">${esc(agent.name || agent.displayName || agent.agentId)}</option>`).join('');
    $('chat-connection').textContent = state.agents.length ? `${state.agents.length} 个已分配 Agent · 请求经 BFF 转发` : '暂无已分配 Agent · 可先保存任务';
    if (chatVersion === state.chatVersion && state.conversations[0]) await openConversation(state.conversations[0].interactionSessionId);
  } catch (error) { if (generation === state.generation) showMessage('chat-error', error.message); }
}
async function openConversation(id) {
  clearTimeout(state.chatTimer); state.chatVersion++; state.interaction = null; state.run = null; state.pendingInput = null;
  renderMessages([]); showMessage('chat-state', '正在加载对话…');
  const generation = state.generation, chatVersion = state.chatVersion;
  try {
    const interaction = await api(`${H}/interactions/${encodeURIComponent(id)}`);
    if (generation !== state.generation || chatVersion !== state.chatVersion) return;
    state.interaction = interaction; $('agent-select').value = interaction.agentId || ''; $('agent-select').disabled = true;
    renderConversations(); await refreshChat();
  } catch (error) { if (generation === state.generation) showMessage('chat-error', error.message); }
}
async function refreshChat() {
  if (!state.user || !state.interaction || state.chatRefreshing) return;
  clearTimeout(state.chatTimer); state.chatRefreshing = true;
  const generation = state.generation, id = state.interaction.interactionSessionId;
  try {
    const pages = [], seen = new Set(); let cursor = null;
    do {
      const data = await api(`${H}/interactions/${encodeURIComponent(id)}/messages?limit=100${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ''}`);
      if (generation !== state.generation || state.interaction?.interactionSessionId !== id) return;
      pages.push(data); cursor = data.nextCursor;
      if (cursor && seen.has(cursor)) throw new Error('消息分页游标重复，请重新加载对话。');
      if (cursor) seen.add(cursor);
    } while (cursor);
    renderMessages(orderedMessages(pages));
    const runs = pages.flatMap(page => page.runs || []).sort((a, b) => String(a.createdAt).localeCompare(String(b.createdAt)));
    const latest = runs.at(-1);
    const loadedRun = latest ? await api(`${H}/runs/${encodeURIComponent(latest.runId)}`) : null;
    if (generation !== state.generation || state.interaction?.interactionSessionId !== id) return;
    state.run = loadedRun;
    const run = state.run;
    showMessage('chat-state', run ? `${run.status === 'succeeded' ? 'Agent 已完成' : STATUS[run.status] || run.status}${run.waitingReason?.message ? ` · ${run.waitingReason.message}` : ''}${run.error?.message ? ` · ${run.error.message}` : ''}` : '尚未发送消息。');
  } catch (error) { if (generation === state.generation && state.interaction?.interactionSessionId === id) showMessage('chat-error', error.message); }
  finally { state.chatRefreshing = false; if (generation === state.generation && state.user && state.interaction && !document.hidden) state.chatTimer = setTimeout(refreshChat, 2500); }
}
$('more-conversations').addEventListener('click', async () => { try { await loadConversations(true); } catch (error) { showMessage('chat-error', error.message); } });
$('conversation-select').addEventListener('change', () => { const id = $('conversation-select').value; if (id) openConversation(id); else newConversation(); });
function newConversation() {
  clearTimeout(state.chatTimer); state.chatVersion++; state.interaction = null; state.run = null; state.pendingInput = null;
  $('agent-select').disabled = false; $('conversation-select').value = ''; $('send-chat').textContent = '发送 ↑'; $('chat-state').textContent = '';
  showMessage('chat-error', ''); renderMessages([]); $('chat-input').focus();
}
$('new-chat').addEventListener('click', newConversation);
$('chat-messages').addEventListener('click', event => { const prompt = event.target.closest('[data-prompt]'); if (prompt) fillPrompt(prompt.dataset.prompt); });
$('chat-input').addEventListener('keydown', event => { if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) { event.preventDefault(); if ($('chat-input').value.trim() && !state.sending) $('chat-form').requestSubmit(); } });
$('chat-form').addEventListener('submit', async event => {
  event.preventDefault(); if (state.sending || !state.user) return;
  const text = $('chat-input').value.trim(); if (!text) return;
  if (state.pendingInput && state.pendingInput.text !== text) { showMessage('chat-error', '上一条请求结果尚未确认。请先恢复原文重试同一请求，或重新打开对话核对后再发送新内容。'); return; }
  state.sending = true; $('send-chat').disabled = true; showMessage('chat-error', '');
  let context = captureChatContext(state);
  try {
    if (!state.interaction) {
      const agentId = $('agent-select').value || null;
      const interaction = await post(`${H}/interactions`, {mode: 'conversation', agentId, ...(state.summary?.world_id && state.summary.transport_mode !== 'demo' ? {worldId: state.summary.world_id} : {})});
      if (!chatContextMatches(state, context)) return;
      state.interaction = interaction;
      context = captureChatContext(state);
      $('agent-select').disabled = true;
    }
    const id = state.interaction.interactionSessionId;
    if (!state.pendingInput) {
      const append = state.run && ACTIVE.has(state.run.status);
      if (append && state.run.waitingReason) throw new Error('上一条任务正在等待 Agent 接入，请等待分配完成，或新建对话另存任务。');
      if (append && state.run.capabilities?.appendInput !== true) throw new Error('当前 Agent 不支持执行中追加输入，请等待这轮任务结束。');
      state.pendingInput = {text, path: append ? `${H}/runs/${encodeURIComponent(state.run.runId)}/inputs` : `${H}/interactions/${encodeURIComponent(id)}/runs`, body: {inputId: `console_${crypto.randomUUID()}`, parts: [{type: 'text', text}]}};
    }
    const pending = state.pendingInput;
    const ack = await post(pending.path, pending.body);
    if (!chatContextMatches(state, context)) return;
    state.pendingInput = null; $('chat-input').value = ''; $('send-chat').textContent = '发送 ↑';
    if (ack.runId) {
      const run = await api(`${H}/runs/${encodeURIComponent(ack.runId)}`);
      if (!chatContextMatches(state, context)) return;
      state.run = run;
    }
    await Promise.allSettled([loadConversations(), refreshChat()]);
  } catch (error) {
    if (chatContextMatches(state, context)) {
      showMessage('chat-error', `${error.message}${state.pendingInput ? ' 重试将沿用同一输入 ID，不创建重复消息。' : ''}`);
      if (error.status && error.status < 500) state.pendingInput = null;
      $('send-chat').textContent = state.pendingInput ? '重试同一条 ↑' : '发送 ↑';
    }
  } finally { state.sending = false; $('send-chat').disabled = false; }
});
document.addEventListener('visibilitychange', () => { clearTimeout(state.timer); clearTimeout(state.chatTimer); if (!document.hidden && state.user) { refresh(); refreshChat(); } });
window.addEventListener('pagehide', () => { clearTimeout(state.timer); clearTimeout(state.chatTimer); });
init();
