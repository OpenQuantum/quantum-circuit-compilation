import assert from 'node:assert/strict';
import { access, mkdir, readFile, writeFile } from 'node:fs/promises';
import { createRequire } from 'node:module';
import { dirname, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { parseArgs } from 'node:util';

export const PROMPT = '请用知识图谱优化 invsqrt';
const runPath = /^\/api\/human\/v1\/interactions\/([^/]+)\/runs$/;
const runRoute = /\/api\/human\/v1\/(?:interactions\/[^/]+\/runs|runs\/[^/]+\/inputs)$/;
const idPattern = /^[A-Za-z0-9_-]+$/;

export function exactIntent(url, body) {
  const match = runPath.exec(new URL(url).pathname);
  if (!match || typeof body?.inputId !== 'string' || !idPattern.test(body.inputId)
    || !Array.isArray(body.parts) || body.parts.length !== 1) return null;
  const part = body.parts[0];
  if (!idPattern.test(match[1]) || part?.type !== 'text' || part.text !== PROMPT) return null;
  return { interactionId: decodeURIComponent(match[1]), inputId: body.inputId };
}

function intentForRequest(request) {
  if (request.method() !== 'POST') return null;
  return exactIntent(request.url(), request.postDataJSON());
}

async function assertUnused(path) {
  try {
    await access(path);
  } catch (error) {
    if (error.code === 'ENOENT') return;
    throw error;
  }
  throw new Error('A send intent already exists. Inspect the saved interaction without resending.');
}

async function sendOne(page, origin, path, identity) {
  let intent;
  let interceptedError;
  const intercept = async route => {
    const request = route.request();
    if (request.method() !== 'POST') return route.continue();
    try {
      const parsed = intentForRequest(request);
      assert.ok(parsed && new URL(request.url()).origin === origin, 'Unexpected conversation input');
      assert.ok(!intent, 'Browser attempted to resend the input');
      intent = { ...parsed, prompt: PROMPT, ...identity, submittedAt: new Date().toISOString() };
      await writeFile(path, JSON.stringify(intent, null, 2) + '\n', { flag: 'wx', mode: 0o600 });
      return route.continue();
    } catch (error) {
      interceptedError = error;
      return route.abort();
    }
  };

  await page.route(runRoute, intercept);
  try {
    await page.locator('#chat-input').fill(PROMPT);
    const [response] = await Promise.all([
      page.waitForResponse(r => !!intentForRequest(r.request()), { timeout: 25000 }),
      page.locator('#send-chat').click(),
    ]);
    assert.equal(response.status(), 201, 'BFF did not acknowledge the user input');
    const { runId } = await response.json();
    assert.ok(intent && idPattern.test(runId ?? ''), 'Missing acknowledgement or durable input');
    const receipt = { ...intent, runId, acknowledgedAt: new Date().toISOString() };
    await writeFile(path + '.ack.json', JSON.stringify(receipt, null, 2) + '\n', { flag: 'wx', mode: 0o600 });
    return receipt;
  } catch (error) {
    throw interceptedError ?? error;
  } finally {
    await page.unroute(runRoute, intercept);
  }
}

async function main() {
  const { values: options } = parseArgs({ options: {
    'base-url': { type: 'string', default: 'http://127.0.0.1:8330/' },
    'login-code-file': { type: 'string' },
    'baseline-full-id': { type: 'string' },
    'baseline-quick-id': { type: 'string' },
    'intent-file': { type: 'string' },
    playwright: { type: 'string' },
    send: { type: 'boolean', default: false },
  } });
  const base = new URL(options['base-url']);
  assert.ok(['127.0.0.1', 'localhost', '[::1]'].includes(base.hostname) && base.href === `${base.origin}/`);
  const baselineFullId = options['baseline-full-id'];
  const baselineQuickId = options['baseline-quick-id'];
  assert.match(baselineFullId ?? '', /^exp-[0-9a-f]+$/);
  assert.match(baselineQuickId ?? '', /^exp-[0-9a-f]+$/);
  assert.ok(options['login-code-file'] && options.playwright, 'Pass --login-code-file and --playwright');
  const file = options['intent-file'] && resolve(options['intent-file']);
  if (options.send) {
    assert.ok(file && file.split(/[/\\]/).includes('.local-console'), 'Pass --intent-file in the local state directory');
    await assertUnused(file);
    await assertUnused(file + '.ack.json');
    await mkdir(dirname(file), { recursive: true });
  }

  const identity = {
    userId: 'local-user', agentId: 'quantum-agent-local', worldId: 'quantum-world-invsqrt-local',
    baselineFullId, baselineQuickId,
  };
  const readiness = await fetch(base.origin + '/health/ready', { signal: AbortSignal.timeout(5000) });
  assert.ok(readiness.ok, 'Console is not ready');
  const { chromium } = createRequire(import.meta.url)(options.playwright);
  const browser = await chromium.launch({ channel: 'chrome', headless: true });
  try {
    const context = await browser.newContext();
    const page = await context.newPage();
    await page.goto(base.origin);
    try {
      const loginCode = (await readFile(options['login-code-file'], 'utf8')).trim();
      assert.ok(loginCode);
      await page.locator('#login-user').fill(identity.userId);
      await page.locator('#access-code').fill(loginCode);
      await page.locator('#login-form button[type=submit]').click();
      await page.locator('#workspace').waitFor({ state: 'visible' });
    } catch {
      throw new Error('Console login failed. Check the login-code file and local-user account.');
    }

    const api = async path => {
      const response = await context.request.get(base.origin + '/api/quantum/v1/' + path, { timeout: 15000 });
      assert.ok(response.ok(), `Console ${path} returned HTTP ${response.status()}`);
      return response.json();
    };
    const [summary, full, quick, optimizations] = await Promise.all([
      api('summary'), api(`experiments/${baselineFullId}`), api(`experiments/${baselineQuickId}`), api('optimizations'),
    ]);
    assert.equal(summary.transport_mode, 'sp');
    assert.equal(summary.world_id, identity.worldId);
    assert.equal(summary.user_id, identity.userId);
    assert.ok(Array.isArray(optimizations.items), 'Expected optimization status API');
    for (const [row, mode] of [[full, 'full'], [quick, 'quick']]) {
      assert.equal(row.task, 'invsqrt');
      assert.equal(row.sender_id, identity.agentId);
      assert.equal(row.mode, mode);
      assert.equal(row.status, 'succeeded');
      assert.equal(row.result?.status, 'PASS');
    }
    assert.ok(full.candidate_id && full.candidate_id === quick.candidate_id, 'The baselines must use one candidate');
    const fingerprint = full.provenance?.built_artifacts?.['invsqrt.kmb'];
    assert.ok(fingerprint && fingerprint === quick.provenance?.built_artifacts?.['invsqrt.kmb'], 'Baseline circuit differs');
    if (!options.send) {
      console.log(JSON.stringify({ status: 'ready', ...identity }));
      return;
    }

    await page.waitForFunction(agentId => Array.from(document.querySelector('#agent-select').options)
      .some(option => option.value === agentId), identity.agentId);
    await page.locator('#new-chat').click();
    await page.locator('#agent-select').selectOption(identity.agentId);
    const receipt = await sendOne(page, base.origin, file, identity);
    console.log(JSON.stringify({ status: 'acknowledged', ...receipt, ackFile: file + '.ack.json' }));
  } finally {
    await browser.close();
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  main().catch(error => { console.error(error.message); process.exitCode = 1; });
}
