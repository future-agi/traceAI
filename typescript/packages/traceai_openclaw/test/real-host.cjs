// E2B-only real OpenClaw + official plugin qualification. Synthetic keys; loopback model + OTLP.
const assert = require('node:assert/strict');
const http = require('node:http');
const { spawn } = require('node:child_process');
const fs = require('node:fs/promises');
const { join } = require('node:path');
const cli = process.env.FI_TEST_CLI || '/work/typescript/packages/traceai_agents/dist/cli.js';
const evidenceDir = '/work/evidence';
const nativeFile = '/work/custom-home/.openclaw/openclaw.json';
const evidence = { host: '2026.10.5', node: process.version, scenarios: [], spans: [] };
let captured = [], requests = [], mode = 'tools', retries = 0, exportFails = false;
let raceExportStarted, raceDelayed = false, commandIndex = 0;
const raceStarted = new Promise(resolve => { raceExportStarted = resolve; });
function attrs(span) { return Object.fromEntries((span.attributes || []).map(a => [a.key, a.value.stringValue ?? a.value.intValue ?? a.value.doubleValue ?? a.value.boolValue])); }
function run(file, args, env = process.env, allowed = [0]) {
  return new Promise((resolve, reject) => {
    const child = spawn(file, args, {env, stdio:['ignore','pipe','pipe']});
    let stdout='', stderr='';
    child.stdout.on('data', b => stdout+=b); child.stderr.on('data', b=>stderr+=b);
    const timer = setTimeout(()=>child.kill('SIGKILL'), 180000);
    child.on('error',reject);
    child.on('close',async code=>{
      clearTimeout(timer);
      await fs.writeFile(join(evidenceDir,`command-${commandIndex++}.log`), stdout+'\nSTDERR\n'+stderr);
      if (!allowed.includes(code)) return reject(new Error(`command failed (${code}): ${file} ${args.slice(0,3).join(' ')}\n${stdout}\n${stderr}`));
      resolve({code,stdout,stderr});
    });
  });
}
const server = http.createServer(async(req,res)=>{
  const chunks=[];for await(const chunk of req)chunks.push(chunk);
  const data=Buffer.concat(chunks);
  if(req.url==='/tracer/v1/traces') {
    assert.equal(req.headers['x-api-key'],'e2b-dummy-public'); assert.equal(req.headers['x-secret-key'],'e2b-dummy-secret');
    assert.equal(req.headers['x-project-name'],'openclaw-test');
    const payload=JSON.parse(data.toString());
    if(exportFails && payload.resourceSpans?.length){res.writeHead(503,{'content-type':'application/json'});res.end('{}');return;}
    for(const resource of payload.resourceSpans??[]) for(const scope of resource.scopeSpans??[]) for(const span of scope.spans??[])
      captured.push({...span,resource:resource.resource,scope:scope.scope});
    if(mode==='race' && !raceDelayed) {
      raceDelayed=true; raceExportStarted();
      setTimeout(()=>{res.writeHead(200,{'content-type':'application/json'});res.end('{}');},3000);return;
    }
    res.writeHead(200,{'content-type':'application/json'});res.end('{}');return;
  }
  if(req.url?.endsWith('/chat/completions')) {
    if(mode==='race') await Promise.race([raceStarted,new Promise(resolve=>setTimeout(resolve,2500))]);
    const body=JSON.parse(data.toString());requests.push({stream:body.stream,toolCount:body.tools?.length||0});
    if(mode==='retry'&&retries++===0){res.writeHead(503,{'content-type':'application/json'});res.end(JSON.stringify({error:{message:'synthetic retry',type:'server_error'}}));return;}
    if(mode==='auth-failure'){res.writeHead(401,{'content-type':'application/json'});res.end(JSON.stringify({error:{message:'synthetic provider auth failure',type:'authentication_error'}}));return;}
    const tool=(body.tools||[]).find(t=>t.function?.name==='read');
    const afterTool=body.messages.some(m=>m.role==='tool');
    const useTool=(mode==='tools'||mode==='tool-error')&&tool&&!afterTool;
    const id='mock-'+requests.length;
    const output=useTool?null:'LOCAL_MOCK_DONE';
    const toolPath=mode==='tool-error'?'/work/project/HISTORICAL_MISSING_FILE':'/work/project/README.md';
    const toolCalls=useTool?[{index:0,id:'read-e2b',type:'function',function:{name:'read',arguments:JSON.stringify({path:toolPath})}}]:undefined;
    res.writeHead(200,{'content-type':'text/event-stream','cache-control':'no-cache'});
    const chunk=(choices,usage)=>res.write('data: '+JSON.stringify({id,object:'chat.completion.chunk',created:1,model:'local-mock',choices,...(usage?{usage}:{})})+'\n\n');
    chunk([{index:0,delta:{role:'assistant',...(output?{content:output}:{tool_calls:toolCalls})},finish_reason:null}]);
    chunk([{index:0,delta:{},finish_reason:useTool?'tool_calls':'stop'}],{prompt_tokens:11,completion_tokens:7,total_tokens:18,
      ...(mode==='cache' ? {prompt_tokens_details:{cached_tokens:5}} : {})});
    res.end('data: [DONE]\n\n');return;
  }
  res.writeHead(404);res.end();
});
(async()=>{
  await fs.mkdir(evidenceDir,{recursive:true});
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  const base='http://127.0.0.1:'+server.address().port;
  Object.assign(process.env,{
    OPENCLAW_HOME:'/work/custom-home',
    FI_AGENT_CONFIG_DIR:'/work/fi-config',XDG_STATE_HOME:'/work/fi-state',
    FI_API_KEY:'e2b-dummy-public',FI_SECRET_KEY:'e2b-dummy-secret',FI_OTLP_ENDPOINT:base+'/tracer/v1/traces',
  });
  delete process.env.OPENCLAW_STATE_DIR;
  delete process.env.OPENCLAW_CONFIG_PATH;
  for(const dir of ['/work/custom-home/.openclaw','/work/project'])await fs.mkdir(dir,{recursive:true,mode:0o700});
  const nativePathOutput=await run('openclaw',['config','file']);
  assert.ok(nativePathOutput.stdout.trim().endsWith(nativeFile));
  evidence.scenarios.push({name:'native-custom-home-discovery',passed:true});
  const hostRequire=require('node:module').createRequire('/work/host/package.json');
  const nativeSecurity=await import(require('node:url').pathToFileURL(hostRequire.resolve('openclaw/plugin-sdk/security-runtime')).href);
  const {normalizeSpan}=require('../dist/normalize.js');
  for(const key of ['secret_key','secret-key','secretKey']) {
    const marker='SYNTHETIC_APPLICATION_CREDENTIAL_123456';
    for(const source of [JSON.stringify({[key]:marker}),key+'='+marker]) {
      const upstream=nativeSecurity.redactSensitiveText(source);
      const filtered=normalizeSpan({name:'openclaw.tool.execution',attributes:{'openclaw.content.tool_output':upstream},
        status:{code:1},spanContext:()=>({traceId:'a'.repeat(32),spanId:'b'.repeat(16),traceFlags:1})},
        {captureContent:true,hideInputs:false,hideOutputs:false,hideErrorDetails:false,projectName:'test'});
      assert.ok(!filtered.attributes['output.value'].includes(marker));
    }
  }
  evidence.scenarios.push({name:'native-redactor-to-unified-core-alias-protection',passed:true});
  await fs.writeFile('/work/project/README.md','# OpenClaw E2B evidence\n',{mode:0o600});
  const native={
    agents:{defaults:{workspace:'/work/project',model:{primary:'mock/local-mock'}},entries:{'test-agent':{workspace:'/work/project'}}},
    models:{mode:'merge',providers:{mock:{baseUrl:base+'/v1',apiKey:'dummy-model-key',api:'openai-completions',models:[{id:'local-mock',name:'local-mock',contextWindow:32000,maxTokens:1000,cost:{input:0,output:0,cacheRead:0,cacheWrite:0}}]}}},
    gateway:{mode:'local',auth:{mode:'token',token:'e2b-dummy-gateway-token'}},
  };
  await fs.writeFile(nativeFile,JSON.stringify({...native,diagnostics:{otel:{enabled:true}}}),{mode:0o600});
  const conflict=await run(process.execPath,[cli,'setup','openclaw','--project','openclaw-test','--json'],process.env,[2]);
  assert.equal(JSON.parse(conflict.stdout).code,'EXISTING_OPENCLAW_EXPORTER');
  const conflictStatus=await run(process.execPath,[cli,'status','openclaw','--json'],process.env,[2]);
  assert.equal(JSON.parse(conflictStatus.stdout).detail,'EXISTING_OPENCLAW_EXPORTER');
  assert.match(JSON.parse(conflictStatus.stdout).diagnostics.find(d=>d.code==='EXISTING_OPENCLAW_EXPORTER').action,/Keep your existing direct exporter/);
  assert.ok(JSON.parse(conflictStatus.stdout).issues.includes('DUPLICATE_REGISTRATION'));
  evidence.scenarios.push({name:'actionable-exporter-conflict',passed:true});
  await fs.writeFile(nativeFile,JSON.stringify(native),{mode:0o600});
  const pending=await run(process.execPath,[cli,'setup','openclaw','--project','openclaw-test','--json'],process.env,[2]);
  assert.equal(JSON.parse(pending.stdout).code,'TRUST_REQUIRED');
  evidence.scenarios.push({name:'headless-trust-not-auto-approved',passed:true});
  // Test-only explicit consent for our known, sandbox-owned lifecycle fixture.
  // Production setup forwards the host's interactive prompt, never this flag.
  const distribution=`/work/fi-config/openclaw-lifecycle/${require('../package.json').version}`;
  await fs.cp(distribution,'/work/older-lifecycle',{recursive:true});
  const oldPackage=JSON.parse(await fs.readFile('/work/older-lifecycle/package.json','utf8'));
  oldPackage.version='0.0.9'; // synthetic previous package version, real native installation
  await fs.writeFile('/work/older-lifecycle/package.json',JSON.stringify(oldPackage));
  await run('openclaw',['plugins','install','/work/older-lifecycle','--force','--accept-capabilities']);
  const older=JSON.parse((await run('openclaw',['plugins','inspect','futureagi-openclaw','--json'])).stdout).plugin;
  assert.equal(older.version,'0.0.9');assert.equal(older.status,'loaded');
  const upgrade=JSON.parse((await run(process.execPath,[cli,'update','openclaw','--json'],process.env,[2])).stdout);
  assert.equal(upgrade.trust,'required');assert.equal(upgrade.detail,'OPENCLAW_LIFECYCLE_REVIEW_REQUIRED');
  assert.match(upgrade.recovery,/plugins uninstall/);
  const stillOlder=JSON.parse((await run('openclaw',['plugins','inspect','futureagi-openclaw','--json'])).stdout).plugin;
  assert.equal(stillOlder.version,'0.0.9','headless update never bypasses consent');
  // Test-only explicit approvals of native replacement in this disposable home.
  await run('openclaw',['plugins','uninstall','futureagi-openclaw','--force']);
  await run('openclaw',['plugins','install',distribution,'--force','--accept-capabilities']);
  await run('openclaw',['plugins','enable','futureagi-openclaw']);
  const upgradedPlugin=JSON.parse((await run('openclaw',['plugins','inspect','futureagi-openclaw','--json'])).stdout).plugin;
  assert.equal(upgradedPlugin.enabled,true);assert.equal(upgradedPlugin.status,'loaded');
  assert.equal(upgradedPlugin.version,require('../package.json').version);
  const updated=JSON.parse((await run(process.execPath,[cli,'update','openclaw','--json'])).stdout);
  assert.equal(updated.registered,true);
  evidence.scenarios.push({name:'older-plugin-upgrade-with-explicit-consent',passed:true});
  const setup=await run(process.execPath,[cli,'setup','openclaw','--project','openclaw-test','--json']);
  const result=JSON.parse(setup.stdout);
  assert.equal(result.registered,true);evidence.scenarios.push({name:'setup',result});
  // Setup is idempotent and never requires account login or grants conversation hooks.
  await run(process.execPath,[cli,'setup','openclaw','--project','openclaw-test','--json']);
  evidence.scenarios.push({name:'idempotent-setup',passed:true});
  await run('openclaw',['config','validate']);
  const nativeAfter=JSON.parse(await fs.readFile(nativeFile,'utf8'));
  assert.equal(nativeAfter.diagnostics.otel.enabled,false);
  assert.equal(nativeAfter.plugins.entries['diagnostics-otel'].hooks,undefined);
  for(const path of ['/work/fi-config/credentials.json','/work/custom-home/.openclaw/futureagi.json']) assert.equal((await fs.stat(path)).mode&0o777,0o600);
  const invocation=['run','openclaw','--','agent','--local','--agent','test-agent','--session-id','e2b-conversation','--message','Read README.md and reply with the heading.','--json','--timeout','45'];
  const first=await run(process.execPath,[cli,...invocation]);
  evidence.scenarios.push({name:'real-local-tool-turn',exit:first.code,modelRequests:requests.length,spans:captured.length,names:captured.map(s=>s.name)});
  assert.ok(requests.length>=2,'real model/tool loop must call mock twice');
  assert.ok(captured.some(s=>s.name==='openclaw.tool.execution'),'native tool span');
  assert.ok(captured.filter(s=>s.name==='openclaw.tool.execution').every(s=>attrs(s)['tool.name']==='read'),'backend tool.name is present');
  assert.ok(captured.every(s=>BigInt(s.endTimeUnixNano)>0n && BigInt(s.startTimeUnixNano)>0n),'native timing survives getter-backed ReadableSpan fields');
  const models=captured.filter(s=>s.name==='openclaw.model.call');
  assert.ok(models.length>=2,'native model calls');
  assert.ok(!captured.some(s=>s.name==='openclaw.model.usage'),'rollups suppressed');
  assert.equal(models.reduce((n,s)=>n+Number(attrs(s)['gen_ai.usage.input_tokens']||0),0),22);
  assert.ok(models.every(s=>s.parentSpanId),'model ancestry retained');
  assert.ok(models.every(s=>attrs(s)['session.id']),'explicit session correlation');
  assert.ok(models.some(s=>attrs(s)['input.value']),'native input captured');
  assert.ok(captured.every(s=>!Object.keys(Object.fromEntries(s.resource.attributes.map(a=>[a.key,a.value]))).some(k=>k.startsWith('host.')||k.startsWith('process.'))));
  const firstRoot=captured.find(s=>s.name==='openclaw.prompt');
  assert.ok(firstRoot);assert.equal(captured.filter(s=>!s.parentSpanId).length,1);
  assert.equal(new Set(captured.map(s=>s.traceId)).size,1);
  evidence.spans.push(...captured); captured=[];requests=[];
  mode='plain';
  await run(process.execPath,[cli,...invocation]);
  const resumedRoot=captured.find(s=>s.name==='openclaw.prompt');
  assert.ok(resumedRoot);assert.notEqual(resumedRoot.traceId,firstRoot.traceId);
  assert.equal(attrs(resumedRoot)['session.id'],attrs(firstRoot)['session.id']);
  assert.equal(captured.filter(s=>!s.parentSpanId).length,1);
  evidence.scenarios.push({name:'resumed-session-separate-prompts',passed:true});
  assert.ok(captured.some(s=>s.name==='openclaw.model.call'&&attrs(s)['input.value']?.includes('LOCAL_MOCK_DONE')),
    'resumed native history contains prior assistant output before privacy filtering');
  evidence.spans.push(...captured);captured=[];requests=[];
  for(const flag of ['FI_HIDE_OUTPUTS','FI_HIDE_OUTPUT_TEXT','FI_HIDE_OUTPUT_MESSAGES']) {
    await run(process.execPath,[cli,...invocation],{...process.env,FI_HIDE_ERROR_DETAILS:'false',[flag]:'true'});
    assert.equal(requests.length,1);
    assert.ok(captured.some(s=>attrs(s)['fi.span.kind']==='LLM'));
    assert.ok(!JSON.stringify(captured).includes('LOCAL_MOCK_DONE'),'prior and current assistant output stay hidden');
    assert.ok(captured.filter(s=>attrs(s)['fi.span.kind']==='LLM').every(s=>attrs(s)['input.value']===undefined));
    evidence.scenarios.push({name:'resumed-history-'+flag,passed:true});
    evidence.spans.push(...captured);captured=[];requests=[];
  }
  mode='tool-error';
  const errorHistoryInvocation=invocation.map(s=>s==='e2b-conversation'?'error-history-conversation':s);
  await run(process.execPath,[cli,...errorHistoryInvocation]);
  assert.ok(captured.some(s=>s.name==='openclaw.tool.execution'&&s.status.code===2));
  assert.ok(captured.some(s=>s.name==='openclaw.model.call'&&attrs(s)['input.value']?.includes('HISTORICAL_MISSING_FILE')),
    'native model history really includes the failed tool result');
  evidence.scenarios.push({name:'historical-tool-error-baseline',passed:true});
  evidence.spans.push(...captured);captured=[];requests=[];
  mode='plain';
  await run(process.execPath,[cli,...errorHistoryInvocation],{...process.env,FI_HIDE_ERROR_DETAILS:'true'});
  assert.equal(requests.length,1);
  assert.ok(captured.some(s=>attrs(s)['fi.span.kind']==='LLM'&&attrs(s)['output.value']?.includes('LOCAL_MOCK_DONE')));
  assert.ok(!JSON.stringify(captured).includes('HISTORICAL_MISSING_FILE'));
  assert.equal(captured.find(s=>s.name==='openclaw.prompt').status.code,1);
  evidence.scenarios.push({name:'resumed-history-FI_HIDE_ERROR_DETAILS',passed:true});
  evidence.spans.push(...captured);captured=[];requests=[];
  mode='retry'; retries=0;
  await run(process.execPath,[cli,...invocation.map(s=>s==='e2b-conversation'?'retry-conversation':s)]);
  evidence.scenarios.push({name:'retry',modelRequests:requests.length,spans:captured.length,traceIds:[...new Set(captured.map(s=>s.traceId))],names:captured.map(s=>s.name)});
  assert.ok(requests.length>=2); assert.ok(!captured.some(s=>s.name==='openclaw.model.usage'));
  assert.equal(new Set(captured.map(s=>s.traceId)).size,1,'one user prompt owns every retry');
  assert.equal(captured.filter(s=>!s.parentSpanId).length,1,'one prompt root');
  const retryRoot=captured.find(s=>s.name==='openclaw.prompt');assert.ok(retryRoot);
  assert.equal(retryRoot.status.code,1,'successful retry leaves the prompt successful');
  assert.equal(captured.filter(s=>s.name==='openclaw.model.call').length,2,'both real model attempts remain');
  assert.equal(new Set(captured.filter(s=>s.name==='openclaw.harness.run').map(s=>attrs(s)['fi.agent.native.trace_id'])).size,2);
  for(const child of captured.filter(s=>s!==retryRoot)) {
    assert.ok(captured.some(s=>s.spanId===child.parentSpanId),'each native tree is connected beneath the prompt');
    assert.ok(attrs(child)['fi.agent.native.span_id']);
  }
  assert.equal(captured.filter(s=>s.name==='openclaw.model.call').reduce((n,s)=>n+Number(attrs(s)['gen_ai.usage.input_tokens']||0),0),11,'only successful retry owns usage');
  assert.ok(captured.some(s=>s.name==='openclaw.model.call'&&s.status?.code===2),'failed model attempt retains error status');
  evidence.spans.push(...captured);captured=[];requests=[];
  mode='tools';
  await run(process.execPath,[cli,...invocation.map(s=>s==='e2b-conversation'?'semconv-conversation':s)],
    {...process.env,OTEL_SEMCONV_STABILITY_OPT_IN:'gen_ai_latest_experimental'});
  const experimentalModels=captured.filter(s=>attrs(s)['fi.span.kind']==='LLM');
  assert.equal(experimentalModels.length,2);
  assert.equal(experimentalModels.reduce((n,s)=>n+Number(attrs(s)['gen_ai.usage.input_tokens']||0),0),22);
  assert.ok(experimentalModels.every(s=>attrs(s)['input.value']&&attrs(s)['output.value']));
  assert.ok(captured.some(s=>attrs(s)['fi.span.kind']==='TOOL'));
  assert.ok(captured.some(s=>s.name==='openclaw.run'));
  assert.ok(captured.some(s=>s.name==='openclaw.harness.run'));
  assert.ok(!captured.some(s=>s.name==='openclaw.model.usage'));
  evidence.scenarios.push({name:'semconv-opt-in',modelRequests:requests.length,llmSpans:experimentalModels.length,spans:captured.length});
  evidence.spans.push(...captured);captured=[];requests=[];
  mode='race';
  const delayed=await run(process.execPath,[cli,...invocation.map(s=>s==='e2b-conversation'?'race-conversation':s)]);
  evidence.scenarios.push({name:'terminal-flush-joins-slow-export',exit:delayed.code,modelRequests:requests.length,spans:captured.length,names:captured.map(s=>s.name)});
  evidence.raceStatus=JSON.parse((await run(process.execPath,[cli,'status','openclaw','--json'],process.env,[0,2])).stdout);
  assert.equal(delayed.code,0);assert.equal(requests.length,1);assert.equal(raceDelayed,true);
  for(const name of ['openclaw.context.assembled','openclaw.model.call','openclaw.run','openclaw.harness.run'])
    assert.ok(captured.some(s=>s.name===name),'terminal drain must export '+name);
  evidence.spans.push(...captured);captured=[];requests=[];
  mode='plain';
  await run(process.execPath,[cli,...invocation.map(s=>s==='e2b-conversation'?'node-options-conversation':s)],
    {...process.env,NODE_OPTIONS:'--max-old-space-size=4096 --use-system-ca --use-openssl-ca'});
  assert.equal(requests.length,1);assert.ok(captured.some(s=>attrs(s)['fi.span.kind']==='LLM'));
  evidence.scenarios.push({name:'nonconflicting-node-options',modelRequests:requests.length,spans:captured.length});
  evidence.spans.push(...captured);captured=[];requests=[];
  const sharedProject='/work/shared-project';
  await fs.mkdir(sharedProject,{mode:0o775}); await fs.chmod(sharedProject,0o775);
  await run(process.execPath,[cli,'run','openclaw','--project-dir',sharedProject,...invocation.slice(2).map(s=>s==='e2b-conversation'?'shared-volume-conversation':s)]);
  assert.equal(requests.length,1);
  assert.ok(captured.some(s=>attrs(s)['fi.span.kind']==='LLM'));
  assert.ok(captured.some(s=>s.name==='openclaw.harness.run'));
  const sharedStatus=JSON.parse((await run(process.execPath,[cli,'status','openclaw','--project-dir',sharedProject,'--json'],process.env,[0,2])).stdout);
  assert.ok(sharedStatus.issues.includes('PROJECT_CONFIG_IGNORED'));
  evidence.scenarios.push({name:'shared-volume-user-config-fallback',modelRequests:requests.length,spans:captured.length});
  evidence.spans.push(...captured);captured=[];requests=[];
  mode='cache';
  await run(process.execPath,[cli,...invocation.map(s=>s==='e2b-conversation'?'cache-conversation':s)]);
  const cacheModels=captured.filter(s=>s.name==='openclaw.model.call');
  assert.equal(cacheModels.length,1);
  assert.equal(Number(attrs(cacheModels[0])['gen_ai.usage.cache_read_tokens']),5);
  assert.equal(Number(attrs(cacheModels[0])['gen_ai.usage.input_tokens']),11);
  assert.equal(Number(attrs(cacheModels[0])['gen_ai.usage.total_tokens']),18);
  assert.equal(attrs(cacheModels[0])['gen_ai.usage.cache_read.input_tokens'],undefined);
  evidence.scenarios.push({name:'canonical-cache-usage-no-double-count',passed:true});
  evidence.spans.push(...captured);captured=[];requests=[];
  mode='plain';
  await run(process.execPath,[cli,'setup','openclaw','--project','openclaw-test','--no-capture-content','--json']);
  const privacySpansStart=evidence.spans.length;
  await run(process.execPath,[cli,...invocation]);
  evidence.scenarios.push({name:'privacy-off',spans:captured.length});
  assert.ok(captured.length>0);
  assert.ok(captured.every(s=>attrs(s)['input.value']===undefined&&attrs(s)['output.value']===undefined&&!s.status?.message));
  evidence.spans.push(...captured);captured=[];requests=[];
  await run(process.execPath,[cli,...invocation.map(s=>s==='e2b-conversation'?'semconv-private-conversation':s)],
    {...process.env,OTEL_SEMCONV_STABILITY_OPT_IN:'gen_ai_latest_experimental'});
  assert.ok(captured.some(s=>attrs(s)['fi.span.kind']==='LLM'));
  assert.ok(captured.every(s=>attrs(s)['input.value']===undefined&&attrs(s)['output.value']===undefined&&!s.status?.message));
  evidence.scenarios.push({name:'semconv-privacy-off',spans:captured.length});
  evidence.spans.push(...captured);captured=[];requests=[];
  exportFails=true;
  const failedExport=await run(process.execPath,[cli,...invocation]);
  evidence.scenarios.push({name:'export-503-fails-open',exit:failedExport.code,modelRequests:requests.length});
  assert.equal(failedExport.code,0);assert.ok(requests.length>0);exportFails=false;requests=[];captured=[];
  const deliveryStatus=JSON.parse((await run(process.execPath,[cli,'status','openclaw','--json'],process.env,[0,2])).stdout);
  assert.equal(deliveryStatus.delivery.exported,evidence.spans.length-privacySpansStart,'receipts belong to the current setup epoch');
  assert.equal(deliveryStatus.delivery.dropped,5,'failed export, including the prompt root, is durably reported');
  assert.equal(deliveryStatus.delivery.lastError,'UNVERIFIED');
  evidence.deliveryBeforeDisable=deliveryStatus.delivery;
  await run(process.execPath,[cli,'disable','openclaw','--json']);
  await run(process.execPath,[cli,...invocation]);
  evidence.scenarios.push({name:'disabled-agent-still-runs',modelRequests:requests.length,spans:captured.length});
  assert.ok(requests.length>0);assert.equal(captured.length,0);
  const gateway=await run(process.execPath,[cli,'run','openclaw','--','gateway'],process.env,[2]);
  assert.match(gateway.stderr,/LOCAL_AGENT_ONLY/);evidence.scenarios.push({name:'gateway-launch-explicitly-unsupported',passed:true});
  const status=await run(process.execPath,[cli,'status','openclaw','--check','--json'],process.env,[0,2]);
  evidence.status=JSON.parse(status.stdout);
  assert.equal(evidence.status.credentialCheck,'ACCEPTED');
  assert.equal(evidence.status.delivery.exported,0,'disable fences the previous delivery epoch');
  assert.equal(evidence.status.delivery.dropped,0);
  // A native provider auth failure fences the next prompt before diagnostics emit
  // any model/run spans. Both launched prompts must remain visible as errors.
  await run(process.execPath,[cli,'enable','openclaw','--json']);
  const failureSpansStart=evidence.spans.length;
  mode='auth-failure'; captured=[];requests=[];
  const authInvocation=invocation.map(s=>s==='e2b-conversation'?'auth-conversation':s);
  const authFailure=await run(process.execPath,[cli,...authInvocation],process.env,[0,1]);
  assert.ok(requests.length>0,'real host reached synthetic provider 401');
  const authRoot=captured.find(s=>s.name==='openclaw.prompt');
  assert.ok(authRoot); assert.equal(authRoot.status.code,2);
  evidence.scenarios.push({name:'fatal-provider-failure-root',exit:authFailure.code,passed:true});
  evidence.spans.push(...captured);captured=[];requests=[];
  mode='plain';
  const cooldown=await run(process.execPath,[cli,...authInvocation],process.env,[1]);
  assert.equal(requests.length,0,'provider cooldown rejected the prompt before a model request');
  assert.equal(captured.length,1,'early-failed prompt exports its root, not an invented model');
  const earlyRoot=captured[0];
  assert.equal(earlyRoot.name,'openclaw.prompt');assert.equal(earlyRoot.status.code,2);
  assert.equal(attrs(earlyRoot)['fi.agent.gap'],'MISSING_NATIVE_SPANS');
  assert.equal(attrs(earlyRoot)['session.id'],attrs(authRoot)['session.id']);
  assert.equal(attrs(earlyRoot)['gen_ai.usage.input_tokens'],undefined);
  evidence.scenarios.push({name:'provider-cooldown-retains-failed-prompt',exit:cooldown.code,passed:true});
  evidence.spans.push(...captured);
  const finalStatus=JSON.parse((await run(process.execPath,[cli,'status','openclaw','--json'],process.env,[0,2])).stdout);
  assert.equal(finalStatus.delivery.exported,evidence.spans.length-failureSpansStart);
  evidence.finalStatus=finalStatus;
  evidence.passed=true;
})().catch(error=>{evidence.error=String(error);process.exitCode=1;console.error(error);}).finally(async()=>{
  await fs.mkdir(evidenceDir,{recursive:true});await fs.writeFile(join(evidenceDir,'result.json'),JSON.stringify(evidence,null,2));
  console.log(JSON.stringify({...evidence,spans:undefined},null,2));server.closeAllConnections();server.close();
});
