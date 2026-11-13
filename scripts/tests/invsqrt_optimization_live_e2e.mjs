// Opt-in live acceptance. Reuse the state file for the same optimization goal.
import assert from 'node:assert/strict';
import { mkdir, readFile, rename, writeFile } from 'node:fs/promises';
import { createRequire } from 'node:module';
import { dirname, resolve } from 'node:path';
import { randomUUID } from 'node:crypto';
import { setTimeout as delay } from 'node:timers/promises';
import { fileURLToPath } from 'node:url';
import { parseArgs } from 'node:util';
import { orderedMessages } from '../../frontend/model.js';
import {
  BUDGET, EXPERIMENT_TERMINAL, RUN_TERMINAL, assessOptimization, needsContinuation,
  optimizationPrompt, readWorldActions, recoverRunId, requireBaseline, requireBaselineQuick,
} from './invsqrt_optimization_evidence.mjs';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '../..');
const H = '/api/human/v1';
const Q = '/api/quantum/v1';
const runPattern = /^\/api\/human\/v1\/interactions\/([^/]+)\/runs$/;
const routePattern = '**/api/human/v1/interactions/*/runs';
const inputRoutePattern = '**/api/human/v1/runs/*/inputs';
const { values: options } = parseArgs({ options: {
  'base-url': { type: 'string', default: 'http://127.0.0.1:8330' },
  'login-code-file': { type: 'string', default: resolve(root, '.local-console/live/login-code') },
  'state-file': { type: 'string', default: resolve(root, '.local-console/live/invsqrt-optimization-evidence.json') },
  'baseline-experiment-id': { type: 'string' },
  'baseline-quick-experiment-id': { type: 'string' },
  'continue-pending': { type: 'boolean', default: false },
  playwright: { type: 'string', default: 'playwright' },
  user: { type: 'string', default: 'local-user' },
  agent: { type: 'string', default: 'quantum-agent-local' },
  world: { type: 'string', default: 'quantum-world-invsqrt-local' },
  'timeout-seconds': { type: 'string', default: '3600' },
  help: { type: 'boolean', short: 'h', default: false },
} });

const stamp = () => new Date().toISOString();
const encode = encodeURIComponent;
const stateFile = resolve(options['state-file']);
const intentPath = index => `${stateFile}.input-${index}.json`;
const runPath = runId => `${H}/runs/${encode(runId)}`;
const experimentPath = id => `${Q}/experiments/${encode(id)}`;
const baselineId = options['baseline-experiment-id'];
const quickBaselineId = options['baseline-quick-experiment-id'] ?? null;
const timeout = Number(options['timeout-seconds']) * 1000;

async function readJson(path) {
  try {
    return JSON.parse(await readFile(path, 'utf8'));
  } catch (error) {
    if (error.code === 'ENOENT') return null;
    throw error;
  }
}

async function writeJson(path, data, exclusive = false) {
  const target = exclusive ? path : `${path}.${randomUUID()}.tmp`;
  await writeFile(target, JSON.stringify(data, null, 2) + '\n', { flag: 'wx', mode: 0o600 });
  if (!exclusive) await rename(target, path);
}

function readIntent(request, prompt) {
  if (request.method() !== 'POST') return null;
  const path = runPattern.exec(new URL(request.url()).pathname);
  if (!path) return null;
  const body = request.postDataJSON();
  if (!body?.parts?.some(part => part.type === 'text' && part.text === prompt)) return null;
  assert.match(body.inputId, /^[A-Za-z0-9_-]+$/, 'Browser run request lacks its idempotency input ID');
  return { inputId: body.inputId, interactionId: decodeURIComponent(path[1]), submittedAt: stamp() };
}

async function sendGoal(page, state, prompt, priorInteraction = null) {
  await page.locator('#chat-input').fill(prompt);
  let intent;
  let writeFailure;
  const index = state.attempts.length;
  const intercept = async route => {
    try {
      const path = new URL(route.request().url()).pathname;
      assert.ok(!/^\/api\/human\/v1\/runs\/[^/]+\/inputs$/.test(path), 'Browser Run has not settled; refusing an append');
      const request = readIntent(route.request(), prompt);
      if (!request) {
        assert.notEqual(route.request().method(), 'POST', 'Unexpected browser Run input');
        return route.continue();
      }
      assert.ok(!intent, 'A browser attempted to repeat the same live input');
      if (priorInteraction) assert.equal(request.interactionId, priorInteraction, 'Continuation changed conversation');
      intent = { ...request, prompt };
      // An exclusive intent file survives a lost response and blocks a concurrent send.
      await writeJson(intentPath(index), intent, true);
    } catch (error) {
      writeFailure = error;
      return route.abort();
    }
    return route.continue();
  };
  await page.route(routePattern, intercept);
  await page.route(inputRoutePattern, intercept);
  try {
    const [response] = await Promise.all([
      page.waitForResponse(response => !!readIntent(response.request(), prompt), { timeout: 25000 }),
      page.locator('#send-chat').click(),
    ]);
    assert.equal(response.status(), 201, 'Browser did not acknowledge the run request');
    assert.ok(intent, 'Missing durable browser input');
    const ack = await response.json();
    assert.match(ack.runId, /^[A-Za-z0-9_-]+$/, 'Run acknowledgement lacks a valid run ID');
    state.attempts.push({ ...intent, runId: ack.runId });
    await writeJson(stateFile, state);
  } catch (error) {
    throw writeFailure ?? error;
  } finally {
    await page.unroute(routePattern, intercept);
    await page.unroute(inputRoutePattern, intercept);
  }
}

async function saveTerminalPageEvidence(page, run, interactionId, verdict, state) {
  if (run.status !== 'succeeded' || !verdict.fullId || !['improved', 'no_improvement'].includes(verdict.status)) return;
  await page.locator('#refresh').click();
  await page.locator(`#records [data-select="${verdict.fullId}"]`).click();
  await page.locator('#detail').getByText(verdict.fullId, { exact: true }).waitFor({ timeout: 15000 });
  await page.locator('#log-name').selectOption('verify.stdout');
  await page.waitForFunction(expected => {
    try {
      const report = JSON.parse(document.querySelector('#log-text')?.textContent);
      return report.status === 'PASS' && report.score === expected;
    } catch {
      return false;
    }
  }, verdict.fullCost, { timeout: 15000 });
  await reconcileConversation(page, run, interactionId, Date.now() + 15000);
  await page.locator('#chat-messages .message.agent .message-text').last().waitFor({ timeout: 15000 });
  const screenshot = `${stateFile}.png`;
  await page.screenshot({ path: screenshot, fullPage: true });
  state.pageEvidence = { screenshot, capturedAt: stamp(), displayedFullId: verdict.fullId, interactionId };
}

async function history(get, interactionId) {
  const pages = [];
  const seen = new Set();
  let cursor = null;
  do {
    const page = await get(`${H}/interactions/${encode(interactionId)}/messages?limit=100${cursor ? '&cursor=' + encode(cursor) : ''}`);
    pages.push(page);
    cursor = page.nextCursor;
    assert.ok(!cursor || !seen.has(cursor), 'Repeated conversation pagination cursor');
    seen.add(cursor);
  } while (cursor);
  return {
    messages: orderedMessages(pages),
    runs: [...new Set(pages.flatMap(page => (page.runs ?? []).map(run => run.runId)))],
  };
}

async function waitFor(label, deadline, check) {
  while (Date.now() < deadline) {
    const value = await check();
    if (value) return value;
    await delay(Math.min(1000, Math.max(0, deadline - Date.now())));
  }
  throw new Error(`Timed out waiting for ${label}; reuse the same state file to observe it again`);
}

async function resumeIntent(get, state, deadline) {
  const saved = await readJson(intentPath(state.attempts.length));
  if (!saved) return;
  const runId = await waitFor('acknowledgement of the saved browser input', deadline, async () => {
    const { runs } = await history(get, saved.interactionId);
    return recoverRunId(saved, await Promise.all(runs.map(id => get(runPath(id)))));
  });
  state.attempts.push({ ...saved, runId });
  await writeJson(stateFile, state);
}

async function verifyLog(get, id, required = true) {
  const path = `${experimentPath(id)}/logs?name=verify.stdout&offset=0&limit=131072`;
  const snapshot = await get(path, { optional: !required });
  if (!snapshot) return null;
  assert.ok(snapshot.eof && snapshot.experiment_id === id && snapshot.name === 'verify.stdout',
    `Missing or incomplete verify.stdout for ${id}`);
  let report;
  try {
    report = JSON.parse(snapshot.text);
  } catch (error) {
    if (required) throw error;
    report = null;
  }
  return { path, observedAt: stamp(), sizeBytes: snapshot.size_bytes, report,
    ...(!report ? { text: snapshot.text } : {}) };
}

function newIds(rows, previous, agent) {
  return rows.filter(row => row.sender_id === agent && !previous.includes(row.id)).map(row => row.id);
}

function modelDecision(messages, runId) {
  return messages.filter(message => message.runId === runId && message.role === 'agent'
    && message.status === 'complete').map(message => ({
    messageId: message.id, createdAt: message.createdAt,
    text: (message.parts ?? []).filter(part => part.type === 'text').map(part => part.text).join('\n'),
  })).filter(message => message.text.trim());
}

async function observe(get, state, deadline) {
  const identity = state.identity;
  let runs;
  let actions;
  let expected;
  let experiments;
  let fingerprint;
  // Observe durable actions even when an Agent Run ends unsuccessfully.
  for (;;) {
    runs = await Promise.all(state.attempts.map(attempt => get(runPath(attempt.runId))));
    actions = runs.flatMap((run, index) => readWorldActions(run, state.attempts[index], identity));
    expected = actions.filter(action => action.name === 'RunExperiment' && action.result)
      .map(action => action.result.experiment_id);
    experiments = await Promise.all(expected.map(id => get(experimentPath(id))));
    const progress = {
      runs: runs.map(run => ({
        runId: run.runId, status: run.status, startedAt: run.timing?.startedAt,
        finishedAt: run.timing?.finishedAt, error: run.error ?? null,
      })),
      actions, experiments,
    };
    const snapshot = JSON.stringify(progress);
    if (fingerprint !== snapshot) {
      fingerprint = snapshot;
      state.progress = { observedAt: stamp(), ...progress };
      await writeJson(stateFile, state);
    }
    assert.equal(new Set(actions.map(action => action.actionId)).size, actions.length, 'Repeated World action ID across Runs');
    for (const mode of Object.keys(BUDGET)) {
      const count = actions.filter(action => action.name === 'RunExperiment' && action.args.mode === mode).length;
      assert.ok(count <= BUDGET[mode], `Agent exceeded ${mode} experiment budget`);
    }
    if ((runs.every(run => RUN_TERMINAL.has(run.status))
      && experiments.every(row => EXPERIMENT_TERMINAL.has(row.status))) || Date.now() >= deadline) break;
    await delay(Math.min(1000, Math.max(0, deadline - Date.now())));
  }
  const [candidateList, experimentList, transcript] = await Promise.all([
    get(`${Q}/candidates`), get(`${Q}/experiments`), history(get, state.attempts[0].interactionId),
  ]);
  const candidateIds = newIds(candidateList.items, state.preExistingCandidates, identity.agent);
  const experimentIds = newIds(experimentList.items, state.preExistingExperiments, identity.agent);
  const candidates = candidateList.items.filter(row => candidateIds.includes(row.id));
  const extraIds = experimentIds.filter(id => !expected.includes(id));
  experiments.push(...experimentList.items.filter(row => extraIds.includes(row.id)));
  const logs = {};
  for (const row of experiments) {
    if (EXPERIMENT_TERMINAL.has(row.status)) logs[row.id] = await verifyLog(get, row.id, row.status === 'succeeded');
  }
  const trace = {
    observedAt: stamp(),
    runs: runs.map(run => ({
      runId: run.runId, interactionId: run.interactionSessionId, status: run.status, timing: run.timing,
      error: run.error ?? null, modelDecision: modelDecision(transcript.messages, run.runId),
    })),
    actions, candidates, experiments, logs,
  };
  state.trace = trace;
  await writeJson(stateFile, state);
  return { runs, trace };
}

async function reconcileConversation(page, run, interactionId, deadline) {
  // The saved conversation can be outside the first page after a restart.
  while (await page.locator('#conversation-select option').evaluateAll(
    (items, id) => !items.some(item => item.value === id), interactionId,
  )) {
    const more = page.locator('#more-conversations');
    assert.ok(await more.isVisible(), 'Saved conversation is absent from the browser directory');
    const optionCount = await page.locator('#conversation-select option').count();
    await more.click();
    await page.waitForFunction(previous => document.querySelector('#conversation-select').options.length > previous
      || document.querySelector('#more-conversations').hidden, optionCount, { timeout: 15000 });
    assert.ok(Date.now() < deadline, 'Timed out reopening the saved conversation');
  }
  await page.locator('#conversation-select').selectOption(interactionId);
  const terminalLabel = run.status === 'succeeded' ? 'Agent 已完成' : '失败';
  await page.locator('#chat-state').filter({ hasText: terminalLabel }).waitFor({ timeout: 15000 });
}

async function main() {
  const base = new URL(options['base-url']);
  assert.ok(['127.0.0.1', 'localhost', '[::1]'].includes(base.hostname)
    && ['http:', 'https:'].includes(base.protocol) && base.href === `${base.origin}/`,
  'Use a local Console origin as --base-url');
  assert.ok(Number.isFinite(timeout) && timeout > 0, 'Expected positive --timeout-seconds');
  const prompt = optimizationPrompt(baselineId, quickBaselineId);
  const identity = { baseUrl: base.origin, userId: options.user, agent: options.agent, world: options.world,
    baselineId, quickBaselineId };
  const deadline = Date.now() + timeout;
  const ready = await fetch(`${base.origin}/health/ready`, { signal: AbortSignal.timeout(3500) }).catch(() => null);
  assert.ok(ready?.ok, `BFF not ready at ${base.origin}/health/ready`);
  const { chromium } = createRequire(import.meta.url)(options.playwright);
  const browser = await chromium.launch({ channel: 'chrome', headless: true });
  let state;
  try {
    const context = await browser.newContext();
    const page = await context.newPage();
    await page.goto(base.origin);
    try {
      const code = (await readFile(options['login-code-file'], 'utf8')).trim();
      assert.ok(code);
      await page.locator('#login-user').fill(options.user);
      await page.locator('#access-code').fill(code);
      await page.locator('#login-form button[type=submit]').click();
      await page.locator('#workspace').waitFor({ state: 'visible' });
    } catch {
      throw new Error('Console login failed; check the user and --login-code-file');
    }
    const get = async (path, { optional = false } = {}) => {
      const response = await context.request.get(base.origin + path, { timeout: 15000 });
      if (optional && response.status() === 404) return null;
      assert.ok(response.ok(), `GET ${path} returned HTTP ${response.status()}`);
      return response.json();
    };
    const summary = await get(`${Q}/summary`);
    assert.equal(summary.transport_mode, 'sp', 'Expected real Star Protocol transport');
    assert.equal(summary.read_only, true, 'Keep the operator panel read-only for optimization acceptance');
    assert.equal(summary.user_id, options.user);
    assert.equal(summary.world_id, options.world);
    await page.waitForFunction(agentId => Array.from(document.querySelector('#agent-select').options)
      .some(option => option.value === agentId), options.agent);
    const baseline = await get(experimentPath(baselineId));
    assert.equal(baseline.id, baselineId);
    const baselineLog = await verifyLog(get, baselineId);
    requireBaseline(baseline, identity, baselineLog.report);
    let quickBaseline = null;
    if (quickBaselineId) {
      const row = await get(experimentPath(quickBaselineId));
      assert.equal(row.id, quickBaselineId);
      const log = await verifyLog(get, quickBaselineId);
      requireBaselineQuick(row, baseline, identity, log.report);
      quickBaseline = { row, log };
    }
    state = await readJson(stateFile);
    if (state) {
      assert.equal(state.version, 1);
      assert.deepEqual(state.identity, identity, 'State belongs to another Console or baseline');
      assert.equal(state.prompt, prompt, 'Saved input differs from the requested optimization goal');
    } else {
      const [experiments, candidates] = await Promise.all([get(`${Q}/experiments`), get(`${Q}/candidates`)]);
      state = {
        version: 1, identity, prompt, createdAt: stamp(), baseline, baselineLog, quickBaseline,
        preExistingExperiments: experiments.items.map(row => row.id),
        preExistingCandidates: candidates.items.map(row => row.id), attempts: [],
      };
      await mkdir(dirname(stateFile), { recursive: true });
      await writeJson(stateFile, state, true);
    }
    await resumeIntent(get, state, deadline);
    if (!state.attempts.length) {
      await page.locator('#new-chat').click();
      await page.locator('#agent-select').selectOption(options.agent);
      await sendGoal(page, state, prompt);
    }
    let continued = false;
    for (;;) {
      const { runs, trace } = await observe(get, state, deadline);
      const latest = runs.at(-1);
      const verdict = assessOptimization({
        identity, baseline, actions: trace.actions, experiments: trace.experiments, candidates: trace.candidates,
        logs: { [baselineId]: baselineLog.report,
          ...Object.fromEntries(Object.entries(trace.logs).map(([id, log]) => [id, log?.report])) },
      });
      const continuationAllowed = verdict.status === 'full_not_run' && state.attempts.length < 3
        && needsContinuation(latest, trace.actions, trace.experiments);
      if (options['continue-pending'] && continuationAllowed && !continued && Date.now() < deadline) {
        const interactionId = state.attempts[0].interactionId;
        await reconcileConversation(page, latest, interactionId, deadline);
        const prompt = '请接续这段对话里尚未完成的原目标，根据已经完成的实验结果继续判断。沿用原预算，不重复已登记的实验。';
        await sendGoal(page, state, prompt, interactionId);
        continued = true;
        continue;
      }
      const measuredStatus = verdict.status;
      if (!RUN_TERMINAL.has(latest.status)) verdict.status = 'agent_pending';
      else if (latest.status !== 'succeeded' && ['improved', 'no_improvement'].includes(verdict.status)) {
        verdict.status = 'agent_failed_after_verification';
      }
      else if (continuationAllowed) verdict.status = 'continuation_needed';
      else if (verdict.status === 'full_not_run' && latest.status !== 'succeeded') verdict.status = 'agent_failed';
      else if (['improved', 'no_improvement'].includes(verdict.status)
        && !trace.runs.at(-1).modelDecision.length) verdict.status = 'model_decision_missing';
      state.report = { ...verdict, measuredStatus, observedAt: stamp(),
        interactionId: state.attempts[0].interactionId, runIds: runs.map(run => run.runId), continuationAllowed };
      await writeJson(stateFile, state);
      await saveTerminalPageEvidence(page, latest, state.report.interactionId, verdict, state);
      if (state.pageEvidence) await writeJson(stateFile, state);
      console.log(JSON.stringify({ ...state.report, stateFile }, null, 2));
      if (verdict.status !== 'improved') process.exitCode = verdict.status === 'no_improvement' ? 2 : 1;
      return;
    }
  } finally {
    await browser.close();
  }
}

if (options.help) {
  console.log('node scripts/tests/invsqrt_optimization_live_e2e.mjs --baseline-experiment-id ID'
    + ' [--baseline-quick-experiment-id ID] [--base-url URL] [--login-code-file FILE]'
    + ' [--state-file FILE] [--playwright PATH] [--user ID] [--agent ID] [--world ID]'
    + ' [--timeout-seconds N] [--continue-pending]');
} else {
  try { await main(); }
  catch (error) {
    console.error(`FAIL: ${error.message}\nEvidence or pending input: ${stateFile}`);
    process.exitCode = 1;
  }
}
