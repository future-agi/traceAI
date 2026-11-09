// Retained real response.messages replay regression, using only loopback SDK/OTLP traffic.
const {test,before,after}=require('node:test');
const assert=require('node:assert/strict');
const http=require('node:http');
const fs=require('node:fs');
const path=require('node:path');
const {createRequire}=require('node:module');
const req=createRequire(path.join(process.env.MASTRA_TEST_DEPS,'package.json'));
const {Mastra}=req('@mastra/core');
const {Agent}=req('@mastra/core/agent');
const {createTool}=req('@mastra/core/tools');
const {z}=req('zod');
const {createOpenAI}=req('@ai-sdk/openai');
const {pathToFileURL}=require('node:url');
const packagePath=process.env.MASTRA_TEST_PACKAGE||path.resolve(__dirname,'../dist/src/index.js');
let createFIObservability;
const tr=createRequire(req.resolve('@opentelemetry/exporter-trace-otlp-proto'));
const wire=tr('@opentelemetry/otlp-transformer/build/src/generated/root.js').opentelemetry.proto.collector.trace.v1.ExportTraceServiceRequest;
const marker='VERIFIER_PRIVATE_THROWN_TOOL_TEXT';
let spans=[],requests=[],native=[],calls=0;
const collector=http.createServer((r,s)=>{const chunks=[];r.on('data',c=>chunks.push(c));r.on('end',()=>{
 const data=wire.toObject(wire.decode(Buffer.concat(chunks)),{longs:String,bytes:String});
 for(const resource of data.resourceSpans||[])for(const scope of resource.scopeSpans||[])for(const span of scope.spans||[])spans.push(span);
 s.writeHead(200,{'content-type':'application/x-protobuf'});s.end();
});});
const provider=http.createServer((r,s)=>{let b='';r.on('data',c=>b+=c);r.on('end',()=>{
 const input=JSON.parse(b);requests.push(input);assert.equal(r.headers.authorization,'Bearer synthetic-verifier');
 const first=requests.length===1;
 const common={id:'verifier-response',created:1700000000,model:'gpt-4o-mini'};
 const tool_calls=['explode','succeed'].map((name,i)=>({index:i,id:`${name}-id`,type:'function',function:{name,arguments:'{"query":"SAFE_QUERY"}'}}));
 const message=first?{role:'assistant',content:null,tool_calls}:{role:'assistant',content:'SAFE_FINAL'};
 const usage={prompt_tokens:7,completion_tokens:3,total_tokens:10};
 if(input.stream){
  s.writeHead(200,{'content-type':'text/event-stream'});
  const send=x=>s.write(`data: ${JSON.stringify({...common,object:'chat.completion.chunk',...x})}\n\n`);
  send({choices:[{index:0,delta:message,finish_reason:null}]});
  send({choices:[{index:0,delta:{},finish_reason:first?'tool_calls':'stop'}]});
  send({choices:[],usage});s.end('data: [DONE]\n\n');
 }else{
  s.writeHead(200,{'content-type':'application/json'});s.end(JSON.stringify({...common,object:'chat.completion',usage,choices:[{index:0,message,finish_reason:first?'tool_calls':'stop'}]}));
 }
});});
before(async()=>{({createFIObservability}=await import(pathToFileURL(packagePath).href));for(const s of [collector,provider])await new Promise(r=>s.listen(0,'127.0.0.1',r));});
after(async()=>{for(const s of [collector,provider]){s.closeAllConnections();await new Promise(r=>s.close(r));}});
const flags=['FI_HIDE_ERROR_DETAILS','FI_HIDE_INPUTS','FI_HIDE_OUTPUTS','FI_HIDE_INPUT_MESSAGES','FI_HIDE_OUTPUT_MESSAGES','FI_HIDE_INPUT_TEXT','FI_HIDE_OUTPUT_TEXT','FI_HIDE_INPUT_IMAGES'];
function setup(flag){
 spans=[];requests=[];native=[];calls=0;for(const key of flags)delete process.env[key];if(flag){const [policy,explicitFalse]=flag.split(':');process.env[policy]='true';if(explicitFalse)process.env.FI_HIDE_ERROR_DETAILS='false';}
 const obs=createFIObservability({apiKey:'synthetic-fi',secretKey:'synthetic-fi-secret',serviceName:'independent-smoke',endpoint:`http://127.0.0.1:${collector.address().port}/v1/traces`});
 const exp=obs.getDefaultInstance().getExporters()[0];const send=exp.exportTracingEvent.bind(exp);
 exp.exportTracingEvent=e=>{if(e.type==='span_ended')native.push(e.exportedSpan);return send(e);};
 const options={description:'Synthetic tool',inputSchema:z.object({query:z.string()})};
 const tools={explode:createTool({...options,id:'explode',execute:async()=>{calls++;throw new Error(marker);}}),succeed:createTool({...options,id:'succeed',execute:async()=>{calls++;return {answer:'SAFE_TOOL_RESULT',error:'ordinary business field'};}})};
 const model=createOpenAI({apiKey:'synthetic-verifier',baseURL:`http://127.0.0.1:${provider.address().port}/v1`}).chat('gpt-4o-mini');
 const a=new Agent({id:'verifier',name:'Verifier',instructions:'Synthetic tools',model,tools});
 const mastra=new Mastra({agents:{a},observability:obs});return {obs,agent:mastra.getAgent('a')};
}
function attrs(s){return Object.fromEntries((s.attributes||[]).map(a=>[a.key,Object.values(a.value)[0]]));}
function evidence(label,extra={}){
 const leaks=spans.flatMap(s=>Object.entries(attrs(s)).filter(([,v])=>JSON.stringify(v).includes(marker)).map(([key])=>({name:s.name,traceId:s.traceId,key})));
 if(process.env.MASTRA_EVIDENCE_DIR){fs.mkdirSync(process.env.MASTRA_EVIDENCE_DIR,{recursive:true});fs.writeFileSync(path.join(process.env.MASTRA_EVIDENCE_DIR,`continued-${label}.json`),JSON.stringify({label,requests,native,spans,leaks,...extra},null,2));}return leaks;
}
for(const flag of [null,'FI_HIDE_ERROR_DETAILS','FI_HIDE_OUTPUTS','FI_HIDE_OUTPUT_MESSAGES','FI_HIDE_OUTPUT_TEXT','FI_HIDE_ERROR_DETAILS:false','FI_HIDE_OUTPUTS:false','FI_HIDE_OUTPUT_MESSAGES:false','FI_HIDE_OUTPUT_TEXT:false']){
 const label=flag||'default';
 test(`streaming thrown tool keeps lifecycle and privacy ${label}`,async()=>{
  const {obs,agent}=setup(flag);
  try{
   const result=await agent.stream('SAFE_PROMPT',{maxSteps:3});let text='';for await(const part of result.textStream)text+=part;
   assert.equal(text,'SAFE_FINAL');await obs.shutdown();
   assert.equal(requests.length,2);assert.equal(calls,2);assert.match(JSON.stringify(requests[1]),new RegExp(marker));
   assert.match(JSON.stringify(native),new RegExp(marker));
   const counts=values=>Object.fromEntries([...new Set(values)].sort().map(v=>[v,values.filter(i=>i===v).length]));
   assert.deepEqual(counts(spans.map(s=>s.spanId)),counts(native.map(s=>Buffer.from(s.id,'hex').toString('base64'))));
   assert.ok(spans.some(s=>attrs(s)['mastra.span.type']==='tool_call'&&s.status?.code===2));
   assert.match(JSON.stringify(spans),/SAFE_TOOL_RESULT/);
   evidence(`stream-${label}`);
   if(flag&&!flag.endsWith(':false'))assert.doesNotMatch(JSON.stringify(spans),new RegExp(marker));else assert.match(JSON.stringify(spans),new RegExp(marker));
  }finally{await obs.shutdown();for(const key of flags)delete process.env[key];}
 });
 test(`carried native tool failure history on a later Agent turn ${label}`,async()=>{
  const {obs,agent}=setup(flag);
  try{
   const result=await agent.generate('SAFE_PROMPT',{maxSteps:3});assert.equal(result.text,'SAFE_FINAL');
   await obs.getDefaultInstance().flush();
   const response=await result.response;
   const retainedResponse=JSON.stringify(response.messages);
   assert.ok(Array.isArray(response?.messages),'generate returns native response.messages for continued conversations');
   assert.match(JSON.stringify(response.messages),new RegExp(marker),'carry real thrown error, not manufactured error history');
   const firstTraceIds=[...new Set(spans.map(s=>s.traceId))];spans=[];native=[];
   const replay=[{role:'user',content:'SAFE_PROMPT'},...response.messages,{role:'user',content:'SAFE_FOLLOWUP'}];
   assert.equal((await agent.generate(replay,{maxSteps:3})).text,'SAFE_FINAL');await obs.shutdown();
   assert.equal(JSON.stringify(response.messages),retainedResponse,'response.messages remains untouched');
   assert.equal(requests.length,3);assert.equal(calls,2,'no tool reruns on followup');
   assert.match(JSON.stringify(requests[2].messages),new RegExp(marker));
   assert.ok(spans.length>0);assert.ok(spans.every(s=>!firstTraceIds.includes(s.traceId)),'followup has fresh trace');
   assert.match(JSON.stringify(spans),/SAFE_TOOL_RESULT/);
   assert.match(JSON.stringify(spans),/explode-id|succeed-id/,'tool identity survives');
   const leaks=evidence(`followup-${label}`,{firstTraceIds});
   console.log('FOLLOWUP',JSON.stringify({label,leaks}));
   if(flag&&!flag.endsWith(':false'))assert.doesNotMatch(JSON.stringify(spans),new RegExp(marker));else assert.match(JSON.stringify(spans),new RegExp(marker));
  }finally{await obs.shutdown();for(const key of flags)delete process.env[key];}
 });
}
