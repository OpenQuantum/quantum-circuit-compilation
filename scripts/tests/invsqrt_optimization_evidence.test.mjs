import assert from 'node:assert/strict';
import test from 'node:test';
import {
  OFFICIAL_SHA256, assessOptimization, canConfirmFull, needsContinuation, optimizationPrompt,
  recoverRunId, readWorldActions, requireBaseline, requireBaselineQuick,
} from './invsqrt_optimization_evidence.mjs';

const identity = { agent: 'agent-one', world: 'world-one', userId: 'user-one' };
const stamp = minute => `2026-11-13T10:${String(minute).padStart(2, '0')}:00.000Z`;
const sha = letter => letter.repeat(64);
const source = { 'gen.py': sha('a') };
const agent = { sender_id: identity.agent };

function record(id, mode, candidateId, score, circuitHash = sha('b')) {
  const full = mode === 'full';
  const candidate = candidateId ? { id: candidateId, entrypoint: 'gen.py', files: source } : null;
  const shots = full ? 200540928 : 262144;
  const filename = candidate ? 'invsqrt.kmx' : 'invsqrt.kmb';
  const canonicalHash = candidate ? sha('9') : circuitHash;
  return {
    ...agent, id, mode, task: 'invsqrt', candidate_id: candidateId, status: 'succeeded',
    created_at: stamp(candidate ? (full ? 5 : 2) : 0),
    started_at: stamp(candidate ? (full ? 5 : 2) : 0),
    finished_at: stamp(candidate ? (full ? 10 : 4) : 1),
    result: { status: 'PASS', full, task_cost: full ? score : null, diagnostic_cost: score, verified_shots: shots },
    provenance: {
      input_kind: candidate ? 'candidate' : 'official_baseline',
      seed: { filename: 'invsqrt.kmb', sha256: OFFICIAL_SHA256, source: 'official_sample_submission' },
      candidate,
      validation: {
        module: 'invsqrt.verify_submission_invsqrt', verifier_sha256: sha('c'),
        preflight_sha256: sha('d'), metric_sha256: sha('e'), mode, max_x: full ? null : 4096,
        max_workers: 2,
      },
      built_artifacts: { [filename]: circuitHash },
      verifier_report: { status: 'PASS', score, verified_shots: shots, circuit_sha256: canonicalHash },
    },
  };
}

function fixture(cost = 80) {
  const baseline = record('exp-baseline', 'full', null, 100, OFFICIAL_SHA256);
  const quick = record('exp-quick', 'quick', 'cand-new', 77);
  const full = record('exp-full', 'full', 'cand-new', cost);
  const candidate = { ...agent, id: 'cand-new', task: 'invsqrt', entrypoint: 'gen.py', files: source };
  const actions = [
    { name: 'CreateCandidate', args: { task: 'invsqrt' }, result: { candidate_id: candidate.id, files: source, task: 'invsqrt' } },
    { name: 'RunExperiment', args: { candidate_id: candidate.id, task: 'invsqrt', mode: 'quick' }, result: { experiment_id: quick.id, status: 'queued' } },
    { name: 'GetExperiment', args: { experiment_id: quick.id }, result: structuredClone(quick) },
    { name: 'RunExperiment', args: { candidate_id: candidate.id, task: 'invsqrt', mode: 'full' }, result: { experiment_id: full.id, status: 'queued' } },
  ];
  const logs = Object.fromEntries([baseline, quick, full].map(row => [row.id, {
    status: 'PASS', score: row.result.diagnostic_cost, verified_shots: row.result.verified_shots,
    circuit_sha256: row.provenance.verifier_report.circuit_sha256,
  }]));
  return { identity, baseline, experiments: [quick, full], candidates: [candidate], actions, logs };
}

test('measures improvement against comparable full runs of the official baseline and the chosen quick build', () => {
  const report = assessOptimization(fixture());
  assert.equal(report.status, 'improved');
  assert.equal(report.baselineCost, 100);
  assert.equal(report.fullCost, 80);
  assert.equal(report.reduction, 20);
  assert.equal(report.selectedQuickId, 'exp-quick');
  assert.deepEqual(report.build, { filename: 'invsqrt.kmx', sha256: sha('b'), circuitSha256: sha('9') });
});

test('reports a verified non-improvement without substituting the lower quick score', () => {
  for (const cost of [100, 120]) {
    const report = assessOptimization(fixture(cost));
    assert.equal(report.status, 'no_improvement');
    assert.equal(report.fullCost, cost);
  }
});

test('reports failed full, no full and still pending work separately', () => {
  const item = fixture();
  item.experiments[1].status = 'failed';
  item.experiments[1].error = { code: 'VERIFICATION_FAILED' };
  item.experiments[1].result = null;
  assert.equal(assessOptimization(item).status, 'full_failed');
  item.experiments[1].status = 'running';
  assert.equal(assessOptimization(item).status, 'experiment_pending');
  item.experiments.pop();
  item.actions.pop();
  assert.equal(assessOptimization(item).status, 'full_not_run');
});

test('rejects cached history and configured seed as the official baseline', () => {
  const item = fixture();
  item.baseline.provenance.input_kind = 'configured_seed';
  assert.throws(() => requireBaseline(item.baseline, item.identity), /official baseline/i);
  const changed = fixture().baseline;
  changed.provenance.built_artifacts['invsqrt.kmb'] = sha('f');
  assert.throws(() => requireBaseline(changed, item.identity), /official baseline/i);
  assert.throws(() => requireBaseline({ task: 'invsqrt', task_cost: 100 }, item.identity), /baseline/i);
});

test('rejects noncomparable settings, cost, full validation coverage and circuit hashes', () => {
  for (const [change, expected] of [
    [item => { item.experiments[1].provenance.validation.metric_sha256 = sha('f'); }, /validation/i],
    [item => { item.experiments[1].result.task_cost = Infinity; }, /finite/i],
    [item => { item.experiments[1].result.verified_shots = 262144; }, /shots/i],
    [item => { item.experiments[1].provenance.built_artifacts['invsqrt.kmx'] = sha('f'); }, /hash|build/i],
    [item => {
      item.experiments[1].provenance.verifier_report.circuit_sha256 = sha('f');
      item.logs['exp-full'].circuit_sha256 = sha('f');
    }, /hash|build/i],
    [item => { item.logs['exp-full'].score = 1; }, /log/i],
  ]) {
    const item = fixture();
    change(item);
    assert.throws(() => assessOptimization(item), expected);
  }
});

test('requires the Agent to observe the actual quick outcome before the chosen full run', () => {
  const item = fixture();
  const quickRead = item.actions.splice(2, 1)[0];
  item.actions.push(quickRead);
  assert.throws(() => assessOptimization(item), /quick.*before|before.*quick/i);
  const bad = fixture();
  bad.actions[2].result.result.diagnostic_cost = 20;
  assert.throws(() => assessOptimization(bad), /quick.*record|record.*quick/i);
});

test('rejects foreign candidates, mismatched IDs, and out-of-budget attempts', () => {
  const foreign = fixture();
  foreign.candidates[0].sender_id = 'other-agent';
  assert.throws(() => assessOptimization(foreign), /candidate/i);
  const mismatch = fixture();
  mismatch.actions[3].args.candidate_id = 'cand-other';
  assert.throws(() => assessOptimization(mismatch), /candidate/i);
  const over = fixture();
  over.actions.push(...[1, 2, 3].map(() => ({ ...over.actions[1], result: null })));
  assert.throws(() => assessOptimization(over), /budget/i);
});

test('only recommends continuation for a terminal run that stopped with pending quick work', () => {
  const item = fixture();
  const quick = item.experiments[0];
  const actions = item.actions.slice(0, 2);
  const ended = { status: 'succeeded', timing: { finishedAt: stamp(3) } };
  assert.equal(needsContinuation(ended, actions, [quick]), true);
  assert.equal(needsContinuation({ ...ended, timing: { finishedAt: stamp(5) } }, actions, [quick]), false);
  assert.equal(needsContinuation({ ...ended, status: 'running' }, actions, [quick]), false);
  assert.equal(needsContinuation(ended, item.actions, item.experiments), false);
  assert.equal(needsContinuation({ ...ended, status: 'failed', error: { code: 'NETWORK' } }, actions, [quick]), false);
  assert.equal(needsContinuation({ ...ended, status: 'failed', error: { code: 'GENERATION_BUDGET_EXCEEDED' } }, actions, [quick]), true);
});

test('binds the run and Outcome to browser input, Agent, World and action identities', () => {
  const saved = { runId: 'run-1', inputId: 'input-1', interactionId: 'chat-1' };
  const run = {
    runId: saved.runId, interactionSessionId: saved.interactionId, agentId: identity.agent,
    worldId: identity.world, inputs: [{ inputId: saved.inputId }],
    tools: [{
      toolScope: 'world', toolName: 'GetTask', status: 'succeeded', worldId: identity.world,
      toolCallId: 'action-1', updatedAt: stamp(1), publicArguments: { task: 'invsqrt', userId: identity.userId },
      publicResult: { worldId: identity.world, toolName: 'GetTask', status: 'completed',
        actionId: 'action-1', outcomeId: 'outcome-1', data: { text: '{"task":"invsqrt"}' } },
    }],
  };
  assert.equal(readWorldActions(run, saved, identity)[0].result.task, 'invsqrt');
  assert.throws(() => readWorldActions(run, { ...saved, inputId: 'other' }, identity), /input/i);
  run.tools[0].publicResult.actionId = 'wrong';
  assert.throws(() => readWorldActions(run, saved, identity), /outcome/i);
});

test('observes active actions without terminal receipts and still rejects missing terminal evidence', () => {
  const saved = { runId: 'run-1', inputId: 'input-1', interactionId: 'chat-1' };
  const run = {
    runId: saved.runId, interactionSessionId: saved.interactionId, agentId: identity.agent,
    worldId: identity.world, inputs: [{ inputId: saved.inputId }],
    tools: [{
      toolScope: 'world', toolName: 'RunExperiment', status: 'running', worldId: identity.world,
      toolCallId: 'action-1', updatedAt: stamp(1), publicResult: { status: 'accepted' },
    }],
  };
  assert.equal(readWorldActions(run, saved, identity)[0].result, null);
  run.tools[0].status = 'succeeded';
  assert.throws(() => readWorldActions(run, saved, identity), /user|outcome/i);
});

test('recovers only a unique accepted run for the durable browser input', () => {
  const saved = { inputId: 'input-1', interactionId: 'chat-1' };
  const run = { runId: 'run-1', interactionSessionId: 'chat-1', inputs: [{ inputId: 'input-1' }] };
  assert.equal(recoverRunId(saved, [run]), 'run-1');
  assert.equal(recoverRunId(saved, [{ ...run, interactionSessionId: 'another' }]), null);
  assert.throws(() => recoverRunId(saved, [run, { ...run, runId: 'run-2' }]), /multiple/i);
});

test('prompt asks for an autonomous goal and bounded budget without prescribing tool names', () => {
  const prompt = optimizationPrompt('exp-official', 'exp-official-quick');
  assert.match(prompt, /exp-official/);
  assert.match(prompt, /exp-official-quick/);
  assert.match(prompt, /最多 3 次 quick/);
  assert.match(prompt, /最多 1 次 full/);
  assert.match(prompt, /本地分析和预筛选/);
  assert.match(prompt, /电路有实际改动/);
  assert.doesNotMatch(prompt, /GetTask|GetExperiment|RunExperiment|CreateCandidate/);
});

test('operator confirmation requires an Agent-observed improving quick and unused full budget', () => {
  const item = fixture();
  const latest = { status: 'succeeded', modelDecision: [{ text: '是否执行这个候选的 full 验证？' }] };
  const quick = item.experiments[0];
  const actions = item.actions.slice(0, -1);
  assert.equal(canConfirmFull(latest, actions, [quick], { result: { diagnostic_cost: 98 } }), true);
  assert.equal(canConfirmFull(latest, item.actions, [quick], { result: { diagnostic_cost: 98 } }), false);
  assert.equal(canConfirmFull(latest, actions.slice(0, -1), [quick], { result: { diagnostic_cost: 98 } }), false);
  assert.equal(canConfirmFull(latest, actions, [quick], { result: { diagnostic_cost: 60 } }), false);
});

test('an optimization retry can cite prior experiment evidence without prescribing a solution', () => {
  const prompt = optimizationPrompt('exp-official', 'exp-official-quick', ['exp-prior']);
  assert.match(prompt, /exp-prior/);
  assert.match(prompt, /此前尝试/);
  assert.doesNotMatch(prompt, /GetTask|GetExperiment|RunExperiment|CreateCandidate/);
  assert.throws(() => optimizationPrompt('exp-official', null, ['../outside']));
});

test('accepts the optional quick baseline only with the same official build and comparable verifier settings', () => {
  const { baseline } = fixture();
  const quick = record('exp-official-quick', 'quick', null, 98, OFFICIAL_SHA256);
  assert.equal(requireBaselineQuick(quick, baseline, identity).cost, 98);
  quick.provenance.validation.verifier_sha256 = sha('f');
  assert.throws(() => requireBaselineQuick(quick, baseline, identity), /validation/i);
});

const optimizedHash = '561687a62b2b562bf72477ef11cedee81e99199e15bf10347490d0258e47352a';

function graphFixture(cost = 80) {
  const item = fixture(cost);
  const baselineCandidate = { id: 'cand-previous', entrypoint: 'invsqrt_gen.py', files: { 'invsqrt_gen.py': sha('a') } };
  item.baseline.candidate_id = baselineCandidate.id;
  item.baseline.provenance.input_kind = 'candidate';
  item.baseline.provenance.candidate = baselineCandidate;
  item.baseline.provenance.built_artifacts['invsqrt.kmb'] = optimizedHash;
  item.baseline.provenance.verifier_report.circuit_sha256 = optimizedHash;
  item.logs[item.baseline.id].circuit_sha256 = optimizedHash;
  item.identity = { ...item.identity, graphGuided: true };
  const node = { id: 'method-a', task: 'invsqrt', title: 'Narrow the accumulator', summary: 'Reduce the accumulator width.',
    source: { type: 'public', url: 'https://example.test/method', version: '2' }, evidence: 'Accumulator width example' };
  const result = { task: 'invsqrt', scope: 'public_and_official_only', read_only: true, nodes: [node] };
  item.actions.unshift(
    { name: 'SearchResearchMethods', args: { task: 'invsqrt' }, result: structuredClone(result), actionId: 'search-a', outcomeId: 'search-result' },
    { name: 'GetResearchNode', args: { task: 'invsqrt', node_id: node.id }, result, actionId: 'detail-a', outcomeId: 'detail-result' },
  );
  const description = JSON.stringify({ method_node_id: node.id, based_on: 'prior optimized reference', changes: 'narrow the accumulator by one bit' });
  item.candidates[0].description = description;
  item.actions[2].args.description = description;
  return item;
}

test('records research and baseline evidence for a graph-guided run against the optimized reference', () => {
  const report = assessOptimization(graphFixture());
  assert.equal(report.status, 'improved');
  assert.equal(report.reductionPct, 20);
  assert.equal(report.nextBaselineId, 'exp-full');
  assert.equal(report.research[0].candidateId, 'cand-new');
  assert.equal(report.research[0].node.id, 'method-a');
  assert.equal(report.research[0].node.source.version, '2');
  assert.equal(report.research[0].detail.outcomeId, 'detail-result');
  assert.equal(report.research[0].changes, 'narrow the accumulator by one bit');
});

test('requires graph evidence linked to the created candidate and read before creation', () => {
  const invalid = [
    item => { item.actions[0].result.nodes[0].id = 'method-b'; },
    item => { item.actions[1].result = null; },
    item => { item.actions.splice(2, 0, item.actions.shift()); },
    item => { item.candidates[0].description = '{}'; },
    item => { item.actions[2].args.description = 'different from World record'; },
  ];
  for (const change of invalid) {
    const item = graphFixture();
    change(item);
    assert.throws(() => assessOptimization(item), /research|method|candidate description/i);
  }
});

test('preserves a verified result with no improvement and does not advance the baseline', () => {
  const report = assessOptimization(graphFixture(100));
  assert.equal(report.status, 'no_improvement');
  assert.equal(report.research[0].node.id, 'method-a');
  assert.equal(report.nextBaselineId, 'exp-baseline');
});

test('graph-guided baseline must be the prior optimized circuit with full verifier evidence', () => {
  const wrongCircuit = graphFixture();
  wrongCircuit.baseline.provenance.built_artifacts['invsqrt.kmb'] = OFFICIAL_SHA256;
  assert.throws(() => assessOptimization(wrongCircuit), /optimized.*reference/i);
  const wrongMode = graphFixture();
  wrongMode.baseline.mode = 'quick';
  assert.throws(() => assessOptimization(wrongMode), /baseline/i);
});

test('graph-guided prompt requests real lookup and candidate linkage without supplying a method', () => {
  const prompt = optimizationPrompt('exp-baseline', undefined, [], { graphGuided: true });
  assert.match(prompt, /SearchResearchMethods[\s\S]*GetResearchNode/);
  assert.match(prompt, /method_node_id/);
  assert.match(prompt, /product_bits=27/);
  assert.doesNotMatch(prompt, /H14|delta_bits|result_bits/);
});

test('graph-guided quick baseline must match the selected full baseline artifact and candidate', () => {
  const item = graphFixture();
  const quick = record('exp-baseline-quick', 'quick', 'cand-previous', 90, optimizedHash);
  quick.provenance = { ...structuredClone(item.baseline.provenance), validation: quick.provenance.validation,
    verifier_report: { ...quick.provenance.verifier_report, circuit_sha256: optimizedHash } };
  assert.equal(requireBaselineQuick(quick, item.baseline, item.identity).cost, 90);
  quick.candidate_id = 'cand-other';
  quick.provenance.candidate.id = 'cand-other';
  assert.throws(() => requireBaselineQuick(quick, item.baseline, item.identity), /same.*candidate/i);
});

test('confirmed quick comparison can use the optimized baseline rather than the original sample', () => {
  const item = graphFixture();
  item.actions.pop();
  const run = { status: 'succeeded', modelDecision: 'Select the tested candidate' };
  const quickBaseline = { result: { diagnostic_cost: 90 }, provenance: item.baseline.provenance };
  assert.equal(canConfirmFull(run, item.actions, item.experiments, quickBaseline), true);
  item.experiments[0].provenance.built_artifacts = item.baseline.provenance.built_artifacts;
  assert.equal(canConfirmFull(run, item.actions, item.experiments, quickBaseline), false);
});

test('graph-guided acceptance rejects the baseline circuit under a new candidate ID', () => {
  const item = graphFixture();
  for (const row of item.experiments) {
    row.provenance.verifier_report.circuit_sha256 = optimizedHash;
    item.logs[row.id].circuit_sha256 = optimizedHash;
  }
  item.actions.find(action => action.name === 'GetExperiment').result = structuredClone(item.experiments[0]);
  assert.throws(() => assessOptimization(item), /same circuit|baseline circuit/i);
});
test('graph-guided retries include a bounded search and remaining quick allowance', () => {
  const prompt = optimizationPrompt('exp-baseline', 'exp-quick-baseline', ['exp-tried'], {
    graphGuided: true, quickBudget: 2,
  });
  assert.match(prompt, /短关键词/);
  assert.match(prompt, /limit=2/);
  assert.match(prompt, /最多 2 次 quick/);
  assert.match(prompt, /exp-tried/);
});
test('retains truncated research lookups without treating previews as successful evidence', () => {
  const saved = { runId: 'run-1', inputId: 'input-1', interactionId: 'chat-1' };
  const run = {
    runId: saved.runId, interactionSessionId: saved.interactionId, agentId: identity.agent,
    worldId: identity.world, inputs: [{ inputId: saved.inputId }],
    tools: [{
      toolScope: 'world', toolName: 'SearchResearchMethods', status: 'succeeded', worldId: identity.world,
      toolCallId: 'action-1', publicArguments: { task: 'invsqrt', userId: identity.userId },
      publicResult: { truncated: true, preview: 'An incomplete result' },
    }],
  };
  const [search] = readWorldActions(run, saved, { ...identity, graphGuided: true });
  assert.equal(search.result, null);
  assert.equal(search.outcomeTruncated, true);
  run.tools[0].toolName = 'RunExperiment';
  assert.throws(() => readWorldActions(run, saved, { ...identity, graphGuided: true }), /outcome/i);
});
test('records a structured field-by-field change summary in the World candidate', () => {
  const item = graphFixture();
  const candidate = item.candidates[0];
  const changes = { delta_bits: '14 → 13', result_bits: '29 → 28' };
  candidate.description = JSON.stringify({ ...JSON.parse(candidate.description), changes });
  item.actions[2].args.description = candidate.description;
  assert.deepEqual(assessOptimization(item).research[0].changes, changes);
});