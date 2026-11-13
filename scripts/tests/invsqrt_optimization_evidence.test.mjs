import assert from 'node:assert/strict';
import test from 'node:test';
import {
  OFFICIAL_SHA256, assessOptimization, needsContinuation, optimizationPrompt,
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

test('accepts the optional quick baseline only with the same official build and comparable verifier settings', () => {
  const { baseline } = fixture();
  const quick = record('exp-official-quick', 'quick', null, 98, OFFICIAL_SHA256);
  assert.equal(requireBaselineQuick(quick, baseline, identity).cost, 98);
  quick.provenance.validation.verifier_sha256 = sha('f');
  assert.throws(() => requireBaselineQuick(quick, baseline, identity), /validation/i);
});
