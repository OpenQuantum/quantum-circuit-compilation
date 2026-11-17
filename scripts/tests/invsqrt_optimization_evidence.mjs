import assert from 'node:assert/strict';

export const OFFICIAL_SHA256 = 'e2bf8eb64350a4460a271fc7f1d591ef125b1e276377e9a0f64b96ab3ca321e1';
const OPTIMIZED_REFERENCE_SHA256 = '561687a62b2b562bf72477ef11cedee81e99199e15bf10347490d0258e47352a';
export const EXPERIMENT_TERMINAL = new Set(['succeeded', 'failed', 'cancelled', 'interrupted']);
export const RUN_TERMINAL = new Set(['succeeded', 'failed', 'cancelled', 'canceled', 'rejected', 'interrupted']);
export const BUDGET = Object.freeze({ quick: 3, full: 1 });
const HEX = /^[a-f0-9]{64}$/;
const IDENTIFIER = /^[A-Za-z0-9_-]+$/;
const VERIFIER = 'invsqrt.verify_submission_invsqrt';
const SHOTS = Object.freeze({ quick: 262144, full: 200540928 });

export function optimizationPrompt(baselineId, quickBaselineId, priorIds = [], { graphGuided = false, quickBudget = BUDGET.quick } = {}) {
  assert.match(baselineId, IDENTIFIER);
  if (quickBaselineId) assert.match(quickBaselineId, IDENTIFIER);
  for (const id of priorIds) assert.match(id, IDENTIFIER);
  assert.ok(Number.isInteger(quickBudget) && quickBudget >= 0 && quickBudget <= BUDGET.quick, 'Invalid remaining quick budget');
  return [
    graphGuided
      ? `请用知识图谱独立优化 invsqrt 电路并降低完整验证成本。基准是已完成 full 的官方参考生成器优化版，实验 ID 为 ${baselineId}。其实现为 build_nr_circuit(10, prec=replace(Precision(), product_bits=27))。请先读取基准回执。`
      : `请独立优化 invsqrt 电路，在满足官方精度和正确性约束的前提下降低完整验证成本。官方样例已经完成一轮完整验证，实验 ID 为 ${baselineId}，请用这条实际记录作为基线。`,
    ...(quickBaselineId ? [`同一基准电路的 quick 筛选对照实验 ID 为 ${quickBaselineId}，无需重复运行基线。`] : []),
    ...(graphGuided ? [
      '用 SearchResearchMethods 搜短关键词（例如“位宽”或“Newton”，limit=2），再用 GetResearchNode 读取所选节点。选择能直接应用到已优化参考生成器的一个方法，并按该节点的做法实现候选。',
      '优先导入官方参考生成器并传入修改后的参数，源码只读必要的一个片段。尽快登记并验证一份明确实现了所选方法的简洁候选。',
      '登记候选时，在 description 中写一个 JSON 对象，字段 method_node_id 为实际读过的节点 ID，based_on 为实际代码起点，changes 为具体的改前和改后。',
      '先完成 quick 筛选并读取验证结果。若有值得做 full 的候选，请报告可比诊断成本和候选 ID，等本对话确认后再运行该候选的 full。',
    ] : []),
    ...(priorIds.length ? [`此前尝试的实验 ID：${priorIds.join('、')}。可以直接用这些已有记录中的预筛选和失败证据决定下一步，避免重复已做过的分析。`] : []),
    '优先把验证预算用于相对基线电路有实际改动的候选。本地分析和预筛选可以在你编写的候选生成器里进行，由你决定如何设计和筛选改法。',
    `本轮预算最多 ${quickBudget} 次 quick，最多 1 次 full（已完成的基线不占预算）。请根据实际 quick 结果决定是否值得对某个候选做完整验证，保留同一候选来比较；不要构建提交包或上传。`,
    '如实验仍排队或运行，可以合理等待，或说明未完成并给出实验 ID。选出候选并登记完整验证后，如果仍在运行就报告该 ID，无需反复等待。最后给出你实际完成的步骤、实验 ID、结果和选择理由，未完成的结果不要猜测。',
  ].join('\n');
}

function validId(value) {
  return typeof value === 'string' && IDENTIFIER.test(value);
}

function cost(value) {
  return Number.isFinite(value) && value > 0;
}

function time(value) {
  return typeof value === 'string' ? Date.parse(value) : NaN;
}

function requireTimeline(row) {
  const created = time(row.created_at);
  const started = time(row.started_at);
  const finished = time(row.finished_at);
  assert.ok(Number.isFinite(created) && created <= started && started <= finished,
    `Missing or inconsistent experiment timestamps for ${row.id}`);
}

function builtCircuit(provenance) {
  const names = Object.keys(provenance?.built_artifacts ?? {}).filter(name => /^invsqrt\.km[bx]$/.test(name));
  assert.equal(names.length, 1, 'Expected one raw invsqrt build hash');
  const filename = names[0];
  const sha256 = provenance.built_artifacts[filename];
  assert.match(sha256, HEX, 'Invalid raw circuit hash');
  return { filename, sha256 };
}

function validationOf(row) {
  const value = row.provenance?.validation;
  assert.equal(value?.module, VERIFIER, `Missing official validation module for ${row.id}`);
  assert.equal(value.mode, row.mode, `Validation mode mismatch for ${row.id}`);
  assert.equal(value.max_x, row.mode === 'quick' ? 4096 : null, `Validation coverage mismatch for ${row.id}`);
  for (const key of ['verifier_sha256', 'preflight_sha256', 'metric_sha256']) {
    assert.match(value[key], HEX, `Invalid validation digest ${key}`);
  }
  assert.ok(Number.isInteger(value.max_workers) && value.max_workers > 0, 'Missing validation worker count');
  const { mode, max_x, ...config } = value;
  return config;
}

function requireVerified(row, log) {
  assert.equal(row.status, 'succeeded');
  requireTimeline(row);
  const result = row.result;
  const full = row.mode === 'full';
  assert.equal(result?.status, 'PASS', `Missing verifier PASS for ${row.id}`);
  assert.equal(result.full, full, `Mismatched full flag for ${row.id}`);
  assert.ok(cost(result.diagnostic_cost), `Expected finite positive diagnostic cost for ${row.id}`);
  if (full) {
    assert.ok(cost(result.task_cost), `Expected finite positive full cost for ${row.id}`);
    assert.equal(result.task_cost, result.diagnostic_cost, `Full cost mismatch for ${row.id}`);
  } else {
    assert.equal(result.task_cost, null, 'Quick cost cannot count as a full result');
  }
  assert.equal(result.verified_shots, SHOTS[row.mode], `Wrong verified shots for ${row.id}`);
  const rawBuild = builtCircuit(row.provenance);
  const verifier = row.provenance.verifier_report;
  const canonicalSha256 = verifier?.circuit_sha256;
  assert.match(canonicalSha256, HEX, `Missing canonical circuit hash for ${row.id}`);
  const expected = { status: 'PASS', score: result.diagnostic_cost, verified_shots: SHOTS[row.mode],
    circuit_sha256: canonicalSha256 };
  for (const [key, value] of Object.entries(expected)) {
    assert.equal(verifier?.[key], value, `Verifier report/hash mismatch for ${row.id}: ${key}`);
    if (log !== undefined) assert.equal(log?.[key], value, `Verifier log mismatch for ${row.id}: ${key}`);
  }
  return { circuit: { ...rawBuild, circuitSha256: canonicalSha256 }, validation: validationOf(row),
    cost: full ? result.task_cost : result.diagnostic_cost };
}

function requireOfficial(row, mode, identity, log) {
  assert.ok(row && validId(row.id) && row.mode === mode && row.task === 'invsqrt'
    && row.sender_id === identity.agent, `Missing owned ${mode} invsqrt baseline experiment`);
  const provenance = row.provenance;
  assert.ok(row.candidate_id === null && provenance?.candidate === null && provenance.input_kind === 'official_baseline'
    && provenance.seed?.source === 'official_sample_submission' && provenance.seed?.sha256 === OFFICIAL_SHA256
    && provenance.seed?.filename === 'invsqrt.kmb'
    && provenance.built_artifacts?.['invsqrt.kmb'] === OFFICIAL_SHA256
    && provenance.verifier_report?.circuit_sha256 === OFFICIAL_SHA256, 'Record is not the official baseline build');
  return requireVerified(row, log);
}

function requireOptimizedReference(row, mode, identity, log) {
  assert.ok(row?.mode === mode && row.task === 'invsqrt' && validId(row.id)
    && row.sender_id === identity.agent, `Missing owned ${mode} invsqrt baseline experiment`);
  const p = row.provenance;
  assert.ok(validId(row.candidate_id) && p?.input_kind === 'candidate'
    && p.candidate?.id === row.candidate_id && p.seed?.source === 'official_sample_submission'
    && p.seed?.sha256 === OFFICIAL_SHA256 && p.seed?.filename === 'invsqrt.kmb'
    && p.built_artifacts?.['invsqrt.kmb'] === OPTIMIZED_REFERENCE_SHA256
    && p.verifier_report?.circuit_sha256 === OPTIMIZED_REFERENCE_SHA256,
    'Record is not the prior optimized official reference circuit');
  return requireVerified(row, log);
}

export function requireBaseline(row, identity, log) {
  return identity.graphGuided
    ? requireOptimizedReference(row, 'full', identity, log)
    : requireOfficial(row, 'full', identity, log);
}

export function requireBaselineQuick(row, baseline, identity, log) {
  const full = requireBaseline(baseline, identity);
  const quick = identity.graphGuided
    ? requireOptimizedReference(row, 'quick', identity, log)
    : requireOfficial(row, 'quick', identity, log);
  if (identity.graphGuided) {
    assert.equal(row.candidate_id, baseline.candidate_id, 'Quick and full baseline must use the same candidate');
    assert.deepEqual(row.provenance.candidate, baseline.provenance.candidate, 'Baseline source manifests differ');
  }
  assert.deepEqual(quick.validation, full.validation, 'Incompatible quick baseline validation configuration');
  return quick;
}

function uniqueById(records, label) {
  const mapped = new Map();
  for (const row of records) {
    assert.ok(validId(row.id) && !mapped.has(row.id), `Invalid or duplicate ${label} ID`);
    mapped.set(row.id, row);
  }
  return mapped;
}

function researchNodes(action, name) {
  const result = action.result;
  return action.name === name && action.args.task === 'invsqrt' && result?.task === 'invsqrt'
    && result.scope === 'public_and_official_only' && result.read_only === true
    && Array.isArray(result.nodes) && validId(action.actionId) && validId(action.outcomeId)
    ? result.nodes : [];
}

function candidateResearch(candidate, created, earlierActions) {
  assert.ok(typeof candidate.description === 'string' && candidate.description === created.args.description,
    'Candidate description differs from CreateCandidate request');
  let annotation;
  try {
    annotation = JSON.parse(candidate.description);
  } catch {
    assert.fail('Candidate description must contain JSON research evidence');
  }
  const hasText = value => typeof value === 'string' && value.trim().length > 0;
  const changes = annotation?.changes;
  const fieldChanges = changes && typeof changes === 'object' && !Array.isArray(changes)
    ? Object.entries(changes) : [];
  assert.ok(validId(annotation?.method_node_id) && hasText(annotation.based_on)
    && (hasText(changes) || (fieldChanges.length > 0 && fieldChanges.every(([field, change]) =>
      hasText(field) && hasText(change)))),
    'Candidate description must identify the research method, starting implementation and changes');

  const nodeId = annotation.method_node_id;
  for (let index = earlierActions.length - 1; index >= 0; index--) {
    const detail = earlierActions[index];
    if (detail.args.node_id !== nodeId) continue;
    const node = researchNodes(detail, 'GetResearchNode').find(n => n.id === nodeId && n.task === 'invsqrt');
    if (!node || !(hasText(node.summary) || hasText(node.explanation))
      || !['public', 'official'].includes(node.source?.type) || !hasText(node.source.url)) continue;
    const search = earlierActions.slice(0, index).findLast(action =>
      researchNodes(action, 'SearchResearchMethods').some(n => n.id === nodeId));
    if (!search) continue;
    return {
      candidateId: candidate.id, basedOn: annotation.based_on, changes: annotation.changes, node,
      search: { runId: search.runId, actionId: search.actionId, outcomeId: search.outcomeId },
      detail: { runId: detail.runId, actionId: detail.actionId, outcomeId: detail.outcomeId },
    };
  }
  assert.fail('Research method must be found and read before creating the candidate');
}

export function assessOptimization({ identity, baseline, experiments, candidates, actions, logs = {} }) {
  const initial = requireBaseline(baseline, identity, logs[baseline.id]);
  assert.ok(logs[baseline.id], 'Missing official baseline verifier log');
  const candidateById = uniqueById(candidates, 'candidate');
  const experimentById = uniqueById(experiments, 'experiment');
  const requests = actions.filter(action => action.name === 'RunExperiment');
  const calls = { quick: [], full: [] };
  for (const action of requests) {
    const { task, mode, candidate_id } = action.args;
    assert.ok(task === 'invsqrt' && Object.hasOwn(calls, mode), 'Experiment outside invsqrt optimization scope');
    assert.ok(validId(candidate_id), 'Optimization experiment must use a candidate');
    calls[mode].push(action);
    assert.ok(calls[mode].length <= BUDGET[mode], `Exceeded ${mode} experiment budget`);
  }
  assert.ok(actions.every(action => !['BuildSubmission', 'ApproveSubmission', 'SubmitSubmission'].includes(action.name)),
    'Agent attempted to prepare or publish a competition submission');
  const known = { quick: [], full: [] };
  const referenced = new Set();
  const researchByCandidate = new Map();
  for (const request of requests.filter(action => action.result)) {
    const { mode, candidate_id } = request.args;
    const id = request.result.experiment_id;
    assert.ok(validId(id) && id !== baseline.id && !referenced.has(id) && request.result.status === 'queued',
      'Invalid, reused or duplicate experiment receipt');
    referenced.add(id);
    const experiment = experimentById.get(id);
    assert.ok(experiment && experiment.sender_id === identity.agent && experiment.task === 'invsqrt'
      && experiment.mode === mode && experiment.candidate_id === candidate_id, 'Experiment record or candidate ID does not match the request');
    const candidate = candidateById.get(candidate_id);
    const created = actions.find(action => action.name === 'CreateCandidate' && action.result?.candidate_id === candidate_id);
    assert.ok(candidate && candidate.sender_id === identity.agent && candidate.task === 'invsqrt'
      && created?.result.task === 'invsqrt' && actions.indexOf(created) < actions.indexOf(request),
    'Missing or foreign candidate creation in the browser run');
    assert.deepEqual(candidate.files, created.result.files, 'Candidate source hashes differ from creation receipt');
    if (identity.graphGuided && !researchByCandidate.has(candidate_id)) {
      researchByCandidate.set(candidate_id, candidateResearch(candidate, created, actions.slice(0, actions.indexOf(created))));
    }
    if (experiment.status === 'succeeded') {
      const provenance = experiment.provenance;
      assert.equal(provenance?.input_kind, 'candidate', 'Experiment lacks candidate provenance');
      assert.deepEqual(provenance.candidate, { id: candidate_id, entrypoint: candidate.entrypoint, files: candidate.files },
        'Experiment provenance does not identify the same immutable candidate');
      assert.ok(logs[id], `Missing verifier log for ${id}`);
      const verified = requireVerified(experiment, logs[id]);
      assert.deepEqual(verified.validation, initial.validation, 'Incompatible validation configuration');
    } else {
      assert.ok(EXPERIMENT_TERMINAL.has(experiment.status) || ['queued', 'running', 'cancelling'].includes(experiment.status),
        `Unknown experiment status ${experiment.status}`);
    }
    known[mode].push({ experiment, request });
  }
  assert.deepEqual([...referenced].sort(), [...experimentById.keys()].sort(),
    'New experiment records exist without matching browser run receipts');
  const counts = { quick: calls.quick.length, full: calls.full.length };
  const base = { baselineId: baseline.id, baselineCost: initial.cost, calls: counts,
    ...(identity.graphGuided ? { nextBaselineId: baseline.id, research: [...researchByCandidate.values()] } : {}) };
  if (requests.some(action => ['unknown', 'running', 'pending'].includes(action.status))) {
    return { ...base, status: 'experiment_outcome_unconfirmed' };
  }
  const full = known.full[0];
  if ([...known.quick, ...known.full].some(item => !EXPERIMENT_TERMINAL.has(item.experiment.status))) {
    return { ...base, status: 'experiment_pending', fullId: full?.experiment.id ?? null };
  }
  if (!full) return { ...base, status: calls.full.length ? 'full_request_failed' : 'full_not_run' };
  if (full.experiment.status !== 'succeeded') {
    return { ...base, status: 'full_failed', fullId: full.experiment.id,
      fullStatus: full.experiment.status, error: full.experiment.error ?? null };
  }
  const fullCircuit = builtCircuit(full.experiment.provenance);
  if (identity.graphGuided) {
    assert.notEqual(full.experiment.provenance.verifier_report.circuit_sha256, initial.circuit.circuitSha256,
      'The full experiment verifies the same circuit as the baseline circuit');
  }
  const sameCandidate = known.quick.filter(item => item.experiment.status === 'succeeded'
    && item.experiment.candidate_id === full.experiment.candidate_id);
  assert.ok(sameCandidate.length, 'Full experiment did not reuse a successful quick candidate');
  const sameBuild = sameCandidate.filter(item => {
    const quickCircuit = builtCircuit(item.experiment.provenance);
    return quickCircuit.filename === fullCircuit.filename && quickCircuit.sha256 === fullCircuit.sha256
      && item.experiment.provenance.verifier_report.circuit_sha256
        === full.experiment.provenance.verifier_report.circuit_sha256;
  });
  assert.ok(sameBuild.length, 'Quick and full circuit build hashes differ');
  const beforeFull = actions.slice(0, actions.indexOf(full.request));
  const selected = sameBuild.find(item => beforeFull.some(action => {
    const quick = item.experiment;
    const receipt = action.result;
    if (action.name !== 'GetExperiment' || action.args.experiment_id !== quick.id || receipt?.status !== 'succeeded') return false;
    assert.equal(receipt.id, quick.id, 'Agent quick query refers to another record');
    assert.equal(receipt.mode, 'quick', 'Agent quick query refers to a different mode');
    assert.equal(receipt.candidate_id, quick.candidate_id, 'Agent quick query refers to a different candidate record');
    assert.deepEqual(receipt.result, quick.result, 'Agent quick result differs from the World record');
    assert.deepEqual(receipt.provenance, quick.provenance, 'Agent quick provenance differs from the World record');
    return time(quick.finished_at) <= time(full.experiment.created_at);
  }));
  assert.ok(selected, 'No observed passing quick result before the selected full experiment');
  const fullCost = full.experiment.result.task_cost;
  const improved = fullCost < initial.cost;
  const reduction = initial.cost - fullCost;
  return { ...base, status: improved ? 'improved' : 'no_improvement', fullCost,
    ...(identity.graphGuided ? {
      reductionPct: 100 * reduction / initial.cost, nextBaselineId: improved ? full.experiment.id : baseline.id,
    } : {}),
    reduction, fullId: full.experiment.id, selectedQuickId: selected.experiment.id,
    candidateId: full.experiment.candidate_id, build: { ...fullCircuit,
      circuitSha256: full.experiment.provenance.verifier_report.circuit_sha256 }, validation: initial.validation };
}

export function canConfirmFull(run, actions, experiments, quickBaseline) {
  if (run.status !== 'succeeded' || !run.modelDecision?.length
    || !cost(quickBaseline?.result?.diagnostic_cost)
    || actions.some(action => action.name === 'RunExperiment' && action.args.mode === 'full')) return false;
  const baselineHash = quickBaseline.provenance ? builtCircuit(quickBaseline.provenance).sha256 : OFFICIAL_SHA256;
  const baselineCircuitHash = quickBaseline.provenance?.verifier_report?.circuit_sha256 ?? baselineHash;
  return experiments.some(row => row.mode === 'quick' && row.status === 'succeeded'
    && row.result?.status === 'PASS' && cost(row.result.diagnostic_cost)
    && row.result.diagnostic_cost < quickBaseline.result.diagnostic_cost
    && builtCircuit(row.provenance).sha256 !== baselineHash
    && row.provenance.verifier_report?.circuit_sha256 !== baselineCircuitHash
    && actions.some(action => action.name === 'GetExperiment'
      && action.args.experiment_id === row.id && action.result?.status === 'succeeded'));
}

export function needsContinuation(run, actions, experiments) {
  const terminal = run.status === 'succeeded'
    || (run.status === 'failed' && run.error?.code === 'GENERATION_BUDGET_EXCEEDED');
  const finished = time(run.timing?.finishedAt);
  if (!terminal || !Number.isFinite(finished)
    || actions.some(action => action.name === 'RunExperiment' && action.args.mode === 'full')) return false;
  const queuedByAgent = new Set(actions.filter(action => action.name === 'RunExperiment' && action.args.mode === 'quick')
    .map(action => action.result?.experiment_id).filter(Boolean));
  return experiments.some(row => row.mode === 'quick' && queuedByAgent.has(row.id)
    && EXPERIMENT_TERMINAL.has(row.status) && time(row.created_at) <= finished && time(row.finished_at) > finished);
}

export function recoverRunId(saved, runs) {
  const matched = new Set(runs.filter(run => run.interactionSessionId === saved.interactionId
    && run.inputs?.some(input => input.inputId === saved.inputId)).map(run => run.runId));
  assert.ok(matched.size <= 1, 'Multiple runs accepted the same saved browser input');
  return [...matched][0] ?? null;
}

export function readWorldActions(run, saved, identity) {
  assert.equal(run.runId, saved.runId, 'Run ID mismatch');
  assert.equal(run.interactionSessionId, saved.interactionId, 'Interaction ID mismatch');
  assert.equal(run.agentId, identity.agent, 'Run Agent mismatch');
  assert.equal(run.worldId, identity.world, 'Run World mismatch');
  assert.ok(run.inputs?.some(input => input.inputId === saved.inputId), 'Run does not contain the saved browser input');
  assert.ok(Array.isArray(run.tools), 'Run is missing persisted tool activity');
  return run.tools.filter(tool => tool.toolScope === 'world').map(tool => {
    const { toolName: name, toolCallId: actionId, status, updatedAt } = tool;
    assert.ok(validId(actionId) && typeof name === 'string' && name.length > 0, 'Invalid World action identity');
    assert.equal(tool.worldId, identity.world, 'World action target mismatch');
    const raw = tool.publicArguments ?? {};
    const args = Object.fromEntries(Object.entries(raw)
      .filter(([key]) => ['userId', 'task', 'mode', 'candidate_id', 'experiment_id', 'entrypoint', 'node_id', 'description'].includes(key)));
    if (args.userId !== undefined || (status === 'succeeded' && ['RunExperiment', 'GetExperiment'].includes(name))) {
      assert.equal(args.userId, identity.userId, 'World action user mismatch');
    }
    const base = { runId: run.runId, actionId, name, args, status, updatedAt,
      ...(tool.resultSummary ? { error: tool.resultSummary } : {}) };
    if (status !== 'succeeded') return { ...base, result: null };
    const receipt = tool.publicResult;
    if (identity.graphGuided && receipt?.truncated === true
      && ['SearchResearchMethods', 'GetResearchNode', 'ReadInversqrtReference', 'GetTask'].includes(name)) {
      return { ...base, result: null, outcomeTruncated: true };
    }
    assert.ok(receipt?.status === 'completed' && receipt.worldId === identity.world
      && receipt.toolName === name && receipt.actionId === actionId && validId(receipt.outcomeId)
      && typeof receipt.data?.text === 'string', `Missing or mismatched completed Outcome for ${actionId}`);
    const result = JSON.parse(receipt.data.text);
    assert.ok(result && typeof result === 'object' && !Array.isArray(result), 'World Outcome result must be an object');
    return { ...base, outcomeId: receipt.outcomeId, result };
  });
}
