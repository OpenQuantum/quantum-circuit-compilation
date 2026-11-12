// Real browser -> model -> research tools. Read-only, no experiments or publishing.
import assert from 'node:assert/strict';
import {readFile, writeFile} from 'node:fs/promises';
import {createRequire} from 'node:module';
import {resolve} from 'node:path';
import {setTimeout as delay} from 'node:timers/promises';

const root=process.cwd(), dir=resolve(root,'.local-console/live');
const {chromium}=createRequire(import.meta.url)(resolve(dir,'test-tools/node_modules/playwright'));
const stateFile=resolve(dir,'research-browser-evidence.json');
const browser=await chromium.launch({channel:'chrome',headless:true});
try {
  const context=await browser.newContext();
  const page=await context.newPage();
  // Hold the directory response to exercise New Chat while initial history loads.
  let releaseDirectory;
  const directoryGate=new Promise(resolve=>{releaseDirectory=resolve;});
  await page.route('**/api/human/v1/agents',async route=>{await directoryGate;await route.continue();});
  await page.goto('http://127.0.0.1:8330/');
  await page.locator('#login-user').fill('local-user');
  await page.locator('#access-code').fill((await readFile(resolve(dir,'login-code'),'utf8')).trim());
  await page.locator('#login-form button[type=submit]').click();
  await page.locator('#workspace').waitFor({state:'visible'});
  await page.locator('#new-chat').click();
  releaseDirectory();
  await page.waitForFunction(()=>document.querySelector('#agent-select option[value="quantum-agent-local"]'));
  assert.equal(await page.locator('#agent-select').isEnabled(),true,'Late directory response must not reopen an old conversation');
  let state;
  try {state=JSON.parse(await readFile(stateFile,'utf8'));}
  catch(error) {if(error.code!=='ENOENT')throw error;}
  if (!state) {
    await page.locator('#new-chat').click();
    await page.locator('#agent-select').selectOption('quantum-agent-local');
    await page.locator('#chat-input').fill('请真实调用 SearchResearchMethods 工具，task="invsqrt"、limit=2、query=""。然后从返回的公开方法里选择一个节点，真实调用 GetResearchNode，task="invsqrt"，node_id 使用该节点的实际 ID。依据回执给出方法标题、来源和简要原理。恰好调用这两个 World 工具各一次，不运行实验、不调用其他 World 工具、不发布、不提交 Kaggle。');
    await page.route('**/api/human/v1/interactions/*/runs',async route=>{
      const request=route.request();
      if(request.method()==='POST') {
        state={inputId:request.postDataJSON().inputId, interactionId:new URL(request.url()).pathname.split('/')[5]};
        await writeFile(stateFile,JSON.stringify(state,null,2),{mode:0o600,flag:'wx'});
      }
      await route.continue();
    });
    const [response]=await Promise.all([
      page.waitForResponse(r=>r.request().method()==='POST' && /\/interactions\/[^/]+\/runs$/.test(new URL(r.url()).pathname)),
      page.locator('#send-chat').click()
    ]);
    assert.equal(response.status(),201);
    state.runId=(await response.json()).runId;
    await writeFile(stateFile,JSON.stringify(state,null,2),{mode:0o600});
    console.error('Browser research run:',state.runId);
  }
  assert.ok(state.runId,'Uncertain request; reconcile saved inputId instead of resending');
  const deadline=Date.now()+180000;
  let snapshot;
  while(Date.now()<deadline) {
    const response=await context.request.get('http://127.0.0.1:8330/api/human/v1/runs/'+encodeURIComponent(state.runId));
    assert.equal(response.status(),200);
    snapshot=await response.json();
    if(snapshot.status==='succeeded')break;
    assert.ok(!['failed','cancelled','rejected','interrupted'].includes(snapshot.status),JSON.stringify(snapshot.error));
    await delay(1000);
  }
  assert.equal(snapshot.status,'succeeded');
  state.interactionId = snapshot.interactionSessionId;
  const calls=snapshot.tools.filter(t=>t.toolScope==='world');
  assert.deepEqual(calls.map(t=>t.toolName),['SearchResearchMethods','GetResearchNode']);
  for(const call of calls) {
    assert.equal(call.status,'succeeded');
    assert.equal(call.publicResult.status,'completed');
  }
  const search=JSON.parse(calls[0].publicResult.data.text);
  const detail=JSON.parse(calls[1].publicResult.data.text);
  assert.equal(search.scope,'public_and_official_only');
  assert.equal(detail.read_only,true);
  assert.ok(search.nodes.some(n=>n.id===detail.nodes[0].id));
  // Some public version nodes contain only a summary. Preserve that absence;
  // do not fabricate an explanation or mistake it for a transport failure.
  assert.ok(detail.nodes[0].summary.length>0 || detail.nodes[0].explanation.length>0);
  assert.ok(['public','official'].includes(detail.nodes[0].source.type));
  await page.locator('#conversation-select').selectOption(state.interactionId);
  await page.locator('#chat-messages .message.agent .message-text').first().waitFor();
  await page.screenshot({path:resolve(dir,'research-connected.png'),fullPage:true});
  state.evidence={status:'passed',tools:calls.map(t=>({name:t.toolName,actionId:t.publicResult.actionId,outcomeId:t.publicResult.outcomeId})),
    node:detail.nodes[0],scope:detail.scope,read_only:true};
  await writeFile(stateFile,JSON.stringify(state,null,2),{mode:0o600});
  console.log(JSON.stringify({status:'passed',runId:state.runId,tools:state.evidence.tools,node_id:detail.nodes[0].id,title:detail.nodes[0].title,source:detail.nodes[0].source,read_only:true},null,2));
} finally {await browser.close();}
