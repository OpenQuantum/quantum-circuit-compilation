// Opt-in live test. Reuse the same state file to resume an uncertain quick request.
import assert from 'node:assert/strict';
import { mkdir, readFile, rename, writeFile } from 'node:fs/promises';
import { createRequire } from 'node:module';
import { dirname, resolve } from 'node:path';
import { randomUUID } from 'node:crypto';
import { setTimeout as delay } from 'node:timers/promises';
import { fileURLToPath } from 'node:url';
import { parseArgs } from 'node:util';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '../..');
const { values: options } = parseArgs({ options: {
  'base-url': { type: 'string', default: 'http://127.0.0.1:8320' },
  'login-code-file': { type: 'string', default: resolve(root, '.local-console/login-code') },
  'state-file': { type: 'string', default: resolve(root, '.local-console/live-browser-evidence.json') },
  playwright: { type: 'string', default: 'playwright' },
  agent: { type: 'string', default: 'quantum-agent-local' },
  world: { type: 'string', default: 'quantum-world-local' },
  'timeout-seconds': { type: 'string', default: '900' },
} });
const userId = 'local-user';
const H = '/api/human/v1';
const Q = '/api/quantum/v1';
const taskPrompt = '请真实调用 Quantum World 的 GetTask 工具，参数 task="invsqrt"。只调用一次 GetTask，不执行其他 World 工具，并据实际回执告诉我任务名称。';
const quickPrompt = '请真实调用 Quantum World 的 RunExperiment 恰好一次，参数 task="invsqrt"、mode="quick"，省略 candidate_id，使用已有 seed。不要再调用其他 World 工具，不要重试或提交比赛。请在取得排队回执后直接返回该 experiment_id，后续由我在实验面板查看。';
const stateFile = resolve(options['state-file']);
const timeout = Number(options['timeout-seconds']) * 1000;
const runPattern = /^\/api\/human\/v1\/interactions\/([^/]+)\/runs$/;

async function waitFor(label, check) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    const result = await check();
    if (result) return result;
    await delay(1000);
  }
  throw new Error(`Timed out waiting for ${label}`);
}

async function savedState() {
  try { return JSON.parse(await readFile(stateFile, 'utf8')); }
  catch (error) { if (error.code === 'ENOENT') return null; throw error; }
}

async function saveState(state, exclusive = false) {
  await mkdir(dirname(stateFile), { recursive: true });
  const content = JSON.stringify(state, null, 2) + '\n';
  if (exclusive) {
    await writeFile(stateFile, content, { flag: 'wx', mode: 0o600 });
  } else {
    const temporary = `${stateFile}.${randomUUID()}.tmp`;
    await writeFile(temporary, content, { flag: 'wx', mode: 0o600 });
    await rename(temporary, stateFile);
  }
}

function isPrompt(request, prompt) {
  return request.method() === 'POST' && runPattern.test(new URL(request.url()).pathname)
    && request.postDataJSON()?.parts?.some(part => part.type === 'text' && part.text === prompt);
}

async function send(page, prompt) {
  await page.locator('#new-chat').click();
  await page.locator('#agent-select').selectOption(options.agent);
  await page.locator('#chat-input').fill(prompt);
  const [response] = await Promise.all([
    page.waitForResponse(response => isPrompt(response.request(), prompt)),
    page.locator('#send-chat').click(),
  ]);
  assert.equal(response.status(), 201, 'Browser run request did not succeed');
  const ack = await response.json();
  const interactionId = decodeURIComponent(runPattern.exec(new URL(response.url()).pathname)[1]);
  const inputId = response.request().postDataJSON().inputId;
  assert.ok(ack.runId && inputId, 'Run acknowledgement lacks runId or inputId');
  console.error(`Browser submitted run ${ack.runId}`);
  return { runId: ack.runId, inputId, interactionId };
}

async function toolResult(get, runIds, toolName, args) {
  const run = await waitFor(`run ${runIds.runId} to succeed`, async () => {
    const snapshot = await get(`${H}/runs/${encodeURIComponent(runIds.runId)}`);
    if (['failed', 'canceled', 'cancelled', 'rejected', 'interrupted'].includes(snapshot.status)) {
      throw new Error(`Run ${runIds.runId} ended ${snapshot.status} (${snapshot.error?.code ?? 'unknown error'})`);
    }
    return snapshot.status === 'succeeded' ? snapshot : null;
  });
  assert.equal(run.agentId, options.agent);
  assert.equal(run.worldId, options.world);
  assert.equal(run.interactionSessionId, runIds.interactionId);
  assert.ok(run.inputs.some(input => input.inputId === runIds.inputId), 'Unexpected run input');
  const worldTools = run.tools.filter(tool => tool.toolScope === 'world');
  assert.deepEqual(worldTools.map(tool => tool.toolName), [toolName], 'Expected exactly one World action');
  const [tool] = worldTools;
  assert.equal(tool.status, 'succeeded');
  assert.equal(tool.worldId, options.world);
  assert.deepEqual(tool.publicArguments, { ...args, userId });
  const receipt = tool.publicResult;
  assert.equal(receipt?.status, 'completed', 'Run has no completed World Outcome');
  assert.equal(receipt.worldId, options.world);
  assert.equal(receipt.toolName, toolName);
  assert.equal(receipt.actionId, tool.toolCallId);
  assert.ok(receipt.outcomeId, 'World Outcome lacks an ID');
  return { ...runIds, actionId: receipt.actionId, outcomeId: receipt.outcomeId,
    result: JSON.parse(receipt.data.text) };
}

async function main() {
  const base = new URL(options['base-url']);
  assert.ok(['127.0.0.1', 'localhost', '[::1]'].includes(base.hostname)
    && ['http:', 'https:'].includes(base.protocol) && base.href === `${base.origin}/`,
  'Use the local Console origin as --base-url');
  assert.ok(Number.isFinite(timeout) && timeout > 0, 'Expected positive --timeout-seconds');
  const ready = await fetch(`${base.origin}/health/ready`, { signal: AbortSignal.timeout(3500) }).catch(() => null);
  assert.ok(ready?.ok, `BFF not ready at ${base.origin}/health/ready${ready ? ` (HTTP ${ready.status})` : ''}`);

  const { chromium } = createRequire(import.meta.url)(options.playwright);
  const browser = await chromium.launch({ channel: 'chrome', headless: true });
  try {
    const context = await browser.newContext();
    const page = await context.newPage();
    await page.goto(base.origin);
    try {
      const code = (await readFile(options['login-code-file'], 'utf8')).trim();
      assert.ok(code);
      await page.locator('#login-user').fill(userId);
      await page.locator('#access-code').fill(code);
      await page.locator('#login-form button[type=submit]').click();
      await page.locator('#workspace').waitFor({ state: 'visible' });
    } catch {
      throw new Error('Console login failed. Check the local-user account and --login-code-file.');
    }
    const get = async path => {
      const response = await context.request.get(base.origin + path);
      assert.ok(response.ok(), `GET ${path} returned HTTP ${response.status()}`);
      return response.json();
    };
    const summary = await get(`${Q}/summary`);
    assert.equal(summary.transport_mode, 'sp', 'Demo transport cannot satisfy this live test');
    assert.equal(summary.user_id, userId);
    assert.equal(summary.world_id, options.world);
    const identity = { baseUrl: base.origin, userId, agent: options.agent, world: options.world };
    let state = await savedState();
    if (state) {
      assert.deepEqual(state.identity, identity, 'State file belongs to another Console');
      console.error(`Resuming saved quick request from ${stateFile}`);
    } else {
      const getTaskRun = await send(page, taskPrompt);
      const getTask = await toolResult(get, getTaskRun, 'GetTask', { task: 'invsqrt' });
      assert.equal(getTask.result.task, 'invsqrt');
      await page.locator('#chat-messages .message.agent .message-text').first().waitFor();
      console.error(`Verified GetTask run ${getTask.runId}, Outcome ${getTask.outcomeId}`);
      const { items } = await get(`${Q}/experiments`);
      const baselineExperimentIds = items.map(item => item.id);
      const routePattern = '**/api/human/v1/interactions/*/runs';
      let writeFailure;
      await page.route(routePattern, async route => {
        const request = route.request();
        if (!isPrompt(request, quickPrompt)) return route.continue();
        const interactionId = decodeURIComponent(runPattern.exec(new URL(request.url()).pathname)[1]);
        const inputId = request.postDataJSON().inputId;
        try {
          assert.ok(inputId, 'Browser quick request lacks its idempotency input ID');
          state = { identity, baselineExperimentIds, getTask, quick: { inputId, interactionId } };
          await saveState(state, true);
        } catch (error) {
          writeFailure = error;
          return route.abort();
        }
        return route.continue();
      });
      try {
        const sent = await send(page, quickPrompt);
        state.quick = sent;
      }
      catch (error) { throw writeFailure ?? error; }
      finally { await page.unroute(routePattern); }
      await saveState(state);
    }

    if (!state.quick.runId) {
      state.quick.runId = await waitFor('saved quick input acknowledgement', async () => {
        const messages = await get(`${H}/interactions/${encodeURIComponent(state.quick.interactionId)}/messages?limit=100`);
        for (const item of messages.runs) {
          const run = await get(`${H}/runs/${encodeURIComponent(item.runId)}`);
          if (run.inputs.some(input => input.inputId === state.quick.inputId)) return run.runId;
        }
        return null;
      });
      await saveState(state);
    }

    const queued = await toolResult(get, state.quick, 'RunExperiment', { task: 'invsqrt', mode: 'quick' });
    const experimentId = queued.result.experiment_id;
    assert.equal(queued.result.status, 'queued');
    assert.match(experimentId, /^[a-zA-Z0-9_-]+$/);
    assert.ok(!state.baselineExperimentIds.includes(experimentId), 'Outcome references a pre-existing experiment');
    const experimentPath = `${Q}/experiments/${experimentId}`;
    const experiment = await waitFor(`experiment ${experimentId} to succeed`, async () => {
      const snapshot = await get(experimentPath);
      assert.equal(snapshot.id, experimentId);
      assert.equal(snapshot.sender_id, options.agent);
      assert.equal(snapshot.task, 'invsqrt');
      assert.equal(snapshot.mode, 'quick');
      if (snapshot.status === 'succeeded') return snapshot;
      assert.ok(['queued', 'running'].includes(snapshot.status), `Experiment ended ${snapshot.status}`);
      return null;
    });
    const { items } = await get(`${Q}/experiments`);
    const fresh = items.filter(item => !state.baselineExperimentIds.includes(item.id)
      && item.sender_id === options.agent && item.task === 'invsqrt' && item.mode === 'quick');
    assert.deepEqual(fresh.map(item => item.id), [experimentId], 'Expected a single new invsqrt quick experiment');

    const result = experiment.result;
    assert.equal(result.status, 'PASS');
    assert.equal(result.full, false);
    assert.equal(result.task_cost, null);
    assert.equal(result.verified_shots, 262144);
    assert.ok(Number.isFinite(result.diagnostic_cost) && result.diagnostic_cost > 0);

    const sendStatusQuery = !state.statusQuery;
    if (sendStatusQuery) {
      const prompt = `请真实调用 Quantum World 的 GetExperiment 工具恰好一次，参数 experiment_id="${experimentId}"。请根据实际回执报告该实验状态。不要调用 RunExperiment 或其他 World 工具。`;
      state.statusQuery = await send(page, prompt);
      await saveState(state);
    }
    const { result: queryReport, ...queryReceipt } = await toolResult(
      get, state.statusQuery, 'GetExperiment', { experiment_id: experimentId },
    );
    assert.equal(queryReport.id, experimentId);
    assert.equal(queryReport.status, 'succeeded');
    assert.equal(queryReport.task, 'invsqrt');
    assert.equal(queryReport.mode, 'quick');
    assert.equal(queryReport.result?.status, result.status);
    assert.equal(queryReport.result?.verified_shots, result.verified_shots);
    assert.equal(queryReport.result?.diagnostic_cost, result.diagnostic_cost);
    if (sendStatusQuery) await page.locator('#chat-messages .message.agent .message-text').first().waitFor();

    await page.locator('#refresh').click();
    await page.locator(`#records [data-select="${experimentId}"]`).click();
    await page.locator('#detail').getByText(experimentId, { exact: true }).waitFor();
    await page.locator('#log-name').selectOption('verify.stdout');
    const displayedLog = await waitFor('verify.stdout JSON in the browser', async () => {
      try { return JSON.parse(await page.locator('#log-text').textContent()); }
      catch (error) { if (error instanceof SyntaxError) return null; throw error; }
    });
    const log = await get(`${experimentPath}/logs?name=verify.stdout&offset=0&limit=131072`);
    assert.ok(log.eof && log.experiment_id === experimentId && log.name === 'verify.stdout');
    assert.deepEqual(displayedLog, JSON.parse(log.text), 'Displayed log differs from API log');
    assert.equal(displayedLog.status, result.status);
    assert.equal(displayedLog.verified_shots, result.verified_shots);
    assert.equal(displayedLog.score, result.diagnostic_cost);

    const evidence = { status: 'passed', getTask: state.getTask, quick: queued, experimentId,
      statusQuery: { ...queryReceipt, experimentId: queryReport.id, status: queryReport.status },
      verification: { status: result.status, verified_shots: result.verified_shots,
        diagnostic_cost: result.diagnostic_cost, full: result.full, task_cost: result.task_cost } };
    await saveState({ ...state, evidence });
    console.log(JSON.stringify({ ...evidence, stateFile }, null, 2));
  } finally {
    await browser.close();
  }
}

try { await main(); }
catch (error) {
  console.error(`FAIL: ${error.message}`);
  process.exitCode = 1;
}
