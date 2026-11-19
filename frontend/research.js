import {escapeHTML as esc, score, timestamp, badge, TASKS, STATUS, approvalAllowed, submissionAllowed, artifactURL, bytes} from './model.js';

const Q = '/api/quantum/v1';
const LOG_NAMES = ['verify.stdout', 'verify.stderr', 'generator.stdout', 'generator.stderr', 'preflight.stdout', 'preflight.stderr'];
const TASK_META = {
  invsqrt: {code: 'invsqrt', title: '逆平方根', goal: '在相位误差约束下，减少量子位和计分门。'},
  dqi: {code: 'DQI', title: '组合优化', goal: '压缩可逆解码器，保留正确的综合征相位。'},
  dfthcsos: {code: 'DF-THC SOS', title: '量子化学', goal: '在哈密顿量精度约束下，减少角度加载及块编码成本。'},
};
const TASK_IDS = Object.keys(TASK_META);

export function submissionTasks(pkg, lists) {
  return Object.keys(TASKS).map(task => {
    const experiment = lists.experiments.find(e => (pkg.experiments || []).includes(e.id) && e.task === task);
    const candidate = lists.candidates.find(c => c.id === experiment?.candidate_id);
    const cost = experiment?.result?.task_cost;
    const factor = cost > 0 && pkg.reference_costs?.[task] ? pkg.reference_costs[task] / cost : null;
    return {task, experiment, candidate, cost, factor};
  });
}

export function createResearchViews({root, state, api, fillPrompt, openView, openApproval, openSubmit, notify}) {
  let trajectory = {items: [], truncated: false}, trajectoryError = '', trajectoryUnavailable = false;
  let selectedRun = 'all', selectedEvent = '', runTouched = false;
  let graphTask = 'invsqrt', graphData = null, graphDetail = null, graphNodeId = '', graphError = '';
  let graphVersion = 0, graphLoaded = false, graphLoading = false;
  let selectedPackageId = '', packageDetail = null;
  let log = {experiment: '', name: LOG_NAMES[0], text: '', error: '', loading: false};
  let signature = '';

  const taskMeta = task => TASK_META[task] || {code: task, title: TASKS[task] || task, goal: ''};
  const statusText = value => STATUS[value] || value || '—';
  const factorText = value => typeof value === 'number' && Number.isFinite(value) ? `${value.toFixed(2)}×` : '—';
  const empty = text => `<div class="empty"><p>${esc(text)}</p></div>`;
  const problem = text => `<p class="error-text" role="alert">${esc(text)}</p>`;

  function artifactsHTML(artifacts = []) {
    if (!artifacts.length) return '<p class="muted small">暂无可下载产物。</p>';
    return `<ul class="artifacts">${artifacts.map(a => `<li>${artifactURL(a) ? `<a href="${esc(artifactURL(a))}" download>${esc(a.name)} ↓</a>` : `<span>${esc(a.name)}</span>`}<span class="muted">${bytes(a.size_bytes)}</span></li>`).join('')}</ul>`;
  }

  function changesOf(candidate) {
    const changes = candidate?.research?.changes;
    if (typeof changes === 'string' && changes.trim()) return changes;
    if (changes && typeof changes === 'object') {
      const text = Object.entries(changes).map(([key, value]) => `${key} ${typeof value === 'object' ? JSON.stringify(value) : value}`).join('，');
      if (text) return text;
    }
    if (candidate?.precision && typeof candidate.precision === 'object') {
      const text = Object.entries(candidate.precision).map(([key, value]) => `${key} ${value}`).join('，');
      if (text) return text;
    }
    return '';
  }
  const methodLabel = candidate => candidate?.description || candidate?.research?.method_node_id || '—';
  const submissionSummary = pkg => [...new Set(submissionTasks(pkg, state.lists).map(t => t.candidate?.description || t.candidate?.research?.method_node_id).filter(Boolean))].join('；') || '—';
  const officialScore = pkg => typeof pkg?.receipt?.public_score === 'number' && Number.isFinite(pkg.receipt.public_score) ? pkg.receipt.public_score : null;

  /* ------------------------------- submissions ------------------------------- */

  function currentPackage() {
    const items = state.lists.packages || [];
    return (packageDetail?.id === selectedPackageId ? packageDetail : items.find(item => item.id === selectedPackageId)) || null;
  }

  function packageRow(pkg) {
    const selected = pkg.id === selectedPackageId;
    const time = pkg.submitted_at || pkg.created_at;
    return `<tr data-package="${esc(pkg.id)}" class="${selected ? 'selected' : ''}">
      <td><button class="record-title" type="button" data-select-package="${esc(pkg.id)}" aria-controls="submission-detail-content" aria-pressed="${selected}">
        <time datetime="${esc(time || '')}">${esc(timestamp(time))}</time><span aria-hidden="true"> ↗</span>
        <span class="record-sub">${esc(pkg.id)}</span></button></td>
      <td class="numeric"><span class="mobile-cell-label">本地估分</span><strong>${score(pkg.local_score)}</strong>
        ${pkg.status === 'awaiting_approval' ? '<span class="record-sub score-pending">待审核</span>' : pkg.status === 'approved' ? '<span class="record-sub">已批准 · 待提交</span>' : pkg.submitted_at ? '<span class="record-sub">已提交</span>' : ''}</td>
      ${submissionTasks(pkg, state.lists).map(t => `<td class="numeric"><span class="mobile-cell-label">${esc(taskMeta(t.task).code)} · 本地</span>${factorText(t.factor)}</td>`).join('')}
      <td class="method-summary"><span class="mobile-cell-label">方法 / 改动</span>${esc(submissionSummary(pkg))}</td></tr>`;
  }

  function packageDetailHTML(pkg) {
    if (!pkg) return empty('选择一条提交记录查看三题方案、验证结果与审核操作。');
    const tasks = submissionTasks(pkg, state.lists);
    const local = pkg.local_score;
    const official = officialScore(pkg);
    const tasksHTML = tasks.map(t => {
      const meta = taskMeta(t.task);
      const nodeId = t.candidate?.research?.method_node_id;
      const label = methodLabel(t.candidate);
      const method = nodeId
        ? `<button class="record-title" type="button" data-method-task="${esc(t.task)}" data-method-node="${esc(nodeId)}">${esc(label)} ↗</button>`
        : `<strong>${esc(label)}</strong>`;
      return `<li><div><strong>${esc(meta.title)}</strong><span class="muted">${esc(t.candidate?.id || '版本未记录')}</span></div>
        <div class="task-method">${method}
          <span>${esc(changesOf(t.candidate) || '—')}</span>
          <span class="muted">${esc(t.experiment?.id || '—')} · ${t.experiment?.status === 'succeeded' ? 'full 通过' : esc(statusText(t.experiment?.status))}</span></div>
        <div class="task-score"><strong>${factorText(t.factor)}</strong><span class="muted">本地分项倍率</span></div></li>`;
    }).join('');
    let actions = '';
    if (pkg.status === 'awaiting_approval') {
      actions += `<div class="detail-actions"><button class="primary" data-review ${approvalAllowed(state.summary, pkg) ? '' : 'disabled'}>审核此提交包 →</button></div>${state.summary?.can_approve ? '' : '<p class="muted small">当前账号没有审批权限。</p>'}`;
    }
    if (pkg.status === 'approved') {
      const allowed = submissionAllowed(state.summary, pkg) && !state.uncertainSubmissions.has(pkg.id);
      actions += `<div class="detail-actions"><button class="primary" data-submit ${allowed ? '' : 'disabled'}>提交到 Kaggle →</button></div><p class="muted small">${allowed ? '提交前将再次核对实时最高分与文件指纹。' : '审批已过期或当前账号无提交权限，请重新核对。'}</p>`;
    }
    if (state.uncertainSubmissions.has(pkg.id)) {
      actions += '<p class="error-text">上次提交请求未返回确认结果。请先核对服务端状态，勿直接重复提交。</p><div class="detail-actions"><button class="button secondary" data-reconcile-package>核对提交状态</button></div>';
    }
    if (pkg.status === 'unknown') actions += '<p class="error-text">上传结果尚未确认。请先到 Kaggle 核对记录，系统不会自动重试。</p>';
    if (pkg.status === 'submitting') actions += '<p class="detail-note">提交请求正在处理，请等待状态更新。</p>';
    if (pkg.error) actions += `<p class="error-text">${esc(typeof pkg.error === 'string' ? pkg.error : pkg.error.message)}</p>`;
    return `<div class="submission-summary"><div>
      <h3>${esc(pkg.id)} ${badge(pkg.status)}</h3>
      <p class="muted small">${pkg.submitted_at ? '提交时间' : '构建时间'} ${esc(timestamp(pkg.submitted_at || pkg.created_at))} · ${esc(statusText(pkg.status))}</p></div>
      <div class="submission-totals"><div><span class="field-label">本地估分</span><strong>${score(local)}</strong></div>
        <div><span class="field-label">官方总分</span><strong>${official == null ? '—' : score(official)}</strong></div></div></div>
      <ul class="submission-methods">${tasksHTML}</ul>
      ${actions}
      <p class="muted small">总分为三个本地改进因子的几何平均；官方成绩以 Kaggle 回执为准。</p>
      <span class="field-label">SHA-256 · 审批绑定此文件指纹</span><code class="hash-block">${esc(pkg.sha256 || '—')}</code>
      <details><summary>提交包产物</summary>${artifactsHTML(pkg.artifacts)}</details>
      ${pkg.receipt ? `<details open><summary>提交回执</summary><pre class="detail-json">${esc(JSON.stringify(pkg.receipt, null, 2))}</pre></details>` : ''}`;
  }

  function submissions() {
    const items = state.lists.packages || [];
    if (!items.some(item => item.id === selectedPackageId)) {
      selectedPackageId = items[0]?.id || '';
      packageDetail = null;
    }
    const table = items.length
      ? `<div class="table-scroll"><table class="submission-table">
          <caption class="sr-only">提交得分、三题本地完整验证倍率和方法</caption>
          <thead><tr><th rowspan="2" scope="col">提交时间 / ID</th><th rowspan="2" scope="col">本地估分</th><th colspan="3" scope="colgroup">本地分项倍率 · full</th><th rowspan="2" scope="col">方法 / 改动</th></tr>
            <tr><th scope="col">invsqrt</th><th scope="col">DQI</th><th scope="col">DF-THC</th></tr></thead>
          <tbody id="submission-rows">${items.map(packageRow).join('')}</tbody></table></div>`
      : empty('还没有提交包。完成三题完整验证后，Agent 即可构建提交包。');
    return `<section id="view-packages" aria-labelledby="submissions-title">
      <div class="page-heading"><div><h1 id="submissions-title">提交轨迹</h1><p>按提交时间回看三题方案和得分变化。</p></div></div>
      <div class="prototype-panel"><div class="prototype-panel-head"><h2>提交记录</h2><span>本地估分 · 最新在前</span></div>${table}</div>
      <section class="prototype-panel prototype-detail" aria-labelledby="submission-detail-title">
        <div class="prototype-panel-head"><h2 id="submission-detail-title">关联详情</h2><button class="text-button" type="button" data-discuss-package>在对话中讨论 ↗</button></div>
        <div class="prototype-panel-body" id="submission-detail-content" aria-live="polite">${packageDetailHTML(currentPackage())}</div>
      </section></section>`;
  }

  async function selectPackage(id) {
    selectedPackageId = id; packageDetail = null; render();
    try {
      const data = await api(`${Q}/packages/${encodeURIComponent(id)}`);
      if (selectedPackageId === id) { packageDetail = data; render(); }
    } catch (error) { notify?.(error.message); }
  }

  async function reconcilePackage(id) {
    try {
      const pkg = await api(`${Q}/packages/${encodeURIComponent(id)}`);
      state.uncertainSubmissions.delete(id);
      if (selectedPackageId === id) { packageDetail = pkg; render(); }
      notify?.(pkg.status === 'approved' ? '服务端仍显示已批准、未提交。请核对错误原因后，再手动决定是否提交。' : `服务端状态：${statusText(pkg.status)}。`);
    } catch (error) { notify?.(error.message); }
  }

  /* -------------------------------- knowledge -------------------------------- */

  function currentGraphNode() {
    return graphDetail || (graphData?.nodes || []).find(node => node.id === graphNodeId) || null;
  }

  function methodDetailHTML() {
    const node = currentGraphNode();
    if (!node) return empty('选择方法查看说明、来源和来源方法。');
    const nodes = graphData?.nodes || [];
    const source = node.source || {};
    const url = typeof source.url === 'string' && /^https?:\/\//i.test(source.url)
      ? ` <a href="${esc(source.url)}" target="_blank" rel="noopener noreferrer">查看来源 ↗</a>` : '';
    const parents = (node.parents || []).map(id => {
      if (id === graphTask) return esc(taskMeta(graphTask).title);
      const parent = nodes.find(item => item.id === id);
      return parent
        ? `<button class="text-button" type="button" data-graph-node="${esc(id)}">${esc(parent.title || id)}</button>`
        : esc(id);
    }).join(' · ') || '—';
    return `<h3 class="method-detail-title">${esc(node.title || node.id)}</h3>
      <p class="method-summary-text">${esc(node.summary || '—')}</p>
      <dl class="method-grid">
        <div><dt>具体做法</dt><dd>${esc(node.explanation || node.summary || '—')}</dd></div>
        <div><dt>来源方法</dt><dd>${parents}</dd></div>
        <div><dt>来源</dt><dd>${esc(source.label || source.type || '未标注来源')}${source.version ? ` · ${esc(source.version)}` : ''}${url}</dd></div>
      </dl>
      ${node.evidence ? `<p class="track-event-copy">${esc(node.evidence)}</p>` : ''}`;
  }

  function graphTreeHTML(nodes) {
    if (!nodes.length) {
      return graphLoading ? '<div class="graph-loading">正在加载图谱…</div>'
        : graphError ? '<div class="graph-loading">图谱暂不可用。</div>'
        : graphLoaded ? '<div class="graph-loading">没有匹配的公开方法。</div>' : '';
    }
    const items = nodes.filter(node => node.id !== graphTask);
    if (!items.length) return '<div class="graph-loading">没有可展示的方法分支。</div>';
    const byId = new Map(items.map(node => [node.id, node]));
    const parentOf = new Map();
    for (const node of items) {
      const parent = (node.parents || []).find(id => id !== graphTask && byId.has(id) && id !== node.id);
      if (parent) parentOf.set(node.id, parent);
    }
    for (const node of items) {
      const seen = new Set(); let current = node.id;
      while (parentOf.has(current)) {
        if (seen.has(current)) { parentOf.delete(current); break; }
        seen.add(current); current = parentOf.get(current);
      }
    }
    const children = new Map(), attached = new Set();
    for (const [child, parent] of parentOf) {
      if (!children.has(parent)) children.set(parent, []);
      children.get(parent).push(child);
      attached.add(child);
    }
    const button = node => `<button class="graph-method" type="button" data-graph-node="${esc(node.id)}" aria-pressed="${node.id === graphNodeId}">
      <strong>${esc(node.title || node.id)}</strong><span>${esc(node.source?.label || node.source?.type || '公开方法')}</span></button>`;
    const branch = (node, path) => {
      const descendants = (children.get(node.id) || []).filter(id => byId.has(id) && !path.has(id));
      const next = new Set(path).add(node.id);
      return `<li>${button(node)}${descendants.length ? `<ul>${descendants.map(id => branch(byId.get(id), next)).join('')}</ul>` : ''}</li>`;
    };
    const meta = taskMeta(graphTask);
    return `<div class="graph-tree">
      <div class="tree-task-node"><span class="section-kicker">${esc(meta.code)}</span><strong>${esc(meta.title)}</strong><span class="muted small">${esc(meta.goal)}</span></div>
      <ul class="tree-roots">${items.filter(node => !attached.has(node.id)).map(node => branch(node, new Set())).join('')}</ul>
    </div>`;
  }

  function knowledge() {
    const meta = taskMeta(graphTask);
    const nodes = graphData?.nodes || [];
    const methods = nodes.filter(node => node.id !== graphTask);
    const total = graphData?.total_matches ?? methods.length;
    const count = graphLoading ? '正在加载' : graphError ? '' : graphLoaded ? (total > methods.length ? `匹配 ${total} 个 · 展示 ${methods.length} 个` : `${methods.length} 个方法`) : '';
    const tabs = TASK_IDS.map(id => `<button id="graph-tab-${id}" role="tab" type="button" aria-controls="graph-panel" aria-selected="${graphTask === id}" tabindex="${graphTask === id ? 0 : -1}" data-graph-task="${id}">${esc(taskMeta(id).title)} <span>${esc(taskMeta(id).code)}</span></button>`).join('');
    return `<section id="view-knowledge" aria-labelledby="knowledge-title">
      <div class="page-heading"><div><h1 id="knowledge-title">知识图谱</h1><p>按题目查看可用方法、来源和方法关联。</p></div></div>
      <div class="graph-tabs" role="tablist" aria-label="比赛题目">${tabs}</div>
      <div class="prototype-panel" id="graph-panel" role="tabpanel" aria-labelledby="graph-tab-${graphTask}">
        <div class="prototype-panel-head"><h2>${esc(meta.title)}方法</h2><span>${esc(count)}</span></div>
        <div class="prototype-panel-body graph-tree-wrap">${graphTreeHTML(nodes)}</div>
      </div>
      ${graphError ? `<div class="prototype-panel prototype-detail"><div class="prototype-panel-body">${problem(`图谱读取失败：${graphError}`)}<button class="text-button" type="button" data-graph-retry>重新加载</button></div></div>` : ''}
      <section class="prototype-panel prototype-detail" aria-labelledby="method-detail-title">
        <div class="prototype-panel-head"><h2 id="method-detail-title">方法详情</h2><button class="text-button" type="button" data-discuss-method>在对话中讨论 ↗</button></div>
        <div class="prototype-panel-body" aria-live="polite">${methodDetailHTML()}</div>
      </section></section>`;
  }

  async function loadGraph(preferredNode = '') {
    const version = ++graphVersion, generation = state.generation;
    graphError = ''; graphData = null; graphDetail = null; graphLoaded = false; graphLoading = true; render();
    try {
      const data = await api(`${Q}/research?task=${encodeURIComponent(graphTask)}`);
      if (version !== graphVersion || generation !== state.generation) return;
      graphData = data; graphLoaded = true; graphLoading = false;
      graphNodeId = preferredNode || data.nodes?.[0]?.id || '';
      render();
      if (graphNodeId) await loadNode(graphNodeId);
    } catch (error) {
      if (version === graphVersion && generation === state.generation) { graphError = error.message; graphLoaded = true; graphLoading = false; render(); }
    }
  }

  async function loadNode(id) {
    const version = ++graphVersion, generation = state.generation;
    graphNodeId = id; graphDetail = null; render();
    try {
      const data = await api(`${Q}/research?task=${encodeURIComponent(graphTask)}&node_id=${encodeURIComponent(id)}`);
      if (version === graphVersion && generation === state.generation) { graphDetail = data.nodes?.[0] || null; render(); }
    } catch (error) { if (version === graphVersion && generation === state.generation) { graphError = error.message; render(); } }
  }

  function switchGraphTask(task) {
    if (!TASK_META[task]) return;
    graphTask = task; graphNodeId = ''; graphData = null; graphDetail = null;
    graphError = ''; graphLoaded = false; graphLoading = false; graphVersion++;
    render(); loadGraph();
  }

  async function openMethod(task, nodeId) {
    if (!TASK_META[task] || !nodeId) return;
    graphTask = task; graphNodeId = nodeId; graphData = null; graphDetail = null;
    graphError = ''; graphLoaded = false; graphLoading = false; graphVersion++;
    openView('knowledge');
  }

  /* --------------------------------- track ---------------------------------- */

  function referenced(action, ids) {
    const values = [...Object.values(action.result || {}), ...Object.values(action.params || {})].filter(value => typeof value === 'string');
    return values.some(value => ids.has(value));
  }

  function scopedCollections() {
    let experiments = state.lists.experiments || [];
    let candidates = state.lists.candidates || [];
    let optimization = null;
    if (selectedRun !== 'all') {
      optimization = (state.optimizations || []).find(item => item.id === selectedRun) || null;
      if (optimization) {
        const experimentsById = new Set([optimization.baseline?.full_id, optimization.baseline?.quick_id, optimization.full_id, ...(optimization.attempts || []).map(item => item.quick_id)].filter(Boolean));
        const candidateIds = new Set([...(optimization.attempts || []).map(item => item.candidate_id).filter(Boolean), ...experiments.filter(item => experimentsById.has(item.id)).map(item => item.candidate_id)]);
        experiments = experiments.filter(item => experimentsById.has(item.id) || candidateIds.has(item.candidate_id));
        candidates = candidates.filter(item => candidateIds.has(item.id));
      } else {
        experiments = experiments.filter(item => item.candidate_id === selectedRun);
        candidates = candidates.filter(item => item.id === selectedRun);
      }
    }
    return {experiments, candidates, optimization};
  }

  function collectEvents() {
    const {experiments, candidates} = scopedCollections();
    const ids = new Set([selectedRun, ...experiments.map(item => item.id), ...candidates.map(item => item.id)]);
    const output = (trajectory.items || []).filter(action => selectedRun === 'all' || referenced(action, ids))
      .map(action => ({id: action.id, at: action.created_at, title: action.tool, type: 'Agent 工具调用', action}));
    for (const candidate of candidates) {
      output.push({id: candidate.id, at: candidate.created_at, title: candidate.description || '创建候选电路', type: taskMeta(candidate.task).title, candidate: candidate.id, data: candidate});
    }
    for (const experiment of experiments) {
      output.push({id: experiment.id, at: experiment.finished_at || experiment.created_at, title: `${experiment.mode === 'full' ? '完整验证' : '快速诊断'} · ${statusText(experiment.status)}`, type: `${taskMeta(experiment.task).title} · ${experiment.id}`, experiment: experiment.id, data: experiment});
    }
    return output.sort((a, b) => String(a.at || '').localeCompare(String(b.at || '')));
  }

  function currentEvent() {
    return collectEvents().find(event => event.id === selectedEvent) || null;
  }

  function summarize(value) {
    if (value == null) return '—';
    if (typeof value !== 'object') return String(value);
    const entries = Object.entries(value);
    return entries.length ? entries.map(([key, item]) => `${key}: ${typeof item === 'object' ? JSON.stringify(item) : item}`).join(' · ') : '—';
  }

  function eventDetailHTML(event) {
    if (!event) return empty('选择一个事件查看具体情况。');
    const fields = [['时间', event.at ? timestamp(event.at) : '未记录'], ['类型', event.type]];
    let copy = '';
    if (event.action) {
      const action = event.action;
      copy = `Agent 调用工具 ${action.tool}${action.status ? `，结果：${statusText(action.status)}` : ''}。`;
      fields.push(['工具参数', summarize(action.params)]);
      fields.push(['结果引用', summarize(action.result)]);
    } else if (event.candidate) {
      const candidate = event.data;
      copy = candidate.description || 'Agent 创建了一个候选电路。';
      fields.push(['任务', taskMeta(candidate.task).title]);
      fields.push(['候选', candidate.id]);
      fields.push(['入口文件', candidate.entrypoint || '—']);
      fields.push(['图谱方法', candidate.research?.method_node_id || '未关联']);
      fields.push(['改动', changesOf(candidate) || '—']);
    } else if (event.experiment) {
      const experiment = event.data;
      copy = `${experiment.mode === 'full' ? '完整验证' : '快速诊断'}：${statusText(experiment.status)}。`;
      fields.push(['任务', taskMeta(experiment.task).title]);
      fields.push(['验证模式', experiment.mode === 'full' ? '完整验证（可提交）' : '快速诊断（不可提交）']);
      fields.push(['任务成本', score(experiment.result?.task_cost)]);
      fields.push(['候选', experiment.candidate_id || '—']);
      if (experiment.finished_at) fields.push(['结束时间', timestamp(experiment.finished_at)]);
      if (experiment.error) fields.push(['失败原因', typeof experiment.error === 'string' ? experiment.error : experiment.error.message || experiment.error.code || '—']);
    }
    let html = `<h3>${esc(event.title)}</h3><p class="track-event-copy">${esc(copy)}</p>
      <dl class="track-event-fields">${fields.map(([label, value]) => `<div><dt>${esc(label)}</dt><dd>${esc(value ?? '—')}</dd></div>`).join('')}</dl>`;
    if (event.experiment) {
      const options = LOG_NAMES.map(name => `<option ${log.name === name ? 'selected' : ''}>${name}</option>`).join('');
      html += `<div class="log-toolbar"><h3>运行日志</h3><label class="sr-only" for="track-log-name">日志文件</label><select id="track-log-name">${options}</select><button class="text-button" type="button" data-track-log="${esc(event.experiment)}">读取日志</button></div>`;
      if (log.experiment === event.experiment) {
        const text = log.loading ? '正在读取日志…' : log.error || log.text || '日志为空。';
        html += `<pre id="track-log-text" class="logs">${esc(text)}</pre>`;
      }
    }
    return html;
  }

  function timelineHTML(timeline) {
    if (!timeline.length) return empty('尚无实验记录。向右侧 Agent 描述优化目标，开始一次实验。');
    return `<ol class="track-events" id="track-events">${timeline.map(event => `<li><button type="button" data-track-event="${esc(event.id)}" aria-pressed="${selectedEvent === event.id}">
      <strong>${esc(event.title)}</strong><small>${esc(event.at ? timestamp(event.at) : '未记录时间')} · ${esc(event.type)}</small></button></li>`).join('')}</ol>`;
  }

  function runSummary(optimization, experiments, candidates) {
    if (optimization) {
      const comparison = optimization.comparison || {};
      const outcome = Number.isFinite(comparison.reduction_pct)
        ? comparison.improved ? `成本下降 ${score(comparison.reduction_pct)}%` : '成本未下降'
        : statusText(optimization.status);
      return [['基准 full 成本', score(optimization.baseline?.task_cost)], ['候选 full 成本', Number.isFinite(comparison.task_cost) ? score(comparison.task_cost) : '等待 full'], ['当前结果', outcome]];
    }
    const fulls = experiments.filter(item => item.mode === 'full' && item.status === 'succeeded');
    return [['已记录候选', String(candidates.length)], ['有效完整验证', String(fulls.length)], ['当前结果', `${experiments.length} 个本地实验`]];
  }

  function attemptsHTML({experiments, candidates, optimization}) {
    if (!candidates.length) return `<tr><td colspan="5"><p class="muted small">尚无候选。向右侧 Agent 描述优化目标，开始一次实验。</p></td></tr>`;
    return candidates.map(candidate => {
      const tests = experiments.filter(item => item.candidate_id === candidate.id);
      const cell = mode => tests.filter(item => item.mode === mode)
        .map(item => `<button type="button" class="text-button" data-track-experiment="${esc(item.id)}">${score(item.result?.task_cost)} · ${esc(statusText(item.status))}</button>`).join('<br>') || '—';
      const full = tests.filter(item => item.mode === 'full' && item.status === 'succeeded').sort((a, b) => String(b.created_at || '').localeCompare(String(a.created_at || '')))[0];
      const baselineCandidate = experiments.find(item => item.id === optimization?.baseline?.full_id)?.candidate_id;
      let result = statusText(candidate.status);
      if (candidate.id !== baselineCandidate && full && Number.isFinite(full.result?.task_cost) && Number.isFinite(optimization?.baseline?.task_cost) && optimization.baseline.task_cost > 0) {
        const reduction = (optimization.baseline.task_cost - full.result.task_cost) / optimization.baseline.task_cost * 100;
        result = reduction > 0 ? `成本下降 ${reduction.toFixed(2)}%` : '成本未下降';
      } else if (full) result = '完整验证通过';
      const selected = selectedEvent === candidate.id || tests.some(item => selectedEvent === item.id);
      return `<tr data-attempt-event="${esc(candidate.id)}" class="${selected ? 'selected' : ''}">
        <td><button type="button" class="text-button" data-track-attempt="${esc(candidate.id)}">${esc(candidate.description || candidate.id)} ↗</button><span class="record-sub">${esc(taskMeta(candidate.task).title)} · ${esc(methodLabel(candidate))}</span></td>
        <td>${esc(changesOf(candidate) || candidate.entrypoint || '—')}</td><td>${cell('quick')}</td><td>${cell('full')}</td><td>${esc(result)}</td></tr>`;
    }).join('');
  }

  function track() {
    if (!runTouched && selectedRun === 'all' && (state.optimizations || []).length) selectedRun = state.optimizations[0].id;
    const choices = [{id: 'all', label: '全部 Agent 与实验记录'}]
      .concat((state.optimizations || []).slice(0, 30).map(item => ({id: item.id, label: `优化轮次 · ${item.id}`})))
      .concat((state.lists.candidates || []).slice(0, 30).map(item => ({id: item.id, label: `${taskMeta(item.task).title} · ${item.id}`})));
    if (selectedRun !== 'all' && !choices.some(choice => choice.id === selectedRun)) selectedRun = 'all';
    const runs = choices.map(choice => `<option value="${esc(choice.id)}" ${choice.id === selectedRun ? 'selected' : ''}>${esc(choice.label)}</option>`);
    const {experiments, candidates, optimization} = scopedCollections();
    const timeline = collectEvents();
    if (!timeline.some(event => event.id === selectedEvent)) selectedEvent = timeline.at(-1)?.id || '';
    const active = timeline.find(event => event.id === selectedEvent) || timeline.at(-1);
    const selectedCandidate = (state.lists.candidates || []).find(item => item.id === selectedRun);
    const title = optimization ? `优化轮次 ${optimization.id}` : selectedRun === 'all' ? '全部 Agent 与实验记录' : `${taskMeta(selectedCandidate?.task).title} · ${selectedRun}`;
    const stateText = optimization ? statusText(optimization.status) : `${timeline.length} 个事件`;
    const summary = runSummary(optimization, experiments, candidates).map(([label, value]) => `<div><span>${esc(label)}</span><strong>${esc(value)}</strong></div>`).join('');
    return `<section id="view-experiments" aria-labelledby="track-title">
      <div class="page-heading"><div><h1 id="track-title">实验轨迹</h1><p>回看 Agent 如何选择方法、提交候选和完成本地验证。</p></div></div>
      <div class="track-run-picker"><label for="track-run">研究轮次</label><select id="track-run">${runs.join('')}</select></div>
      <section class="prototype-panel"><div class="prototype-panel-head"><h2>${esc(title)}</h2><span>${esc(stateText)}</span></div><div class="prototype-panel-body"><div class="track-summary" id="track-summary">${summary}</div></div></section>
      ${state.optimizationError ? `<div class="track-note">${problem(`优化轮次读取失败：${state.optimizationError}`)}</div>` : ''}
      <div class="track-columns">
        <section class="prototype-panel"><div class="prototype-panel-head"><h2>Agent 执行轨迹</h2><span>工具调用与实验事件</span></div>${trajectoryUnavailable ? '<p class="track-note muted small">当前服务未提供 Agent 工具轨迹，仅显示候选与实验记录。</p>' : ''}${trajectoryError ? `<div class="track-note">${problem(`工具轨迹读取失败：${trajectoryError}`)}</div>` : ''}${trajectory.truncated ? '<p class="track-note muted small">显示最近 500 条工具调用；候选与实验记录继续保留。</p>' : ''}${timelineHTML(timeline)}</section>
        <section class="prototype-panel"><div class="prototype-panel-head"><h2>事件详情</h2><button type="button" class="text-button" data-discuss-event>在对话中讨论 ↗</button></div><div class="prototype-panel-body" id="track-event-detail" aria-live="polite">${eventDetailHTML(active)}</div></section>
      </div>
      <section class="prototype-panel prototype-detail"><div class="prototype-panel-head"><h2>尝试过的方法与本地测试</h2><span>点击记录查看具体过程</span></div><div class="table-scroll"><table id="track-attempts"><thead><tr><th>候选 / 方法</th><th>改动</th><th>quick 诊断成本</th><th>full 任务成本</th><th>结果</th></tr></thead><tbody>${attemptsHTML({experiments, candidates, optimization})}</tbody></table></div></section>
    </section>`;
  }

  function selectExperimentEvent(experimentId) {
    const timeline = collectEvents();
    const event = timeline.find(item => item.experiment === experimentId && !item.finished) || timeline.find(item => item.experiment === experimentId);
    if (event) { selectedEvent = event.id; render(); }
  }

  function selectAttemptEvent(candidateId) {
    const timeline = collectEvents();
    const event = timeline.filter(item => item.candidate === candidateId || (item.experiment && (state.lists.experiments || []).find(experiment => experiment.id === item.experiment)?.candidate_id === candidateId)).at(-1);
    if (event) { selectedEvent = event.id; render(); }
  }

  async function loadLog(experimentId) {
    const name = log.name;
    log = {...log, experiment: experimentId, loading: true, error: '', text: ''}; render();
    try {
      const data = await api(`${Q}/experiments/${encodeURIComponent(experimentId)}/logs?name=${encodeURIComponent(name)}&offset=0&limit=32768`);
      if (log.experiment === experimentId && log.name === name) { log = {...log, loading: false, text: data.text || ''}; render(); }
    } catch (error) {
      if (log.experiment === experimentId && log.name === name) { log = {...log, loading: false, error: error.message, text: ''}; render(); }
    }
  }

  /* --------------------------------- render --------------------------------- */

  function render(force = false) {
    const visible = ['packages', 'knowledge', 'experiments'].includes(state.view);
    root.hidden = !visible;
    if (!visible) return;
    const markup = state.view === 'packages' ? submissions() : state.view === 'knowledge' ? knowledge() : track();
    if (force || markup !== signature) {
      root.innerHTML = markup; signature = markup;
      if (state.view === 'experiments') {
        const list = root.querySelector('.track-events');
        const pressed = list?.querySelector('button[aria-pressed="true"]');
        if (list && pressed) {
          const offset = pressed.getBoundingClientRect().top - list.getBoundingClientRect().top + list.scrollTop;
          if (offset < list.scrollTop || offset + pressed.offsetHeight > list.scrollTop + list.clientHeight) {
            list.scrollTop = Math.max(0, offset - list.clientHeight + pressed.offsetHeight + 12);
          }
        }
      }
    }
  }

  root.addEventListener('click', event => {
    const button = event.target.closest('button');
    if (!button) return;
    if (button.dataset.selectPackage) { selectPackage(button.dataset.selectPackage); return; }
    if (button.dataset.graphTask) { if (button.dataset.graphTask !== graphTask) switchGraphTask(button.dataset.graphTask); return; }
    if (button.dataset.graphNode) { loadNode(button.dataset.graphNode); return; }
    if (button.hasAttribute('data-graph-retry')) { graphVersion++; graphLoaded = false; loadGraph(graphNodeId); return; }
    if (button.hasAttribute('data-review')) { const pkg = currentPackage(); if (pkg && approvalAllowed(state.summary, pkg)) openApproval?.(pkg); return; }
    if (button.hasAttribute('data-submit')) {
      const pkg = currentPackage();
      if (pkg && submissionAllowed(state.summary, pkg) && !state.uncertainSubmissions.has(pkg.id)) openSubmit?.(pkg);
      return;
    }
    if (button.hasAttribute('data-reconcile-package')) { const pkg = currentPackage(); if (pkg) reconcilePackage(pkg.id); return; }
    if (button.dataset.methodNode) { openMethod(button.dataset.methodTask, button.dataset.methodNode); return; }
    if (button.dataset.trackEvent) { selectedEvent = button.dataset.trackEvent; render(); return; }
    if (button.dataset.trackExperiment) { selectExperimentEvent(button.dataset.trackExperiment); return; }
    if (button.dataset.trackAttempt) { selectAttemptEvent(button.dataset.trackAttempt); return; }
    if (button.dataset.trackLog) { loadLog(button.dataset.trackLog); return; }
    if (button.hasAttribute('data-discuss-package')) {
      const pkg = currentPackage();
      fillPrompt(`请分析提交 ${pkg?.id || ''} 各题的改动、分项倍率和下一步可以验证的方向。`);
      return;
    }
    if (button.hasAttribute('data-discuss-method')) {
      const node = currentGraphNode();
      fillPrompt(`请解释「${node?.title || '该方法'}」的适用条件，以及如何验证它对 ${taskMeta(graphTask).code} 的作用。`);
      return;
    }
    if (button.hasAttribute('data-discuss-event')) {
      const active = currentEvent();
      fillPrompt(`请分析本轮实验中「${active?.title || ''}」的具体结果和后续验证方向。`);
    }
  });

  root.addEventListener('change', event => {
    if (event.target.id === 'track-run') { selectedRun = event.target.value; selectedEvent = ''; runTouched = true; render(); }
    if (event.target.id === 'track-log-name') { log.name = event.target.value; log.text = ''; log.error = ''; log.loading = false; render(); }
  });

  root.addEventListener('keydown', event => {
    const tab = event.target.closest('[role="tab"][data-graph-task]');
    if (!tab) return;
    const directions = {ArrowRight: 1, ArrowLeft: -1};
    if (!(event.key in directions) && !['Home', 'End'].includes(event.key)) return;
    event.preventDefault();
    const index = TASK_IDS.indexOf(graphTask);
    const next = event.key === 'Home' ? TASK_IDS[0] : event.key === 'End' ? TASK_IDS.at(-1) : TASK_IDS[(index + directions[event.key] + TASK_IDS.length) % TASK_IDS.length];
    switchGraphTask(next);
    root.querySelector(`#graph-tab-${next}`)?.focus();
  });

  return {
    render,
    enter() {
      render();
      if (state.view === 'knowledge' && !graphLoaded && !graphLoading) loadGraph(graphNodeId);
    },
    update(data, error, unavailable = false) {
      trajectory = {items: data?.items || [], truncated: !!data?.truncated};
      trajectoryError = error || '';
      trajectoryUnavailable = unavailable;
      packageDetail = null;
      render();
    },
    reset() {
      graphVersion++; trajectory = {items: [], truncated: false}; trajectoryError = ''; trajectoryUnavailable = false;
      selectedRun = 'all'; selectedEvent = ''; runTouched = false; graphTask = 'invsqrt'; graphData = null; graphDetail = null;
      graphNodeId = ''; graphError = ''; graphLoaded = false; graphLoading = false;
      selectedPackageId = ''; packageDetail = null; log = {experiment: '', name: LOG_NAMES[0], text: '', error: '', loading: false};
      signature = ''; root.innerHTML = '';
    },
  };
}
