export const STATUS = {
  queued: '排队中', running: '执行中', succeeded: '验证成功', failed: '失败',
  cancelled: '已取消', canceled: '已取消', interrupted: '已中断', cancelling: '取消中',
  awaiting_approval: '待人工审核', approved: '已批准', submitting: '提交中',
  submitted: '已提交', unknown: '结果待核对', waiting: '等待中', paused: '已暂停',
  completed: '已完成', rejected: '已拒绝', pending: '等待中', streaming: '回复中',
  quick: '快速验证', full: '完整验证', no_improvement: '未达改进',
};
export const TASKS = {invsqrt: 'Inverse Square Root', dqi: 'DQI', dfthcsos: 'DF-THC SOS'};
export const ACTIVE = new Set(['queued', 'running', 'waiting', 'paused', 'cancelling', 'submitting']);
export const escapeHTML = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[char]));
export const score = value => typeof value === 'number' && Number.isFinite(value) ? value.toLocaleString('en-US', {maximumFractionDigits: 5}) : '—';
export const bytes = value => typeof value !== 'number' || !Number.isFinite(value) || value < 0 ? '—' : value >= 1048576 ? `${(value / 1048576).toFixed(1)} MiB` : value >= 1024 ? `${(value / 1024).toFixed(1)} KiB` : `${value} B`;
export function timestamp(value) {
  const date = new Date(value);
  return value && !Number.isNaN(date.getTime()) ? date.toLocaleString('zh-CN', {month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit'}) : '—';
}
export function badge(value) {
  const tone = ['succeeded', 'completed', 'approved', 'submitted'].includes(value) ? 'success' : ['running', 'streaming', 'submitting', 'quick', 'full'].includes(value) ? 'live' : ['failed', 'rejected', 'unknown', 'interrupted'].includes(value) ? 'danger' : value === 'awaiting_approval' ? 'warning' : 'neutral';
  return `<span class="badge ${tone}"><i aria-hidden="true"></i>${escapeHTML(STATUS[value] || value || '未知')}</span>`;
}
function experimentReference(label, id, description) {
  if (!id) return '';
  const detail = description ? ` <span class="muted">${escapeHTML(description)}</span>` : '';
  return `<li>${escapeHTML(label)} <button type="button" class="text-button mono" data-open-experiment="${escapeHTML(id)}">${escapeHTML(id)} ↗</button>${detail}</li>`;
}

export function optimizationCards(items) {
  return items.map(item => {
    const baseline = item.baseline;
    const comparison = item.comparison;
    const comparable = Number.isFinite(comparison?.task_cost) && Number.isFinite(comparison?.baseline_cost);
    const change = Number.isFinite(comparison?.reduction_pct)
      ? (comparison.improved ? ` · 成本下降 ${score(comparison.reduction_pct)}%` : ' · 成本未下降')
      : '';
    const costs = comparable
      ? `成本 ${score(comparison.baseline_cost)} → ${score(comparison.task_cost)}${change}`
      : Number.isFinite(baseline?.task_cost) ? `基准成本 ${score(baseline.task_cost)}` : '';
    const sync = {pending: '待写回图谱', synced: '已写回图谱'}[item.sync_status];
    const attempts = (item.attempts || []).map((attempt, index) => experimentReference(
      `候选快速验证 ${index + 1}`, attempt.quick_id,
      attempt.method_node_id ? `方法 ${attempt.method_node_id}` : '',
    )).join('');
    const links = experimentReference('基准完整验证', baseline?.full_id)
      + experimentReference('基准快速验证', baseline?.quick_id)
      + attempts + experimentReference('候选完整验证', item.full_id);
    const error = typeof item.error === 'string' ? item.error : item.error?.message || item.error?.code;
    return `<article class="record-panel optimization-run">
      <div class="optimization-heading"><code class="record-id">${escapeHTML(item.id)}</code>${badge(item.status)}</div>
      ${costs ? `<p>${costs}</p>` : ''}${sync ? `<p class="muted">${sync}</p>` : ''}
      ${links ? `<ul class="artifacts">${links}</ul>` : ''}
      ${error ? `<p class="error-text">${escapeHTML(error)}</p>` : ''}
    </article>`;
  }).join('');
}

export function approvalAllowed(summary, pkg) {
  return summary?.can_approve === true && !summary?.read_only && pkg?.status === 'awaiting_approval' && /^[a-f0-9]{64}$/i.test(pkg.sha256 || '');
}
export function submissionAllowed(summary, pkg, now = Date.now()) {
  return summary?.can_approve === true && !summary?.read_only && pkg?.status === 'approved' && /^[a-f0-9]{64}$/i.test(pkg.sha256 || '') && pkg.approval?.sha256 === pkg.sha256 && new Date(pkg.approval?.expires_at).getTime() > now;
}
export function artifactURL(artifact) {
  // Construct our own authenticated endpoint; never trust a file URL from a tool result.
  return typeof artifact?.id === 'string' && artifact.id.length > 0 && artifact.id.length < 2048 ? `/api/quantum/v1/artifacts/${encodeURIComponent(artifact.id)}` : null;
}
export function errorMessage(payload, fallback = '请求未完成，请重试。') {
  const message = payload?.data?.message ?? payload?.error?.message ?? payload?.detail;
  return typeof message === 'string' ? message : fallback;
}
export function orderedMessages(pages) {
  const messages = new Map();
  for (const page of pages) for (const item of page.items || []) messages.set(`${item.runId || ''}:${item.role}:${item.id}`, item);
  return [...messages.values()].sort((a, b) => (a.timelineSequence || 0) - (b.timelineSequence || 0) || String(a.createdAt || '').localeCompare(String(b.createdAt || '')));
}
export function selectedRecord(view, selection, lists) {
  return lists[view]?.find(item => item.id === selection) || null;
}
export function captureChatContext(state) {
  return {generation: state.generation, chatVersion: state.chatVersion, interactionId: state.interaction?.interactionSessionId ?? null};
}
export function chatContextMatches(state, context) {
  return state.generation === context.generation && state.chatVersion === context.chatVersion
    && (state.interaction?.interactionSessionId ?? null) === context.interactionId;
}
