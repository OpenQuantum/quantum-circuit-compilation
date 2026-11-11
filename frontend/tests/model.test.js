import test from 'node:test';
import assert from 'node:assert/strict';
import {approvalAllowed, submissionAllowed, artifactURL, orderedMessages, errorMessage, escapeHTML, score, captureChatContext, chatContextMatches} from '../model.js';

const sha = 'a'.repeat(64);
test('approval needs the human role, awaiting package and exact hash', () => {
  const pkg = {status: 'awaiting_approval', sha256: sha};
  assert.equal(approvalAllowed({can_approve: true}, pkg), true);
  assert.equal(approvalAllowed({can_approve: false}, pkg), false);
  assert.equal(approvalAllowed({can_approve: true}, {...pkg, status: 'submitted'}), false);
  assert.equal(approvalAllowed({can_approve: true}, {...pkg, sha256: 'not-a-hash'}), false);
});
test('submission never follows a pending, expired or mismatched approval', () => {
  const now = Date.parse('2026-11-10T12:00:00Z');
  const pkg = {status: 'approved', sha256: sha, approval: {sha256: sha, expires_at: '2026-11-10T13:00:00Z'}};
  assert.equal(submissionAllowed({can_approve: true}, pkg, now), true);
  for (const status of ['awaiting_approval', 'submitting', 'submitted', 'unknown']) assert.equal(submissionAllowed({can_approve: true}, {...pkg, status}, now), false);
  assert.equal(submissionAllowed({can_approve: true}, {...pkg, approval: {...pkg.approval, sha256: 'b'.repeat(64)}}, now), false);
  assert.equal(submissionAllowed({can_approve: true}, {...pkg, approval: {...pkg.approval, expires_at: '2026-11-10T11:59:59Z'}}, now), false);
  assert.equal(submissionAllowed({can_approve: true}, {...pkg, approval: {}}, now), false);
});
test('artifact links use the authenticated local route rather than upstream URLs', () => {
  assert.equal(artifactURL({id: 'opaque/file?x=1', download_url: 'https://evil.example/file'}), '/api/quantum/v1/artifacts/opaque%2Ffile%3Fx%3D1');
  assert.equal(artifactURL({download_url: 'javascript:alert(1)'}), null);
});
test('tool text is HTML escaped including attributes', () => {
  assert.equal(escapeHTML('<img src=x onerror="alert(1)">'), '&lt;img src=x onerror=&quot;alert(1)&quot;&gt;');
});
test('history pagination preserves timeline order and latest overlapping message', () => {
  const one = {id: 'x', role: 'user', runId: 'run1', timelineSequence: 1, parts: [{type: 'text', text: 'question'}]};
  const stream = {id: 'y', role: 'agent', runId: 'run1', timelineSequence: 2, parts: [{type: 'text', text: 'a'}]};
  const done = {...stream, parts: [{type: 'text', text: 'answer'}]};
  assert.deepEqual(orderedMessages([{items: [stream]}, {items: [one, done]}]), [one, done]);
});
test('real BFF and World error formats yield actionable messages', () => {
  assert.equal(errorMessage({type: 'error', data: {code: 'AUTH_REQUIRED', message: '请登录'}}), '请登录');
  assert.equal(errorMessage({error: {message: '文件已变化'}}), '文件已变化');
  assert.equal(errorMessage({detail: [{loc: 'schema'}]}, '格式无效'), '格式无效');
});
test('missing scores are not rendered as a zero or a leaderboard result', () => {
  assert.equal(score(null), '—'); assert.equal(score(undefined), '—'); assert.equal(score(NaN), '—'); assert.equal(score(0), '0');
});
test('late run response cannot attach an old run to a newly selected conversation', async () => {
  const state = {generation: 1, chatVersion: 5, interaction: {interactionSessionId: 'A'}, run: null};
  const context = captureChatContext(state);
  let release;
  const response = new Promise(resolve => { release = resolve; });
  const task = response.then(run => { if (chatContextMatches(state, context)) state.run = run; });
  state.chatVersion++;
  state.interaction = {interactionSessionId: 'B'};
  release({runId: 'run_A'});
  await task;
  assert.equal(state.run, null);
  assert.equal(state.interaction.interactionSessionId, 'B');
  // Selecting A again does not revive an earlier view's outstanding request.
  state.interaction = {interactionSessionId: 'A'};
  assert.equal(chatContextMatches(state, context), false);
});
test('late conversation creation cannot replace a fresh draft or new account', async () => {
  const state = {generation: 2, chatVersion: 1, interaction: null};
  const draft = captureChatContext(state);
  let release;
  const response = new Promise(resolve => { release = resolve; });
  const task = response.then(interaction => { if (chatContextMatches(state, draft)) state.interaction = interaction; });
  state.chatVersion++;
  release({interactionSessionId: 'orphaned-old-draft'});
  await task;
  assert.equal(state.interaction, null);
  const fresh = captureChatContext(state);
  assert.equal(chatContextMatches(state, fresh), true);
  state.generation++;
  assert.equal(chatContextMatches(state, fresh), false);
});
