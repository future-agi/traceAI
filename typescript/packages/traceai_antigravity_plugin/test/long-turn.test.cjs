const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs/promises');
const os=require('node:os');
const path=require('node:path');
const http=require('node:http');
const {spawn}=require('node:child_process');
const core=require('@traceai/agent-core');
const {antigravityAdapter}=require('../dist');
const attrs=s=>Object.fromEntries(s.attributes.map(a=>[a.key,Object.values(a.value)[0]]));
for (const toolCount of [4,20]) test(`real agy normal ${toolCount}-file read turn`,{timeout:150000,skip:process.env.AGY_TEST_GENERATION !== '1'},async t=>{
 const home=await fs.mkdtemp(path.join(os.tmpdir(),'fi-agy-review-tools-'));
 const project=path.join(home,'project');await fs.mkdir(project,{mode:0o700});
 for(let i=1;i<=toolCount;i++) await fs.writeFile(path.join(project,`file-${i}.txt`),`Synthetic module ${i}.\n`);
 const exports=[],requests=[];let count=0;
 const server=http.createServer((req,res)=>{const chunks=[];req.on('data',b=>chunks.push(b));req.on('end',()=>{
  const body=JSON.parse(Buffer.concat(chunks).toString()||'{}');res.setHeader('Content-Type','application/json');
  if(req.url==='/tracer/v1/traces'){exports.push(body);res.end('{}');return;}
  if(!req.url.includes('GenerateContent')){res.statusCode=404;res.end('{}');return;}
  count++;requests.push(body);
  const functions=(body.tools||[]).flatMap(t=>t.functionDeclarations||t.function_declarations||[]);
  const tool=functions.find(f=>f.name==='read_file')||functions.find(f=>f.name==='view_file');
  assert.ok(tool,'native read tool exists');
  const filename=path.join(project,`file-${count}.txt`);
  const parts=count<=toolCount?[{functionCall:{name:tool.name,args:tool.name==='read_file'?{Path:filename}:{AbsolutePath:filename}}}]:[{text:'All requested files read successfully.'}];
  const response={candidates:[{index:0,content:{role:'model',parts},finishReason:'STOP'}],usageMetadata:{promptTokenCount:20,candidatesTokenCount:7,thoughtsTokenCount:3,totalTokenCount:30},modelVersion:'gemini-4-argon-medium'};
  const sse=req.url.includes('alt=sse');res.setHeader('Content-Type',sse?'text/event-stream':'application/json');res.end(sse?'data: '+JSON.stringify(response)+'\n\n':JSON.stringify([response]));
 });});
 await new Promise(r=>server.listen(0,'127.0.0.1',r));
 const base=`http://127.0.0.1:${server.address().port}`,options={agent:'antigravity',home,env:{}};
 let result,summary;
 t.after(async()=>{
  const evidence=process.env.AGY_EVIDENCE_DIR||'/work/reviewer-evidence';await fs.mkdir(evidence,{recursive:true});
  await fs.writeFile(path.join(evidence,`tools-${toolCount}.json`),JSON.stringify({summary,result,exports,requestCount:requests.length},null,2));
  const brain=path.join(home,'.gemini/antigravity-cli/brain');
  for(const id of await fs.readdir(brain).catch(()=>[])) await fs.copyFile(path.join(brain,id,'.system_generated/logs/transcript_full.jsonl'),path.join(evidence,`tools-${toolCount}-${id}.jsonl`)).catch(()=>{});
  server.closeAllConnections();await new Promise(r=>server.close(r));await fs.rm(home,{recursive:true,force:true});
 });
 assert.equal((await core.setup({...options,credentials:{apiKey:'synthetic-api',secretKey:'synthetic-secret'},projectName:'test',otlpEndpoint:base+'/tracer/v1/traces',adapter:antigravityAdapter})).registered,true);
 result=await new Promise((resolve,reject)=>{
  const child=spawn('agy',['--settings',JSON.stringify({modelProvider:'gemini'}),'--model','Gemini 4 Argon (Medium)','--print-timeout','110s','--output-format','stream-json','-p',`Read file-1.txt through file-${toolCount}.txt and summarize the modules.`],{cwd:project,env:{HOME:home,PATH:process.env.PATH,GEMINI_API_KEY:'synthetic-key',GOOGLE_GEMINI_BASE_URL:base,NO_COLOR:'1'},stdio:['ignore','pipe','pipe']});
  let stdout='',stderr='';child.stdout.on('data',b=>stdout+=b);child.stderr.on('data',b=>stderr+=b);child.on('error',reject);
  const timer=setTimeout(()=>child.kill('SIGKILL'),120000);child.on('close',(code,signal)=>{clearTimeout(timer);resolve({code,signal,stdout,stderr})});
 });
 const spans=exports.flatMap(b=>(b.resourceSpans||[]).flatMap(r=>r.scopeSpans.flatMap(s=>s.spans)));
 const roots=spans.filter(s=>!s.parentSpanId),llms=spans.filter(s=>attrs(s)['fi.span.kind']==='LLM'),tools=spans.filter(s=>attrs(s)['fi.span.kind']==='TOOL');
 const terminal=result.stdout.trim().split('\n').map(x=>{try{return JSON.parse(x)}catch{return {}}}).find(r=>r.event==='result')?.result;
 summary={toolCount,requestCount:requests.length,hostExitCode:result.code,hostSignal:result.signal,hostTerminal:terminal,roots:roots.length,generations:llms.length,tools:tools.length,outcomes:roots.map(r=>attrs(r)['fi.agent.outcome']),inputTokens:llms.reduce((n,s)=>n+Number(attrs(s)['gen_ai.usage.input_tokens']||0),0),outputTokens:llms.reduce((n,s)=>n+Number(attrs(s)['gen_ai.usage.output_tokens']||0),0),status:await core.inspectStatus({...options,adapter:antigravityAdapter})};
 console.log('NORMAL_TOOLS_REVIEW',JSON.stringify(summary));
 assert.equal(result.code,0);assert.equal(result.signal,null);assert.equal(requests.length,toolCount+1);
 assert.equal(roots.length,1);assert.equal(llms.length,toolCount+1,'one generation per completed provider response');
 assert.equal(tools.length,toolCount);assert.equal(summary.inputTokens,20*(toolCount+1));assert.equal(summary.outputTokens,10*(toolCount+1));
 assert.equal(summary.outcomes[0],'success');
});
