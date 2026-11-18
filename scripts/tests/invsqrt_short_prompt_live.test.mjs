import assert from 'node:assert/strict';
import test from 'node:test';
import { PROMPT, exactIntent } from './invsqrt_short_prompt_live.mjs';

test('only the exact new conversation text and one valid input ID pass', () => {
  const url = 'http://127.0.0.1:8330/api/human/v1/interactions/convo-42/runs';
  const post = { inputId: 'console_a1', parts: [{ type: 'text', text: '请用知识图谱优化 invsqrt' }] };
  assert.equal(PROMPT, post.parts[0].text);
  assert.deepEqual(exactIntent(url, post), { interactionId: 'convo-42', inputId: 'console_a1' });
  assert.equal(exactIntent(url, { ...post, parts: [...post.parts, post.parts[0]] }), null);
  assert.equal(exactIntent(url, { ...post, parts: [{ ...post.parts[0], text: PROMPT + '。' }] }), null);
  assert.equal(exactIntent(url, { ...post, inputId: 'wrong shape' }), null);
  assert.equal(exactIntent('http://127.0.0.1:8330/api/human/v1/runs/run-42/inputs', post), null);
});
