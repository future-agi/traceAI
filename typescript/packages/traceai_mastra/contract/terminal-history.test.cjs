// Terminal-turn provenance: real tools, unmodified response.messages, loopback SDK and OTLP.
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
let spans=[],requests=[],native=[],snapshots=[],calls=0,invocations;
const defaultInvocations=()=>['explode','succeed'].map(name=>({id:`${name}-id`,name,args:{query:'SAFE_QUERY'}}));
const collector=http.createServer((r,s)=>{const chunks=[];r.on('data',c=>chunks.push(c));r.on('end',()=>{
 const data=wire.toObject(wire.decode(Buffer.concat(chunks)),{longs:String,bytes:String});
 for(const resource of data.resourceSpans||[])for(const scope of resource.scopeSpans||[])for(const span of scope.spans||[])spans.push(span);
 s.writeHead(200,{'content-type':'application/x-protobuf'});s.end();
});});
const provider=http.createServer((r,s)=>{let b='';r.on('data',c=>b+=c);r.on('end',()=>{
 const input=JSON.parse(b);requests.push(input);assert.equal(r.headers.authorization,'Bearer synthetic-verifier');
 const first=requests.length===1;
 const common={id:'verifier-response',created:1700000000,model:'gpt-4o-mini'};
 const tool_calls=invocations.map(({id,name,args},i)=>({index:i,id,type:'function',function:{name,arguments:JSON.stringify(args)}}));
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
function setup(flag, sharedObs, agentId='verifier', throwing=true, extraTools={}){
 spans=[];requests=[];native=[];snapshots=[];calls=0;invocations=defaultInvocations();for(const key of flags)delete process.env[key];if(flag){const [policy,explicitFalse]=flag.split(':');process.env[policy]='true';if(explicitFalse)process.env.FI_HIDE_ERROR_DETAILS='false';}
 const obs=sharedObs||createFIObservability({apiKey:'synthetic-fi',secretKey:'synthetic-fi-secret',serviceName:'independent-smoke',endpoint:`http://127.0.0.1:${collector.address().port}/v1/traces`});
 const exp=obs.getDefaultInstance().getExporters()[0];const send=exp.exportTracingEvent.bind(exp);
 exp.exportTracingEvent=e=>{if(e.type==='span_ended'){native.push(e.exportedSpan);snapshots.push(JSON.stringify(e.exportedSpan));}return send(e);};
 const options={description:'Synthetic tool',inputSchema:z.object({query:z.string()})};
 const tools={explode:createTool({...options,id:'explode',execute:async()=>{calls++;if(throwing)throw new Error(marker);return marker;}}),succeed:createTool({...options,id:'succeed',execute:async()=>{calls++;return {answer:'SAFE_TOOL_RESULT',error:'ordinary business field'};}})};
 const model=createOpenAI({apiKey:'synthetic-verifier',baseURL:`http://127.0.0.1:${provider.address().port}/v1`}).chat('gpt-4o-mini');
 const a=new Agent({id:agentId,name:agentId,instructions:'Synthetic tools',model,tools:{...tools,...extraTools}});
 const mastra=new Mastra({agents:{a},observability:obs});return {obs,agent:mastra.getAgent('a')};
}
function attrs(s){return Object.fromEntries((s.attributes||[]).map(a=>[a.key,Object.values(a.value)[0]]));}
function evidence(label,extra={}){
 const leaks=spans.flatMap(s=>Object.entries(attrs(s)).filter(([,v])=>JSON.stringify(v).includes(marker)).map(([key])=>({name:s.name,traceId:s.traceId,key})));
 if(process.env.MASTRA_EVIDENCE_DIR){fs.mkdirSync(process.env.MASTRA_EVIDENCE_DIR,{recursive:true});fs.writeFileSync(path.join(process.env.MASTRA_EVIDENCE_DIR,`terminal-${label}.json`),JSON.stringify({label,requests,native,spans,leaks,...extra},null,2));}return leaks;
}

for(const [method,stop] of [['generate','maxSteps=1'],['stream','maxSteps=1'],['generate','abort']])for(const flag of [null,'FI_HIDE_ERROR_DETAILS','FI_HIDE_OUTPUTS','FI_HIDE_OUTPUT_MESSAGES','FI_HIDE_OUTPUT_TEXT','FI_HIDE_ERROR_DETAILS:false','FI_HIDE_OUTPUTS:false','FI_HIDE_OUTPUT_MESSAGES:false','FI_HIDE_OUTPUT_TEXT:false']){
 const label=`${method}-${stop}-${flag||'default'}`;
 test(`terminal native response replay ${label}`,async()=>{
  const {obs,agent}=setup(flag);
  const controller=new AbortController();let stepCallbacks=0;
  try{
   const options=stop==='abort'?{maxSteps:3,abortSignal:controller.signal,onStepFinish:()=>{stepCallbacks++;controller.abort();}}:{maxSteps:1};
   const result=await agent[method]('SAFE_PROMPT',options);
   if(stop==='abort'){assert.equal(stepCallbacks,1);assert.equal(controller.signal.aborted,true);}
   if(method==='stream')for await(const part of result.textStream)assert.equal(part,'');
   await obs.getDefaultInstance().flush();
   const response=await result.response;
   assert.equal(requests.length,1,'no later request exists in originating trace');
   assert.equal(calls,2,'both actual tools completed');
   assert.ok(Array.isArray(response?.messages));
   assert.match(JSON.stringify(response.messages),new RegExp(marker));
   assert.match(JSON.stringify(response.messages),/SAFE_TOOL_RESULT/);
   const firstTraceIds=[...new Set(spans.map(s=>s.traceId))];
   const origin={requests:[...requests],spans:[...spans],native:[...native],response};
   spans=[];native=[];snapshots=[];
   const retained=JSON.stringify(response.messages);
   const replay=[{role:'user',content:'SAFE_PROMPT'},...response.messages,{role:'user',content:'SAFE_FOLLOWUP'}];
   assert.equal((await agent.generate(replay,{maxSteps:3})).text,'SAFE_FINAL');await obs.shutdown();
   assert.equal(JSON.stringify(response.messages),retained,'response.messages remains untouched');
   assert.equal(requests.length,2);assert.equal(calls,2,'no tools rerun');
   assert.match(JSON.stringify(requests[1].messages),new RegExp(marker));
   assert.ok(spans.length>0);assert.ok(spans.every(s=>!firstTraceIds.includes(s.traceId)));
   assert.match(JSON.stringify(spans),/SAFE_TOOL_RESULT/);
   assert.match(JSON.stringify(spans),/explode-id/);assert.match(JSON.stringify(spans),/succeed-id/);
   const counts=values=>Object.fromEntries([...new Set(values)].sort().map(v=>[v,values.filter(i=>i===v).length]));
   assert.deepEqual(counts(spans.map(s=>s.spanId)),counts(native.map(s=>Buffer.from(s.id,'hex').toString('base64'))));
   evidence(label,{origin,firstTraceIds});
   if(flag&&!flag.endsWith(':false'))assert.doesNotMatch(JSON.stringify(spans),new RegExp(marker));else assert.match(JSON.stringify(spans),new RegExp(marker));
  }finally{await obs.shutdown();for(const key of flags)delete process.env[key];}
 });
}

// An unrelated *successful* tool returns the exact same text and call ID. This
// is not the failed response being moved to an unrelated privacy context.
for(const boundary of ['exporter','agent','explicit-false-policy']){
 test(`same successful text and tool identity is independent across ${boundary}`,async()=>{
  const first=setup('FI_HIDE_ERROR_DETAILS');let second;
  try{
   const failed=await first.agent.generate('SAFE_PROMPT',{maxSteps:1});
   assert.match(JSON.stringify((await failed.response).messages),new RegExp(marker));
   await first.obs.getDefaultInstance().flush();
   const firstEvidence={requests:[...requests],spans:[...spans],native:[...native]};
   second=setup(boundary==='explicit-false-policy'?'FI_HIDE_OUTPUTS:false':'FI_HIDE_ERROR_DETAILS',boundary==='agent'?first.obs:undefined,boundary==='agent'?'other-agent':'verifier',false);
   const successful=await second.agent.generate('SAFE_PROMPT',{maxSteps:1});
   assert.equal(calls,2);assert.equal(requests.length,1);
   const response=await successful.response;
   assert.match(JSON.stringify(response.messages),new RegExp(marker));
   await second.obs.getDefaultInstance().flush();spans=[];native=[];snapshots=[];
   const retained=JSON.stringify(response.messages);
   assert.equal((await second.agent.generate([{role:'user',content:'SAFE_PROMPT'},...response.messages,{role:'user',content:'SAFE_FOLLOWUP'}],{maxSteps:3})).text,'SAFE_FINAL');
   await second.obs.getDefaultInstance().flush();
   assert.equal(requests.length,2);assert.equal(calls,2);
   assert.equal(JSON.stringify(response.messages),retained);
   assert.match(JSON.stringify(requests[1]),new RegExp(marker));
   evidence(`negative-${boundary}`,{firstEvidence});
   assert.match(JSON.stringify(spans),new RegExp(marker),'unrelated successful output must not be misclassified as a failure');
   assert.match(JSON.stringify(spans),/SAFE_TOOL_RESULT/);
  }finally{if(second)await second.obs.shutdown();await first.obs.shutdown();for(const key of flags)delete process.env[key];}
 });
}

// Parsing is deliberately non-injective: every invocation executes with the
// same parsed input, but one succeeds and two fail, in a different finish order.
// Neither original/parsed argument equality nor tool-name/order matching can
// identify these failures. Only the native tool-error lifecycle carries the ID.
const schemaCases = {
 default: () => z.object({query:z.string(),defaulted:z.string().default('SAFE_DEFAULT')}),
 coerce: () => z.object({query:z.coerce.number()}),
 transform: () => z.object({query:z.string().transform(value=>value.toUpperCase())}),
 strip: () => z.object({query:z.string()}),
};
function schemaTopology(){
 assert.deepEqual(native.map(span=>JSON.stringify(span)),snapshots,'native span data remains untouched');
 const b64=id=>Buffer.from(id,'hex').toString('base64');
 assert.deepEqual(spans.map(s=>s.spanId).sort(),native.map(s=>b64(s.id)).sort(),'one OTLP export per native completion');
 for(const span of spans){const source=native.find(s=>b64(s.id)===span.spanId);assert.equal(span.traceId,b64(source.traceId));assert.equal(span.parentSpanId||'',source.parentSpanId?b64(source.parentSpanId):'');}
}
function toolResults(value,found=[]){
 if(!value||typeof value!=='object')return found;
 if(value.role==='tool'&&typeof value.tool_call_id==='string')found.push({id:value.tool_call_id,value:value.content});
 if(value.type==='tool-result')found.push({id:value.toolCallId,value:value.output??value.result});
 if(value.type==='tool_call_response')found.push({id:value.id,value:value.response});
 for(const child of Object.values(value))toolResults(child,found);
 return found;
}
for(const [schemaName,schema] of Object.entries(schemaCases))for(const flag of [null,'FI_HIDE_ERROR_DETAILS','FI_HIDE_OUTPUTS','FI_HIDE_OUTPUT_MESSAGES','FI_HIDE_OUTPUT_TEXT','FI_HIDE_ERROR_DETAILS:false','FI_HIDE_OUTPUTS:false','FI_HIDE_OUTPUT_MESSAGES:false','FI_HIDE_OUTPUT_TEXT:false']){
 test(`parsed input preserves exact same-tool identity: ${schemaName} ${flag||'default'}`,async()=>{
  const secondMarker='SECOND_PRIVATE_FAILURE';const executed=[];
  const lookup=createTool({id:'lookup',description:'Synthetic parsed input',inputSchema:schema(),execute:async(input,context)=>{
   calls++;const id=context.agent.toolCallId;executed.push({id,input});
   await new Promise(resolve=>setTimeout(resolve,id==='failed-one'?15:id==='failed-two'?1:5));
   if(id==='failed-one')throw new Error(marker);
   if(id==='failed-two')throw new Error(secondMarker);
   return marker; // Same text as a failure, but this invocation SUCCEEDED.
  }});
  const {obs,agent}=setup(flag,undefined,'schema-agent',true,{lookup});
  const args=schemaName==='coerce'?{query:'42'}:schemaName==='transform'?{query:'safe_query'}:{query:'SAFE_QUERY'};
  invocations=['failed-one','safe-same-text','failed-two'].map((id,index)=>({id,name:'lookup',args:{...args,...(schemaName==='strip'?{discarded:index}:{})}}));
  try{
   const result=await agent.generate('SAFE_PROMPT',{maxSteps:1});await obs.flush();
   assert.equal(calls,3);assert.equal(requests.length,1);
   assert.deepEqual(new Set(executed.map(x=>x.id)),new Set(invocations.map(x=>x.id)));
   assert.deepEqual(executed[0].input,executed[1].input);assert.deepEqual(executed[0].input,executed[2].input);
   assert.notDeepEqual(executed[0].input,invocations[0].args,'native schema must actually change input');
   schemaTopology();
   const response=await result.response;const retained=JSON.stringify(response.messages);
   assert.match(retained,new RegExp(marker));assert.match(retained,new RegExp(secondMarker));
   const origin={requests:[...requests],spans:[...spans],native:[...native],response,executed};spans=[];native=[];snapshots=[];
   assert.equal((await agent.generate([{role:'user',content:'SAFE_PROMPT'},...response.messages,{role:'user',content:'FOLLOWUP'}],{maxSteps:3})).text,'SAFE_FINAL');
   await obs.shutdown();schemaTopology();assert.equal(JSON.stringify(response.messages),retained);assert.equal(calls,3);assert.equal(requests.length,2);
   const results=[];
   for(const span of spans)for(const [key,value]of Object.entries(attrs(span))){
    if(!/(?:input\.value|input\.messages|\.input)$/.test(key)||typeof value!=='string')continue;
    try{toolResults(JSON.parse(value),results);}catch{}
   }
   for(const id of invocations.map(x=>x.id))assert.ok(results.some(x=>x.id===id),`missing wire tool result ${id}`);
   const hiding=flag&&!flag.endsWith(':false');
   for(const entry of results){
    if(entry.id==='safe-same-text')assert.match(JSON.stringify(entry.value),new RegExp(marker),'safe identical text retained');
    if(entry.id==='failed-one'||entry.id==='failed-two'){
     const expected=entry.id==='failed-one'?marker:secondMarker;
     if(hiding)assert.doesNotMatch(JSON.stringify(entry.value),new RegExp(expected));else assert.match(JSON.stringify(entry.value),new RegExp(expected));
    }
   }
   if(hiding)assert.doesNotMatch(JSON.stringify(spans),new RegExp(secondMarker));
   evidence(`schema-${schemaName}-${flag||'default'}`,{origin,results});
  }finally{await obs.shutdown();for(const key of flags)delete process.env[key];}
 });
}
