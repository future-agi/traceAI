const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const os = require('node:os');
const http = require('node:http');
const {spawn} = require('node:child_process');
const core = require('@traceai/agent-core');
const {applyEvent, emitTurn, neutral, isCli, stopOutcome, text} = require('../dist/mapper.js');
const {handleEvent, seal, unseal} = require('../dist/runtime.js');
const {antigravityAdapter: adapter, hooks, PLUGIN_NAME, supportedHostVersion, pinnedNode} = require('../dist/adapter.js');
const p = {conversationId:'test-conversation', initialNumSteps:1, invocationNum:0,
  transcriptPath:'/home/test/.gemini/antigravity-cli/brain/test-conversation/transcript_full.jsonl'};
const state = () => ({schemaVersion:1, generation:0, touched:Date.now(), completed:[]});
const config = {captureContent:true, hideInputs:false, hideOutputs:false, hideErrorDetails:false,
  endpoint:'http://127.0.0.1:9999/tracer/v1/traces', projectName:'test', generation:0,
  identityKey:'a'.repeat(64), credentials:{apiKey:'synthetic-api',secretKey:'synthetic-secret'}};
function run(exe,args,env,input,cwd) {
  return new Promise((resolve,reject) => {
    const child=spawn(exe,args,{env,cwd,stdio:['pipe','pipe','pipe']});
    let stdout='',stderr='';
    child.stdout.on('data',x=>stdout+=x); child.stderr.on('data',x=>stderr+=x);
    child.on('error',reject); child.on('close',code=>resolve({code,stdout,stderr}));
    child.stdin.on('error',()=>{}); child.stdin.end(input);
  });
}
async function fixture(t) {
  const home = await fs.mkdtemp(path.join(os.tmpdir(),'fi-agy-'));
  const project=path.join(home,'project'); await fs.mkdir(project,{mode:0o700});
  const requests=[];
  const server=http.createServer((req,res)=>{
    const chunks=[]; req.on('data',b=>chunks.push(b)); req.on('end',()=>{
      requests.push({headers:req.headers,body:JSON.parse(Buffer.concat(chunks).toString())});
      res.setHeader('Content-Type','application/json');res.end('{}');
    });
  });
  await new Promise(r=>server.listen(0,'127.0.0.1',r));
  t.after(async()=>{server.closeAllConnections();await new Promise(r=>server.close(r));await fs.rm(home,{recursive:true,force:true});});
  const options={agent:'antigravity',home,env:{}};
  const result=await core.setup({...options,credentials:config.credentials,projectName:'test',
    otlpEndpoint:`http://127.0.0.1:${server.address().port}/tracer/v1/traces`,configureOnly:true});
  assert.equal(result.configured,true);
  const resolved=await core.resolveConfig(options);
  const env={...process.env,HOME:home,XDG_CONFIG_HOME:path.join(home,'.config'),XDG_STATE_HOME:path.join(home,'.local/state')};
  delete env.FI_API_KEY; delete env.FI_SECRET_KEY; delete env.FI_OTLP_ENDPOINT; delete env.FI_AGENT_CONFIG_DIR;
  const payload=extra=>({...p,workspacePaths:[project],...extra});
  const send=(event,extra={})=>handleEvent(event,payload({...extra,workspacePaths:[project]}),options);
  const spans=()=>requests.flatMap(r=>(r.body.resourceSpans??[]).flatMap(rs=>rs.scopeSpans.flatMap(s=>s.spans)));
  return {home,project,options,resolved,env,payload,send,spans,requests};
}
const attrs=span=>Object.fromEntries(span.attributes.map(a=>[a.key,Object.values(a.value)[0]]));

test('native named-hook-map; only observer events, no permission decisions',()=>{
  const map=hooks('/opt/node/bin/node',"/tmp/it' s/config")[PLUGIN_NAME];
  assert.deepEqual(Object.keys(map),['SessionStart','PreInvocation','PostInvocation','PostToolUse','Stop']);
  assert.equal(map.PostToolUse[0].matcher,'.*');
  assert.equal(map.Stop[0].timeout,26);
  assert.equal(map.PreInvocation[0].timeout,10);
  assert.ok(map.Stop[0].command.includes("'/opt/node/bin/node' ./hook.cjs Stop"));
  assert.equal(neutral('Stop'),'{"decision":""}\n'); assert.equal(neutral('PostToolUse'),'{}\n');
});
test('isCli remains CLI-only; app is a distinct supported surface',()=>{
  assert.equal(isCli(p),true);
  assert.equal(isCli({...p,transcriptPath:'/home/me/.gemini/antigravity/brain/a/t.jsonl'}),false);
  assert.equal(isCli({...p,transcriptPath:'/home/me/.gemini/antigravity-ide/brain/a/t.jsonl'}),false);
  assert.equal(isCli({conversationId:'a'}),false);
});
test('idle false is not terminal; invocation spans are AGENT, not fabricated LLM requests',()=>{
  const s=state();
  applyEvent(s,'PreInvocation',{...p,lastUserInput:'prompt'},config,1);
  applyEvent(s,'PostInvocation',p,config,3);
  assert.deepEqual(applyEvent(s,'Stop',{...p,fullyIdle:false,terminationReason:'NO_TOOL_CALL'},config,4),[]);
  assert.ok(s.active);
  const [finished]=applyEvent(s,'Stop',{...p,fullyIdle:true,terminationReason:'NO_TOOL_CALL'},config,5);
  assert.equal(finished.input,'prompt');assert.equal(finished.activities[0].kind,'agent');
  assert.equal(finished.end,5);assert.equal(finished.outcome,'success');
  assert.deepEqual(applyEvent(s,'Stop',{...p,fullyIdle:true},config,6),[]);
  assert.deepEqual(applyEvent(s,'PreInvocation',p,config,7),[]); assert.equal(s.active,undefined);
});
test('cancel and missing stop are not successful; only a new native user record opens a turn',()=>{
  const s=state();s.latestUserStep=0;applyEvent(s,'PreInvocation',p,config,1);
  assert.deepEqual(applyEvent(s,'PreInvocation',{...p,initialNumSteps:4},config,5),[]);
  assert.equal(s.active.id,'turn-1');
  s.latestUserStep=7;
  const [gap]=applyEvent(s,'PreInvocation',{...p,initialNumSteps:8},config,10);
  assert.equal(gap.outcome,'incomplete');assert.equal(s.active.id,'turn-8');
  assert.equal(stopOutcome({terminationReason:'USER_CANCELED'}),'interrupted');
  assert.equal(stopOutcome({terminationReason:'ERROR'}),'error');
  assert.equal(stopOutcome({terminationReason:'max_steps_exceeded'}),'incomplete');
});
test('content policy applies before encrypted persistence; changed destination/policy cannot decrypt',()=>{
  const s=state();applyEvent(s,'PreInvocation',{...p,lastUserInput:'synthetic-secret hello'},config);
  const blob=seal(s,config);assert.ok(!blob.includes('hello'));assert.ok(!blob.includes('synthetic-secret'));
  assert.equal(unseal(blob,config).active.input,'[REDACTED] hello');
  assert.throws(()=>unseal(blob,{...config,projectName:'other'}));
  assert.throws(()=>unseal(blob,{...config,captureContent:false}));
  const hidden=state();applyEvent(hidden,'PreInvocation',{...p,lastUserInput:'secret text'},{...config,captureContent:false});
  assert.equal(hidden.active.input,undefined);
});
test('real recorded CLI fixture replay produces three roots, shared session for resume, six tools and zero LLM spans',async t=>{
  const f=await fixture(t);
  for (const filename of ['real-cli-1.4.2.jsonl','real-cli-1.4.2-tool-error.jsonl']) {
    const lines=(await fs.readFile(path.join(__dirname,'fixtures',filename),'utf8')).trim().split('\n');
    for (const line of lines) {const row=JSON.parse(line);await f.send(row.event,row.payload);}
  }
  const spans=f.spans(), roots=spans.filter(s=>!s.parentSpanId);
  assert.equal(roots.length,3);assert.equal(spans.length,16);
  assert.equal(spans.filter(s=>attrs(s)['fi.span.kind']==='LLM').length,0);
  assert.equal(spans.filter(s=>attrs(s)['fi.span.kind']==='TOOL').length,6);
  assert.equal(spans.filter(s=>attrs(s)['fi.span.kind']==='TOOL'&&s.status.code===2).length,2);
  assert.equal(attrs(roots[0])['session.id'],attrs(roots[1])['session.id']);
  assert.notEqual(attrs(roots[0])['session.id'],attrs(roots[2])['session.id']);
  assert.notEqual(roots[0].traceId,roots[1].traceId);
  assert.ok(spans.every(s=>!s.parentSpanId||spans.some(p=>p.traceId===s.traceId&&p.spanId===s.parentSpanId)));
  assert.ok(spans.every(s=>!Object.keys(attrs(s)).some(k=>k.includes('token_count'))));
  for(const request of f.requests) {
    assert.equal(request.headers['x-project-name'],'test');assert.equal(request.headers['x-api-key'],'synthetic-api');
    for(const rs of request.body.resourceSpans??[]) {
      const resource=Object.fromEntries(rs.resource.attributes.map(a=>[a.key,Object.values(a.value)[0]]));
      assert.equal(resource.project_type,'observe');assert.equal(resource.project_name,'test');
    }
  }
  const status=await core.inspectStatus(f.options);assert.equal(status.delivery.exported,16);
  assert.equal(status.productReadback,'unverified');
  console.log('RECORDED_FIXTURE_OTLP',JSON.stringify({roots:3,tools:6,invocationActivities:7,llm:0,errors:2}));
});
test('CLI and app checkpoints and trace identities do not collide for the same conversation ID', async t => {
  const f = await fixture(t);
  for (const surface of ['cli', 'app']) {
    const transcriptPath = p.transcriptPath.replace('antigravity-cli', surface === 'app' ? 'antigravity' : 'antigravity-cli');
    await handleEvent('PreInvocation', {...p, transcriptPath, workspacePaths: [], lastUserInput: 'same prompt'}, f.options);
    await handleEvent('PostInvocation', {...p, transcriptPath, workspacePaths: []}, f.options);
    await handleEvent('Stop', {...p, transcriptPath, workspacePaths: [], fullyIdle: true, terminationReason: 'NO_TOOL_CALL'}, f.options);
  }
  const roots = f.spans().filter(s => !s.parentSpanId);
  assert.equal(roots.length, 2);
  assert.deepEqual(roots.map(s => attrs(s)['fi.agent.surface']), ['cli', 'app']);
  assert.notEqual(roots[0].traceId, roots[1].traceId);
  assert.notEqual(attrs(roots[0])['session.id'], attrs(roots[1])['session.id']);
  assert.equal((await fs.readdir(path.join(f.resolved.paths.stateDir, 'antigravity-conversations'))).filter(n => n.endsWith('.json')).length, 2);
});
test('redact quoted credentials in mixed text and whole known keys before truncation',()=>{
  for(const value of ['prefix {"password":"private pass phrase"} suffix', "password='private pass phrase'", 'prefix '+JSON.stringify(JSON.stringify({api_key:'private pass phrase'}))])
    assert.ok(!text(value,config).includes('private pass phrase'));
  const nearCap='x'.repeat(19995)+'synthetic-secret';
  assert.ok(!text(nearCap,config).includes('synth'));
});
test('model cost uses the backend semantic convention',async t=>{
  const f=await fixture(t),tracer=core.createTurnTracer({config:f.resolved});
  const root=tracer.startTurn({sessionId:'cost-test',turnId:'1',usagePlan:'request'});
  root.child({id:'response-1',kind:'generation',fidelity:'reconstructed-message'}).end({usage:{input:2,output:1,cacheInInput:true,reasoningInOutput:true,costUsd:0.02}});
  root.end();await tracer.flush();await tracer.shutdown();
  const model=f.spans().find(s=>attrs(s)['fi.span.kind']==='LLM');
  assert.equal(attrs(model)['gen_ai.cost.total'],0.02);
  assert.equal(attrs(model)['gen_ai.usage.cost'],undefined);
});
test('credential fields are absent from decrypted checkpoints and exported OTLP',async t=>{
  const f=await fixture(t);
  const secrets=['private-pass-value','private-api-value','private-token-value','private-secret-value','private-escaped-value'];
  const args={password:secrets[0],nested:[{api_key:secrets[1],Token:secrets[2]}],secret:secrets[3],safe:'kept'};
  await f.send('PreInvocation',{lastUserInput:'synthetic-secret '+JSON.stringify(args)});
  await f.send('PostToolUse',{stepIdx:2,toolCall:{name:'example',args},result:{text:JSON.stringify(args),escaped:JSON.stringify(JSON.stringify({password:secrets[4]}))}});
  const files=await fs.readdir(path.join(f.resolved.paths.stateDir,'antigravity-conversations'));
  const raw=await fs.readFile(path.join(f.resolved.paths.stateDir,'antigravity-conversations',files.find(n=>n.endsWith('.json'))),'utf8');
  const persisted=JSON.stringify(unseal(raw,f.resolved));
  for(const secret of [...secrets,'synthetic-secret']) assert.ok(!persisted.includes(secret),'checkpoint contains a synthetic credential');
  assert.ok(persisted.includes('kept'));
  await f.send('PostInvocation');
  await f.send('Stop',{fullyIdle:true,terminationReason:'NO_TOOL_CALL',finalModelOutput:JSON.stringify(args)});
  assert.equal(f.spans().length,3);
  for(const secret of [...secrets,'synthetic-secret']) assert.ok(!JSON.stringify(f.spans()).includes(secret),'OTLP contains a synthetic credential');
});

test('idle gate, duplicate stop, prompt/output privacy, disable fence across hook processes',async t=>{
  const f=await fixture(t);
  await f.send('PreInvocation',{lastUserInput:'safe prompt synthetic-secret'});
  await f.send('PostToolUse',{stepIdx:2,toolCall:{name:'view_file',args:{path:'example.txt'}},result:'tool result'});
  await f.send('PostInvocation',{});
  await f.send('Stop',{fullyIdle:false,terminationReason:'NO_TOOL_CALL'});assert.equal(f.spans().length,0);
  await f.send('Stop',{fullyIdle:true,terminationReason:'NO_TOOL_CALL',finalModelOutput:'response'});
  assert.equal(f.spans().length,3);assert.ok(!JSON.stringify(f.spans()).includes('synthetic-secret'));
  await f.send('Stop',{fullyIdle:true});assert.equal(f.spans().length,3);
  await f.send('PreInvocation',{initialNumSteps:8});await core.disable(f.options);
  await f.send('PostInvocation',{initialNumSteps:8});await f.send('Stop',{fullyIdle:true});
  assert.equal(f.spans().length,3);
  const files=await fs.readdir(path.join(f.resolved.paths.stateDir,'antigravity-conversations'));
  for(const name of files) assert.equal((await fs.stat(path.join(f.resolved.paths.stateDir,'antigravity-conversations',name))).mode&0o777,0o600);
});
for (const [terminationReason,outcome,status] of [['ERROR','error',2],['USER_CANCELED','interrupted',0],['NO_TOOL_CALL','incomplete',0]]) {
  test(`missing PostInvocation preserves ${outcome} and records a separate gap`,async t=>{
    const f=await fixture(t);
    await f.send('PreInvocation',{lastUserInput:'synthetic input'});
    await f.send('Stop',{fullyIdle:true,terminationReason});
    const root=f.spans().find(s=>!s.parentSpanId);
    assert.equal(root.status.code,status);
    assert.equal(attrs(root)['fi.agent.outcome'],outcome);
    assert.equal(attrs(root)['fi.agent.gap'],'MISSING_CHILD_STOP');
  });
}
test('bounded transcript skip is persisted across hooks and visible in delivery diagnostics',async t=>{
  const f=await fixture(t);
  const payload=f.payload({});
  const transcriptPath=path.join(f.home,'.gemini/antigravity-cli/brain',payload.conversationId,'.system_generated/logs/transcript_full.jsonl');
  await fs.mkdir(path.dirname(transcriptPath),{recursive:true,mode:0o700});
  await fs.writeFile(transcriptPath,JSON.stringify({step_index:0,status:'DONE',content:'x'.repeat(1200000)})+'\n');
  await f.send('PreInvocation',{transcriptPath});
  await f.send('PostInvocation',{transcriptPath});
  assert.equal((await core.inspectStatus(f.options)).delivery.lastError,'TRANSCRIPT_BACKLOG_SKIPPED');
  const row={step_index:1,status:'DONE',source:'MODEL',type:'PLANNER_RESPONSE',content:'recovered',input_tokens:10,output_tokens:2};
  await fs.appendFile(transcriptPath,JSON.stringify(row)+'\n');
  await f.send('Stop',{transcriptPath,fullyIdle:true,terminationReason:'NO_TOOL_CALL'});
  assert.equal(f.spans().filter(s=>attrs(s)['fi.span.kind']==='LLM').length,1);
  const status=await core.inspectStatus(f.options);
  assert.ok(status.delivery.dropped>=1);
});
test('bundled process hook is neutral for invalid/oversized input and hides parser errors',async t=>{
  const f=await fixture(t), entry=path.join(__dirname,'../plugin/hook.cjs');
  for (const input of ['not json','[]','x'.repeat(300000)]) {
    const result=await run(process.execPath,[entry,'Stop'],f.env,input);
    assert.equal(result.code,0);assert.equal(result.stdout,'{"decision":""}\n');assert.equal(result.stderr,'');
  }
});
test('per-project configuration may reduce content but cannot redirect saved keys',async t=>{
  const f=await fixture(t);
  const projectPaths=await core.configPaths({...f.options,projectDir:f.project});
  await core.writeProtected(projectPaths.project,JSON.stringify({antigravity:{schemaVersion:1,captureContent:false}}));
  await f.send('PreInvocation',{lastUserInput:'PRIVATE'});await f.send('PostInvocation');
  await f.send('Stop',{fullyIdle:true,terminationReason:'NO_TOOL_CALL',finalModelOutput:'PRIVATE'});
  assert.ok(!JSON.stringify(f.spans()).includes('PRIVATE'));
  await core.writeProtected(projectPaths.project,JSON.stringify({antigravity:{schemaVersion:1,transport:{endpoint:'https://example.com/traces'}}}));
  await f.send('PreInvocation',{initialNumSteps:8});await f.send('PostInvocation',{initialNumSteps:8});
  await f.send('Stop',{fullyIdle:true});assert.equal(f.spans().length,2);
});
test('supported CLI major tolerates updates but rejects unqualified versions',()=>{
  for(const version of ['1.5.2','1.5.3','1.16.0']) assert.equal(supportedHostVersion(version),true);
  for(const version of ['2.0.0','1.5.2-beta','garbage','1.5.2\n']) assert.equal(supportedHostVersion(version),false);
});
test('unsafe project layers use the approved user profile and expose a diagnostic',async t=>{
  const f=await fixture(t);
  await fs.chmod(f.project,0o775);
  const effective=await core.resolveConfig({...f.options,projectDir:f.project});
  assert.equal(effective.enabled,true);
  assert.ok(effective.issues.includes('PROJECT_CONFIG_IGNORED'));
  assert.ok((await core.inspectStatus({...f.options,projectDir:f.project})).issues.includes('PROJECT_CONFIG_IGNORED'));
  await f.send('PreInvocation',{lastUserInput:'external volume'});await f.send('PostInvocation');
  await f.send('Stop',{fullyIdle:true,terminationReason:'NO_TOOL_CALL'});
  assert.equal(f.spans().length,2);
});
test('unknown hook shapes are reported without corrupting a valid turn',async t=>{
  const f=await fixture(t);
  await f.send('PreInvocation');
  await f.send('PostToolUse',{stepIdx:'unknown'});
  assert.equal((await core.inspectStatus(f.options)).delivery.lastError,'UNKNOWN_HOOK_SHAPE');
  await f.send('PostInvocation');await f.send('Stop',{fullyIdle:true,terminationReason:'NO_TOOL_CALL'});
  assert.equal(f.spans().length,2);
});
test('pin stable interpreter symlink rather than copying or resolving it away',async t=>{
  const f=await fixture(t),bin=path.join(f.home,'bin');await fs.mkdir(bin);
  const node=path.join(bin,'node');await fs.symlink(process.execPath,node);
  assert.equal(await pinnedNode(process.execPath,bin),node);
});
test('overlapping hooks wait; stale locks recover; concurrent stops export once',async t=>{
  const f=await fixture(t);await f.send('PreInvocation');
  const dir=path.join(f.resolved.paths.stateDir,'antigravity-conversations');
  const name=(await fs.readdir(dir)).find(x=>x.endsWith('.json'));
  const lock=path.join(dir,name.replace('.json','.event-lock-v2'));
  await fs.mkdir(lock);await fs.utimes(lock,new Date(0),new Date(0));
  await Promise.all(Array.from({length:12},(_,i)=>f.send('PostToolUse',{stepIdx:i+2,toolCall:{name:'tool_'+i,args:{i}},result:'ok'})));
  await f.send('PostInvocation');
  await Promise.all([f.send('Stop',{fullyIdle:true,terminationReason:'NO_TOOL_CALL'}),f.send('Stop',{fullyIdle:true,terminationReason:'NO_TOOL_CALL'})]);
  assert.equal(f.spans().length,14);
  assert.equal(f.spans().filter(s=>!s.parentSpanId).length,1);
  assert.equal((await core.inspectStatus(f.options)).delivery.dropped,0);
  assert.ok(f.spans().filter(s=>attrs(s)['fi.span.kind']==='TOOL').every(s=>attrs(s)['tool.name']===s.name));
});
test('terminal capture waits for a response written three seconds after Stop',async t=>{
  const f=await fixture(t);
  const transcriptPath=path.join(f.home,'.gemini/antigravity-cli/brain',p.conversationId,'.system_generated/logs/transcript_full.jsonl');
  await fs.mkdir(path.dirname(transcriptPath),{recursive:true,mode:0o700});
  await fs.writeFile(transcriptPath,'');
  await f.send('PreInvocation',{transcriptPath,lastUserInput:'prompt context',modelName:'gemini-test'});
  await f.send('PostInvocation',{transcriptPath});
  const pending=f.send('Stop',{transcriptPath,fullyIdle:true,terminationReason:'NO_TOOL_CALL'});
  await new Promise(r=>setTimeout(r,3000));
  await fs.appendFile(transcriptPath,JSON.stringify({step_index:1,status:'DONE',source:'MODEL',type:'PLANNER_RESPONSE',content:'',tool_calls:[{name:'read_file',args:{password:'late-private',path:'safe.txt'}}],input_tokens:10,output_tokens:2})+'\n');
  await pending;
  const llms=f.spans().filter(s=>attrs(s)['fi.span.kind']==='LLM');assert.equal(llms.length,1);
  assert.equal(attrs(llms[0])['input.value'],'prompt context');
  assert.ok(attrs(llms[0])['output.value'].includes('read_file'));
  assert.ok(!JSON.stringify(f.spans()).includes('late-private'));
  assert.equal(f.spans().reduce((n,s)=>n+Number(attrs(s)['gen_ai.usage.input_tokens']??0),0),10);
});
test('nonterminal runtime imports no OTel exporter',async t=>{
  const f=await fixture(t);
  const script=`const Module=require('node:module');const load=Module._load;Module._load=function(name,...rest){if(name==='@traceai/fi-core'||name.includes('otlp-transformer'))throw new Error('EXPORTER_LOADED');return load.call(this,name,...rest)};const runtime=require(${JSON.stringify(path.join(__dirname,'../dist/runtime.js'))});runtime.handleEvent('PreInvocation',${JSON.stringify(f.payload({}))},{home:${JSON.stringify(f.home)},env:{}}).catch(()=>process.exitCode=1);`;
  const result=await run(process.execPath,['-e',script],f.env);
  assert.equal(result.code,0,result.stderr);
});
test('real agy 1.5.2 native installation, idempotence, standalone hooks and ownership-safe uninstall',
  {skip:process.env.AGY_TEST_REAL!=='1'},async t=>{
  const f=await fixture(t), ctx={config:f.resolved,scope:'user',projectDir:f.project};
  assert.deepEqual(await adapter.detect(ctx),{supported:true,version:'1.5.2',reason:undefined});
  const install=await adapter.install(ctx);assert.equal(install.registered,true);
  assert.equal((await adapter.install(ctx)).registered,true);
  const list=await run('agy',['plugin','list','--json'],f.env);assert.equal(list.code,0);
  assert.equal(JSON.parse(list.stdout).filter(p=>p.name===PLUGIN_NAME).length,1);
  assert.equal((await run('agy',['plugin','disable',PLUGIN_NAME],f.env)).code,0);
  assert.equal((await adapter.inspect(ctx)).detail,'ENABLE_PLUGIN_IN_AGY');
  assert.equal((await adapter.install(ctx)).detail,'ENABLE_PLUGIN_IN_AGY');
  assert.equal((await run('agy',['plugin','enable',PLUGIN_NAME],f.env)).code,0);
  assert.equal((await adapter.inspect(ctx)).registered,true);
  const previous=JSON.parse(await fs.readFile(path.join(f.resolved.paths.configDir,'antigravity-registration.json'),'utf8'));
  const bin=path.join(f.home,'stable-bin');await fs.mkdir(bin,{mode:0o700});await fs.symlink(process.execPath,path.join(bin,'node'));
  const originalPath=process.env.PATH;
  try {
    process.env.PATH=bin+path.delimiter+originalPath;
    assert.equal((await adapter.install(ctx)).registered,true);
  } finally {process.env.PATH=originalPath;}
  await assert.rejects(fs.stat(previous.runtimePath),{code:'ENOENT'});
  const receipt=JSON.parse(await fs.readFile(path.join(f.resolved.paths.configDir,'antigravity-registration.json'),'utf8'));
  assert.ok(receipt.node);assert.ok(receipt.runtimePath);
  assert.deepEqual((await fs.readdir(receipt.runtimePath)).sort(),['hook.cjs','hooks.json','plugin.json']);
  const directory=path.join(f.home,'.gemini/config/plugins',PLUGIN_NAME);
  const manifest=JSON.parse(await fs.readFile(path.join(directory,'hooks.json'),'utf8'));
  const hook=manifest[PLUGIN_NAME];
  for (const [event,data] of [['PreInvocation',{lastUserInput:'installed hook prompt'}],['PostToolUse',{stepIdx:2,toolCall:{name:'view_file',args:{file:'README.md'}},result:'contents'}],['PostInvocation',{}],['Stop',{fullyIdle:true,terminationReason:'NO_TOOL_CALL',finalModelOutput:'done'}]]) {
    const handler=event==='PostToolUse'?hook[event][0].hooks[0]:hook[event][0];
    const result=await run('/bin/sh',['-c',handler.command],{HOME:f.home,PATH:'/usr/bin:/bin'},JSON.stringify(f.payload(data)),directory);
    assert.equal(result.code,0);assert.equal(result.stdout,neutral(event));assert.equal(result.stderr,'');
  }
  assert.equal(f.spans().length,3);
  const badFile=path.join(directory,'hooks.json');const original=await fs.readFile(badFile,'utf8');
  await fs.writeFile(badFile,original+' ');
  assert.equal((await adapter.uninstall(ctx)).registered,true);
  await fs.writeFile(badFile,original);
  assert.equal((await adapter.uninstall(ctx)).registered,false);
  await assert.rejects(fs.stat(receipt.runtimePath),{code:'ENOENT'});
  console.log('REAL_AGY_NATIVE',JSON.stringify({version:'1.5.2',install:true,idempotent:true,strippedPathHookReplaySpans:3,generation:false}));
});
