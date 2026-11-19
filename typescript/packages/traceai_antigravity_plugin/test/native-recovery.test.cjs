const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs/promises');
const path=require('node:path');
const os=require('node:os');
const http=require('node:http');
const {spawn}=require('node:child_process');
const core=require('@traceai/agent-core');
const {antigravityAdapter}=require(process.env.AGY_PACKAGE_PATH||'../dist');
const attrs=s=>Object.fromEntries(s.attributes.map(a=>[a.key,Object.values(a.value)[0]]));
const pause=ms=>new Promise(r=>setTimeout(r,ms));
for(const scenario of ['sigint','subagent','subagent-delayed','denied'])test(`real agy ${scenario}: terminal recovery and originating trace ownership`,{skip:process.env.AGY_TEST_GENERATION!=='1',timeout:60000},async t=>{
 // e2b.sh runs this with only loopback networking. No paid model or real credentials.
 const home=await fs.mkdtemp(path.join(os.tmpdir(),'fi-agy-native-recovery-')),project=path.join(home,'project');
 await fs.mkdir(project,{mode:0o700});await fs.writeFile(path.join(project,'README.md'),'Synthetic file.');
 const exports=[],requests=[];let child,parentCount=0,childCount=0,signalSent=false;
 const server=http.createServer((req,res)=>{const chunks=[];req.on('data',b=>chunks.push(b));req.on('end',()=>{
   const body=JSON.parse(Buffer.concat(chunks).toString()||'{}');
   if(req.url==='/tracer/v1/traces'){exports.push(body);res.setHeader('Content-Type','application/json');res.end('{}');return;}
   if(!req.url.includes('GenerateContent')){res.statusCode=404;res.end('{}');return;}
   const reviewer=!body.systemInstruction,isChild=!reviewer&&JSON.stringify(body.systemInstruction).includes('CHILD_SYSTEM_SENTINEL');
   requests.push({reviewer,child:isChild});if(isChild)childCount++;else if(!reviewer)parentCount++;
   if(scenario==='sigint'&&!reviewer&&parentCount===2){signalSent=true;setTimeout(()=>child.kill('SIGINT'),500);return;}
   let parts;
   if(reviewer)parts=[{text:JSON.stringify({reason:'Synthetic local fixture.',severity:0,confidence:10})}];
   else if(isChild)parts=[{text:'Synthetic child completed.'}];
   else if(scenario==='sigint'){
     const funcs=(body.tools||[]).flatMap(t=>t.functionDeclarations||t.function_declarations||[]),tool=funcs.find(f=>f.name==='read_file')||funcs.find(f=>f.name==='view_file');
     assert.ok(tool);parts=[{functionCall:{name:tool.name,args:tool.name==='read_file'?{Path:path.join(project,'README.md')}:{AbsolutePath:path.join(project,'README.md')}}}];
   }else if(parentCount===1)parts=[{functionCall:{name:'define_subagent',args:{Name:'test-helper',Description:'Synthetic helper',SystemPrompt:'CHILD_SYSTEM_SENTINEL. Reply briefly.',EnableWriteTools:false,EnableSubagentTools:false,EnableMCPTools:false}}}];
   else if(parentCount===2)parts=[{functionCall:{name:'invoke_subagent',args:{TypeName:'test-helper',Role:'Synthetic helper',Prompt:'Return a synthetic result.',Model:'inherit'}}}];
   else if(scenario==='denied')parts=[{functionCall:{name:'run_command',args:{CommandLine:'sleep 2',toolSummary:'Synthetic wait',WaitMsBeforeAsync:1000}}}];
   else parts=[{text:'Synthetic parent completed.'}];
   const response={candidates:[{index:0,content:{role:'model',parts},finishReason:'STOP'}],usageMetadata:{promptTokenCount:20,candidatesTokenCount:7,thoughtsTokenCount:3,totalTokenCount:30},modelVersion:'gemini-4-argon-medium'};
   const sse=req.url.includes('alt=sse');res.setHeader('Content-Type',sse?'text/event-stream':'application/json');
   const send=()=>res.end(sse?'data: '+JSON.stringify(response)+'\n\n':JSON.stringify([response]));
   if(scenario==='subagent-delayed'&&isChild)setTimeout(send,10000);
   else if(scenario!=='subagent-delayed'&&!reviewer&&!isChild&&parentCount>=3)setTimeout(send,1500);else send();
 })});
 await new Promise(r=>server.listen(0,'127.0.0.1',r));const base=`http://127.0.0.1:${server.address().port}`;
 const options={agent:'antigravity',home,env:{}};
 let result;
 const spans=()=>exports.flatMap(b=>(b.resourceSpans??[]).flatMap(r=>r.scopeSpans.flatMap(s=>s.spans)));
 t.after(async()=>{
   if(process.env.AGY_EVIDENCE_DIR){await fs.mkdir(process.env.AGY_EVIDENCE_DIR,{recursive:true});await fs.writeFile(path.join(process.env.AGY_EVIDENCE_DIR,scenario+'.json'),JSON.stringify({scenario,parentCount,childCount,signalSent,result,requests,spans:spans()},null,2));}
   server.closeAllConnections();await new Promise(r=>server.close(r));await fs.rm(home,{recursive:true,force:true});
 });
 assert.equal((await core.setup({...options,credentials:{apiKey:'synthetic-api',secretKey:'synthetic-secret'},projectName:'test',otlpEndpoint:base+'/tracer/v1/traces',adapter:antigravityAdapter})).registered,true);
 result=await new Promise((resolve,reject)=>{
   child=spawn('agy',['--settings',JSON.stringify({modelProvider:'gemini'}),'--model','Gemini 4 Argon (Medium)','--print-timeout','25s','--output-format','stream-json','-p',scenario==='sigint'?'Read README.md then answer.':'Use a helper to return a synthetic result.'],{cwd:project,env:{HOME:home,PATH:process.env.PATH,GEMINI_API_KEY:'synthetic-key',GOOGLE_GEMINI_BASE_URL:base,NO_COLOR:'1'},stdio:['ignore','pipe','pipe']});
   let stdout='',stderr='';child.stdout.on('data',b=>stdout+=b);child.stderr.on('data',b=>stderr+=b);child.on('error',reject);
   const timer=setTimeout(()=>child.kill('SIGKILL'),40000);child.on('close',(code,signal)=>{clearTimeout(timer);resolve({code,signal,stdout,stderr})});
 });
 assert.equal(result.signal,null);assert.equal(result.code,scenario==='sigint'?1:0,result.stderr);
 const records=result.stdout.trim().split('\n').map(JSON.parse),terminal=records.find(r=>r.event==='result')?.result;
 if(scenario==='sigint'){assert.ok(signalSent);assert.equal(terminal.finished_reason,'interrupted');}
 if(scenario==='denied')assert.ok(terminal.denied_actions?.length,'native host must actually deny a command');
 if(scenario!=='sigint')assert.ok(childCount>0,'native subagent must actually make a model request');
 const deadline=Date.now()+12000;while(!spans().some(s=>!s.parentSpanId)&&Date.now()<deadline)await pause(100);
 const all=spans(),roots=all.filter(s=>!s.parentSpanId),llms=all.filter(s=>attrs(s)['fi.span.kind']==='LLM');
 assert.equal(roots.length,1,JSON.stringify({scenario,spans:all.map(s=>({name:s.name,attrs:attrs(s)}))}));
 assert.equal(new Set(all.map(s=>s.traceId)).size,1);
 assert.ok(all.every(s=>attrs(s)['session.id']===attrs(roots[0])['session.id']));
 const expected=requests.filter(r=>!r.reviewer).length-(scenario==='sigint'?1:0);
 if(scenario==='subagent-delayed'){assert.equal(terminal.num_turns,1);assert.equal(expected,5,'one user prompt plus delayed system continuation');}
 assert.equal(llms.length,expected,'each completed planner response is exported once');
 assert.equal(llms.reduce((n,s)=>n+Number(attrs(s)['gen_ai.usage.input_tokens']),0),20*expected);
 assert.equal(llms.reduce((n,s)=>n+Number(attrs(s)['gen_ai.usage.output_tokens']),0),10*expected);
 assert.equal(new Set(all.map(s=>s.spanId)).size,all.length);
 if(scenario.startsWith('subagent'))assert.equal(attrs(roots[0])['fi.agent.outcome'],'success');
 else assert.equal(attrs(roots[0])['fi.agent.outcome'],'incomplete','absent native Stop must not invent a successful outcome');
 if(scenario!=='sigint'){
   const tool=all.find(s=>s.name==='invoke_subagent'),agent=all.find(s=>s.name==='subagent');assert.ok(tool&&agent);assert.equal(agent.parentSpanId,tool.spanId);
   const generation=llms.find(s=>attrs(s)['output.value']==='Synthetic child completed.');assert.ok(generation);
   const invocation=all.find(s=>s.spanId===generation.parentSpanId);assert.equal(invocation.parentSpanId,agent.spanId);
 }
 assert.ok(all.every(s=>!s.parentSpanId||all.some(p=>p.spanId===s.parentSpanId&&p.traceId===s.traceId)));
 const status=await core.inspectStatus({...options,adapter:antigravityAdapter});assert.equal(status.pendingTurns,0);
 await pause(500);assert.equal(spans().length,all.length,'exit observer and Stop must not export twice');
 console.log('NATIVE_RECOVERY',JSON.stringify({scenario,roots:roots.length,generations:llms.length,input:20*expected,output:10*expected,pending:status.pendingTurns}));
});
