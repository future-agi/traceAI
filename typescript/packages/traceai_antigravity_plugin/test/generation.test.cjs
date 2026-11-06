const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const os = require('node:os');
const http = require('node:http');
const {spawn} = require('node:child_process');
const core = require('@traceai/agent-core');
const {antigravityAdapter} = require(process.env.AGY_PACKAGE_PATH || '../dist');
const attrs=s=>Object.fromEntries(s.attributes.map(a=>[a.key,Object.values(a.value)[0]]));
function run(args,env,cwd){return new Promise((resolve,reject)=>{
  const child=spawn('agy',args,{env,cwd,stdio:['ignore','pipe','pipe']});let stdout='',stderr='';
  child.stdout.on('data',b=>stdout+=b);child.stderr.on('data',b=>stderr+=b);child.on('error',reject);
  const timeout=setTimeout(()=>child.kill('SIGKILL'),30000);
  child.on('close',(code,signal)=>{clearTimeout(timeout);resolve({code,signal,stdout,stderr})});
});}
test('real agy native plugin: two turns, Gemini requests, successful/error tool and terminal flush',
  {skip:process.env.AGY_TEST_GENERATION!=='1'},async t=>{
  // Runner invokes this inside unshare --net with ONLY loopback up; network egress is impossible.
  const home=await fs.mkdtemp(path.join(os.tmpdir(),'fi-agy-real-'));
  const project=path.join(home,'project');await fs.mkdir(project,{mode:0o700});
  await fs.writeFile(path.join(project,'README.md'),'Synthetic fixture repository.\n');
  const modelRequests=[],exports=[],runs=[];
  let count=0;
  const server=http.createServer((req,res)=>{const chunks=[];req.on('data',b=>chunks.push(b));req.on('end',()=>{
    let body;try{body=JSON.parse(Buffer.concat(chunks).toString()||'{}')}catch{res.statusCode=400;res.end('{}');return;}
    if(req.url==='/tracer/v1/traces'){
      assert.equal(req.headers['x-api-key'],'synthetic-api');assert.equal(req.headers['x-secret-key'],'synthetic-secret');
      assert.equal(req.headers['x-project-name'],'test');exports.push(body);res.setHeader('Content-Type','application/json');res.end('{}');return;
    }
    if(!req.url.includes('GenerateContent')){res.statusCode=404;res.end('{}');return;}
    modelRequests.push({path:req.url,body});count++;
    const functions=(body.tools||[]).flatMap(t=>t.functionDeclarations||t.function_declarations||[]);
    const tool=functions.find(f=>f.name==='read_file')||functions.find(f=>/read_file|view_file/.test(f.name));
    const requestedPath=path.join(project,count===1?'README.md':'missing.txt');
    const args=tool?.name==='read_file'?{Path:requestedPath}:{AbsolutePath:requestedPath};
    const parts=(count===1||count===3)&&tool?[{functionCall:{name:tool.name,args}}]:[{text:`Synthetic answer ${count}.`}];
    const response={candidates:[{index:0,content:{role:'model',parts},finishReason:'STOP'}],
      usageMetadata:{promptTokenCount:20*count,candidatesTokenCount:7,thoughtsTokenCount:3,totalTokenCount:20*count+10},modelVersion:'gemini-4-argon-medium'};
    const sse=req.url.includes('alt=sse');res.setHeader('Content-Type',sse?'text/event-stream':'application/json');
    res.end(sse?'data: '+JSON.stringify(response)+'\n\n':JSON.stringify([response]));
  });});
  await new Promise(r=>server.listen(0,'127.0.0.1',r));
  const base=`http://127.0.0.1:${server.address().port}`;
  const options={agent:'antigravity',home,env:{}};
  t.after(async()=>{
    if(process.env.AGY_EVIDENCE_DIR){
      await fs.mkdir(process.env.AGY_EVIDENCE_DIR,{recursive:true});
      await fs.writeFile(path.join(process.env.AGY_EVIDENCE_DIR,'generation.json'),JSON.stringify({modelRequests,exports,runs},null,2));
      // Synthetic transcripts only; no auth caches, settings, or other home state.
      const brain=path.join(home,'.gemini/antigravity-cli/brain');
      for(const id of await fs.readdir(brain).catch(()=>[])){
        const source=path.join(brain,id,'.system_generated/logs/transcript_full.jsonl');
        try{await fs.copyFile(source,path.join(process.env.AGY_EVIDENCE_DIR,id+'.jsonl'))}catch{}
      }
    }
    server.closeAllConnections();await new Promise(r=>server.close(r));await fs.rm(home,{recursive:true,force:true});
  });
  if (process.env.AGY_CLI_PATH) {
    const hooks=path.join(project,'.agents/hooks.json');
    await fs.mkdir(path.dirname(hooks),{mode:0o700});
    await fs.writeFile(hooks,JSON.stringify({legacy:'fi-hook-to-otlp'}));
    const duplicate=await new Promise((resolve,reject)=>{
      require('node:child_process').execFile(process.execPath,[process.env.AGY_CLI_PATH,'setup','antigravity','--project','test','--json'],
        {cwd:project,env:{HOME:home,PATH:process.env.PATH,FI_API_KEY:'synthetic-api',FI_SECRET_KEY:'synthetic-secret',FI_OTLP_ENDPOINT:base+'/tracer/v1/traces'},timeout:20000},
        (error,stdout)=>{try{resolve({code:error?.code??0,result:JSON.parse(stdout)})}catch(e){reject(e)}});
    });
    assert.equal(duplicate.code,2);
    assert.ok(duplicate.result.diagnostics.some(d=>d.code==='REMOVE_LEGACY_FUTUREAGI_HOOKS'));
    const list=await run(['plugin','list','--json'],{HOME:home,PATH:process.env.PATH},project);
    assert.ok(!JSON.parse(list.stdout).some(p=>p.name==='futureagi-tracing'));
    await fs.unlink(hooks);
    for (let i=0;i<2;i++) {
      const result = await new Promise((resolve,reject) => {
        const {execFile}=require('node:child_process');
        execFile(process.execPath,[process.env.AGY_CLI_PATH,'setup','antigravity','--project','test','--json'],
          {cwd:project,env:{HOME:home,PATH:process.env.PATH,FI_API_KEY:'synthetic-api',FI_SECRET_KEY:'synthetic-secret',FI_OTLP_ENDPOINT:base+'/tracer/v1/traces'},timeout:20000},
          (error,stdout)=>error?reject(error):resolve(JSON.parse(stdout)));
      });
      assert.equal(result.registered,true,JSON.stringify(result));
    }
  } else {
    const setup=await core.setup({...options,credentials:{apiKey:'synthetic-api',secretKey:'synthetic-secret'},projectName:'test',
      otlpEndpoint:base+'/tracer/v1/traces',adapter:antigravityAdapter});
    assert.equal(setup.registered,true,JSON.stringify(setup));
  }
  const env={HOME:home,PATH:process.env.PATH,GEMINI_API_KEY:'synthetic-gemini-key',GOOGLE_GEMINI_BASE_URL:base,NO_COLOR:'1'};
  const flags=['--settings',JSON.stringify({modelProvider:'gemini'}),'--model','Gemini 4 Argon (Medium)',
    '--print-timeout','20s','--output-format','stream-json'];
  const first=await run([...flags,'-p','Read README.md and summarize it.'],env,project);runs.push(first);
  assert.equal(first.code,0,first.stderr);assert.equal(first.signal,null);
  const conversation=first.stdout.trim().split('\n').map(x=>JSON.parse(x)).find(x=>x.event==='init').conversation_id;
  const second=await run([...flags,'--conversation',conversation,'-p','Read missing.txt and report the error.'],env,project);runs.push(second);
  assert.equal(second.code,0,second.stderr);assert.equal(second.signal,null);
  const spans=exports.flatMap(r=>(r.resourceSpans??[]).flatMap(rs=>rs.scopeSpans.flatMap(s=>s.spans)));
  const roots=spans.filter(s=>!s.parentSpanId), llms=spans.filter(s=>attrs(s)['fi.span.kind']==='LLM'), tools=spans.filter(s=>attrs(s)['fi.span.kind']==='TOOL');
  assert.equal(modelRequests.length,4);assert.equal(roots.length,2);
  assert.equal(llms.length,4,JSON.stringify(spans.map(s=>({name:s.name,attrs:attrs(s)}))));assert.equal(tools.length,2);
  assert.equal(tools.filter(s=>s.status.code===2).length,1);
  assert.equal(attrs(roots[0])['session.id'],attrs(roots[1])['session.id']);assert.notEqual(roots[0].traceId,roots[1].traceId);
  assert.ok(llms.every(s=>attrs(s)['fi.agent.fidelity']==='reconstructed-message'));
  assert.ok(llms.every(s=>attrs(s)['fi.agent.timing']==='reconstructed'));
  assert.ok(llms.every(s=>attrs(s)['input.value']&&attrs(s)['output.value']));
  assert.equal(llms.reduce((n,s)=>n+Number(attrs(s)['gen_ai.usage.input_tokens']),0),200);
  assert.equal(llms.reduce((n,s)=>n+Number(attrs(s)['gen_ai.usage.output_tokens']),0),40);
  assert.ok(roots.every(s=>attrs(s)['gen_ai.usage.total_tokens']===undefined));
  assert.ok(spans.every(s=>!s.parentSpanId||spans.some(p=>p.spanId===s.parentSpanId&&p.traceId===s.traceId)));
  const status=await core.inspectStatus({...options,adapter:antigravityAdapter});
  assert.equal(status.registration,'REGISTERED');assert.equal(status.delivery.exported,spans.length);
  await core.disable(options);
  const third=await run([...flags,'--conversation',conversation,'-p','Say disabled.'],env,project);runs.push(third);assert.equal(third.code,0);
  const after=exports.flatMap(r=>(r.resourceSpans??[]).flatMap(rs=>rs.scopeSpans.flatMap(s=>s.spans)));
  assert.equal(after.length,spans.length);
  console.log('REAL_GEMINI_OTLP',JSON.stringify({version:'1.5.2',turns:roots.length,generations:llms.length,tools:tools.length,
    spans:spans.length,inputTokens:200,outputTokens:40,disabledNoExport:true,externalNetwork:'unshare-net-loopback-only'}));
});
