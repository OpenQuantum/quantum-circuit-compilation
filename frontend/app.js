import {createResearchViews} from './research.js';
import {ACTIVE, STATUS, escapeHTML as esc, score, timestamp, approvalAllowed, submissionAllowed, errorMessage, orderedMessages, captureChatContext, chatContextMatches} from './model.js';

const $ = id => document.getElementById(id);
const H = '/api/human/v1';
const Q = '/api/quantum/v1';
const state = {user: null, csrf: '', summary: null, view: 'packages', lists: {experiments: [], candidates: [], packages: []}, optimizations: [], optimizationError: '', interaction: null, run: null, conversations: [], conversationCursor: null, agents: [], generation: 0, refreshing: false, sending: false, pendingInput: null, timer: null, chatTimer: null, chatRefreshing: false, chatVersion: 0, dialogPackage: null, uncertainSubmissions: new Set(), approving: false, submitting: false};
let authConfig = null;
const research = createResearchViews({root: $('research-views'), state, api, fillPrompt, openView: view => changeView(view), openApproval, openSubmit, notify});

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
  state.user = null; state.csrf = ''; state.view = 'packages'; state.interaction = null; state.run = null; state.pendingInput = null;
  state.summary = null; state.conversations = []; state.conversationCursor = null; state.agents = [];
  state.lists = {experiments: [], candidates: [], packages: []};
  state.optimizations = []; state.optimizationError = ''; research.reset();
  state.dialogPackage = null; state.uncertainSubmissions.clear(); state.refreshing = false; state.chatRefreshing = false;
  $('approval-dialog').close(); $('submit-dialog').close();
  $('workspace').hidden = true; $('login-view').hidden = false; $('logout').hidden = true;
  $('approval-count').hidden = true; $('approval-count').textContent = ''; $('demo-label').hidden = true;
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
  renderMessages([]);
  changeView('packages');
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

function renderChrome() {
  const summary = state.summary;
  const waiting = summary?.packages_by_status?.awaiting_approval || 0;
  $('approval-count').textContent = waiting; $('approval-count').hidden = !waiting;
  if (summary) { $('world-name').textContent = summary.world_id || 'Quantum World'; $('demo-label').hidden = summary.transport_mode !== 'demo'; }
}
function changeView(view) {
  if (!['packages', 'knowledge', 'experiments'].includes(view)) return;
  state.view = view;
  document.querySelectorAll('[data-view]').forEach(button => {
    const active = button.dataset.view === view;
    button.classList.toggle('active', active);
    if (active) button.setAttribute('aria-current', 'page'); else button.removeAttribute('aria-current');
  });
  research.enter();
}
document.querySelectorAll('[data-view]').forEach(button => button.addEventListener('click', () => changeView(button.dataset.view)));

async function refresh() {
  if (!state.user || state.refreshing) return;
  const generation = state.generation;
  clearTimeout(state.timer); state.refreshing = true;
  try {
    const types = ['experiments', 'candidates', 'packages'];
    const [results, optimization, trajectory] = await Promise.all([
      Promise.all([api(`${Q}/summary`), ...types.map(type => api(`${Q}/${type}`))]),
      api(`${Q}/optimizations`).catch(error => ({error})),
      api(`${Q}/trajectory`).catch(error => ({error})),
    ]);
    if (generation !== state.generation) return;
    state.summary = results[0];
    types.forEach((type, i) => { state.lists[type] = results[i + 1].items || []; });
    const missing = optimization.error?.status === 404;
    if (!optimization.error || missing) state.optimizations = optimization.items || [];
    state.optimizationError = missing ? '' : optimization.error?.message || '';
    research.update(trajectory, trajectory.error?.status === 404 ? '' : trajectory.error?.message, trajectory.error?.status === 404);
    renderChrome(); showMessage('world-error', '');
  } catch (error) {
    if (generation === state.generation) showMessage('world-error', `Quantum World：${error.message}${state.summary ? ' 当前显示上次同步的数据。' : ''}`);
  } finally {
    if (generation === state.generation) { state.refreshing = false; if (state.user && !document.hidden) state.timer = setTimeout(refresh, 4000); }
  }
}

function openApproval(pkg) {
  state.dialogPackage = {...pkg}; $('approval-confirm').checked = false; $('confirm-approval').disabled = true;
  $('approval-content').innerHTML = `<p class="muted small record-id">${esc(pkg.id)}</p><div class="review-scores"><div><span>本地估分</span><strong>${score(pkg.local_score)}</strong></div><div><span>构建时配置参考分</span><strong>${score(pkg.configured_best)}</strong></div></div><p>审核绑定下方文件。请先检查关联实验及下载的产物，本地估分不等于 Kaggle 官方成绩。</p><code class="hash-block">${esc(pkg.sha256)}</code>`;
  showMessage('approval-error', ''); $('approval-dialog').showModal();
}
function openSubmit(pkg) {
  state.dialogPackage = {...pkg}; $('confirm-submit').disabled = false; $('submit-sha').textContent = pkg.sha256; showMessage('submit-error', ''); $('submit-dialog').showModal();
}
$('approval-confirm').addEventListener('change', () => { $('confirm-approval').disabled = state.approving || !$('approval-confirm').checked; });
$('close-approval').addEventListener('click', () => $('approval-dialog').close());
$('close-submit').addEventListener('click', () => $('submit-dialog').close());
$('confirm-approval').addEventListener('click', async () => {
  const pkg = state.dialogPackage;
  if (state.approving || !$('approval-confirm').checked || !pkg || !approvalAllowed(state.summary, pkg)) return;
  state.approving = true; $('confirm-approval').disabled = true;
  try { await post(`${Q}/packages/${encodeURIComponent(pkg.id)}/approve`, {sha256: pkg.sha256}); $('approval-dialog').close(); notify('此提交包已批准。需要你再次点击「提交到 Kaggle」才能上传。'); await refresh(); }
  catch (error) { showMessage('approval-error', error.message); }
  finally { state.approving = false; $('confirm-approval').disabled = !$('approval-confirm').checked; }
});
$('confirm-submit').addEventListener('click', async () => {
  const pkg = state.dialogPackage;
  if (state.submitting || !pkg || !submissionAllowed(state.summary, pkg) || state.uncertainSubmissions.has(pkg.id)) return;
  state.submitting = true; $('confirm-submit').disabled = true; $('confirm-submit').textContent = '正在提交…';
  try { await post(`${Q}/packages/${encodeURIComponent(pkg.id)}/submit`, {sha256: pkg.sha256}, {timeout: 120000}); $('submit-dialog').close(); notify('提交请求已处理，请查看提交包状态与回执。'); }
  catch (error) { state.uncertainSubmissions.add(pkg.id); notify(`${error.message} 请点击「核对提交状态」，不要重复上传。`); }
  finally { state.submitting = false; $('confirm-submit').textContent = '确认提交到 Kaggle'; $('submit-dialog').close(); await refresh(); }
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
