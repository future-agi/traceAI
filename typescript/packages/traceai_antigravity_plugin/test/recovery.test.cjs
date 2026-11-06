const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs/promises');
const path=require('node:path');
const os=require('node:os');
const http=require('node:http');
const {spawn}=require('node:child_process');
const core=require('@traceai/agent-core');
const {handleEvent,unseal,pendingTurns}=require('../dist/runtime');
const {text}=require('../dist/mapper');
const attrs=s=>Object.fromEntries(s.attributes.map(a=>[a.key,Object.values(a.value)[0]]));
const pause=ms=>new Promise(r=>setTimeout(r,ms));
async function fixture(t){
 const home=await fs.mkdtemp(path.join(os.tmpdir(),'fi-agy-recovery-')),exports=[];
 const server=http.createServer((req,res)=>{const chunks=[];req.on('data',b=>chunks.push(b));req.on('end',()=>{exports.push(JSON.parse(Buffer.concat(chunks).toString()));res.setHeader('Content-Type','application/json');res.end('{}')})});
 await new Promise(r=>server.listen(0,'127.0.0.1',r));
 t.after(async()=>{server.closeAllConnections();await new Promise(r=>server.close(r));await fs.rm(home,{recursive:true,force:true})});
 const options={agent:'antigravity',home,env:{}};
 await core.setup({...options,configureOnly:true,credentials:{apiKey:'synthetic-api',secretKey:'synthetic-secret'},projectName:'test',otlpEndpoint:`http://127.0.0.1:${server.address().port}/tracer/v1/traces`});
 const config=await core.resolveConfig(options),dir=path.join(config.paths.stateDir,'antigravity-conversations');
 const payload=id=>({conversationId:id,transcriptPath:path.join(home,'.gemini/antigravity-cli/brain',id,'.system_generated/logs/transcript_full.jsonl'),initialNumSteps:1,invocationNum:0,lastUserInput:'originating prompt',modelName:'gemini-test'});
 const send=(event,p,extra={})=>handleEvent(event,p,{...options,...extra});
 const spans=()=>exports.flatMap(b=>(b.resourceSpans??[]).flatMap(r=>r.scopeSpans.flatMap(s=>s.spans)));
 const rows=async(p,data)=>{await fs.mkdir(path.dirname(p.transcriptPath),{recursive:true,mode:0o700});await fs.writeFile(p.transcriptPath,data.map(r=>JSON.stringify(r)).join('\n')+(data.length?'\n':''))};
 return {home,config,options,dir,payload,send,spans,rows};
}
const response=(step,content='completed')=>({step_index:step,status:'DONE',source:'MODEL',type:'PLANNER_RESPONSE',content,input_tokens:17,output_tokens:3});
for(const [reason,outcome] of [['USER_CANCELED','interrupted'],['ERROR','error']])test(`late completed response survives ${outcome} and repeated terminal hooks`,async t=>{
 const f=await fixture(t),p=f.payload('late');await f.rows(p,[]);
 await f.send('PreInvocation',p);await f.send('PostInvocation',p);
 const terminal=f.send('Stop',{...p,fullyIdle:true,terminationReason:reason});
 await pause(300);await fs.appendFile(p.transcriptPath,JSON.stringify(response(1))+'\n');await terminal;
 await f.send('Stop',{...p,fullyIdle:true,terminationReason:reason});
 const spans=f.spans(),llms=spans.filter(s=>attrs(s)['fi.span.kind']==='LLM');assert.equal(llms.length,1);
 assert.equal(attrs(llms[0])['gen_ai.usage.input_tokens'],17);assert.equal(attrs(spans.find(s=>!s.parentSpanId))['fi.agent.outcome'],outcome);
});
test('secret aliases and nested escaped password fragments never reach checkpoint or OTLP',async t=>{
 const f=await fixture(t),p=f.payload('privacy');await f.rows(p,[response(1)]);
 const fragments=['aws-sensitive-fragment','private-key-sensitive-fragment','password-prefix-fragment','password-suffix-fragment'];
 const secret=fragments[2]+'"\\\\'+fragments[3];
 const values=[];let encoded=JSON.stringify({password:secret,ordinary:'kept'});
 for(let depth=0;depth<4;depth++){values.push('log prefix '+encoded+' suffix');encoded=JSON.stringify(encoded)}
 const input={AWS_SECRET_ACCESS_KEY:fragments[0],nested:[{private_key:fragments[1],public_key:'public-kept'}],json:JSON.stringify({aws_secret_access_key:fragments[0],privateKey:fragments[1]}),logs:values,safe:'kept'};
 for(const v of values)for(const fragment of fragments)assert.ok(!text(v,f.config).includes(fragment));
 await f.send('PreInvocation',p);await f.send('PostToolUse',{...p,stepIdx:2,toolCall:{name:'fixture',args:input},result:input});
 const saved=(await fs.readdir(f.dir)).find(n=>n.endsWith('.json'));
 const checkpoint=JSON.stringify(unseal(await fs.readFile(path.join(f.dir,saved),'utf8'),f.config));
 for(const fragment of fragments)assert.ok(!checkpoint.includes(fragment),'checkpoint leaked a synthetic secret fragment');
 assert.ok(checkpoint.includes('public-kept'));assert.ok(checkpoint.includes('kept'));
 await f.send('PostInvocation',p);await f.send('Stop',{...p,fullyIdle:true,terminationReason:'NO_TOOL_CALL'});
 for(const fragment of fragments)assert.ok(!JSON.stringify(f.spans()).includes(fragment),'OTLP leaked a synthetic secret fragment');
 assert.ok(JSON.stringify(f.spans()).includes('public-kept'));
});
test('child completes before parent correlation: retain then join exactly once beneath invoking tool',async t=>{
 const f=await fixture(t),parent=f.payload('parent'),child={...f.payload('child'),initialNumSteps:0,lastUserInput:''};
 await f.rows(child,[response(1,'child response')]);await f.rows(parent,[]);
 await f.send('PreInvocation',child);await f.send('PostInvocation',child);
 await f.send('Stop',{...child,fullyIdle:true,terminationReason:'NO_TOOL_CALL'});
 assert.equal(f.spans().length,0);assert.equal(await pendingTurns(f.config),1);
 await f.send('PreInvocation',parent);
 await f.send('PostToolUse',{...parent,stepIdx:2,toolCall:{name:'invoke_subagent',args:{Prompt:'child prompt'}}});
 await fs.appendFile(parent.transcriptPath,[response(1),{step_index:2,status:'DONE',source:'MODEL',type:'GENERIC',content:'Created the following subagent:\n'+JSON.stringify({conversationId:'child'})+'\nThe subagent will send you a message.'}].map(JSON.stringify).join('\n')+'\n');
 await f.send('PostInvocation',parent);
 await Promise.all([f.send('Stop',{...parent,fullyIdle:true,terminationReason:'NO_TOOL_CALL'}),f.send('Stop',{...parent,fullyIdle:true,terminationReason:'NO_TOOL_CALL'})]);
 const spans=f.spans(),roots=spans.filter(s=>!s.parentSpanId),llms=spans.filter(s=>attrs(s)['fi.span.kind']==='LLM');
 assert.equal(roots.length,1);assert.equal(llms.length,2);assert.equal(new Set(spans.map(s=>s.traceId)).size,1);
 const subagent=spans.find(s=>s.name==='subagent'),tool=spans.find(s=>s.name==='invoke_subagent');assert.equal(subagent.parentSpanId,tool.spanId);
 assert.equal(llms.reduce((n,s)=>n+Number(attrs(s)['gen_ai.usage.input_tokens']),0),34);
 assert.equal(await pendingTurns(f.config),0);
});
test('legacy lock files are inert under the unified protected writer',async t=>{
 const f=await fixture(t),p=f.payload('legacy');await f.rows(p,[response(1)]);await f.send('PreInvocation',p);
 const name=(await fs.readdir(f.dir)).find(n=>n.endsWith('.json')),file=path.join(f.dir,name);
 await fs.writeFile(file+'.lock','legacy owner',{mode:0o600});
 await f.send('PostInvocation',p);await f.send('Stop',{...p,fullyIdle:true,terminationReason:'NO_TOOL_CALL'});
 assert.equal(f.spans().filter(s=>!s.parentSpanId).length,1);
 assert.equal(await fs.readFile(file+'.lock','utf8'),'legacy owner');
});
for(const interval of ['atomic-write'])test(`SIGKILL in ${interval} does not wedge later checkpoint saves`,async t=>{
 const f=await fixture(t),p=f.payload('kill');await f.rows(p,[response(1)]);await f.send('PreInvocation',p);
 const name=(await fs.readdir(f.dir)).find(n=>n.endsWith('.json')),file=path.join(f.dir,name);
 // Stop an actual writer at the filesystem boundary, not a manually fabricated lock.
 const script=`const fs=require('node:fs').promises;const open=fs.open.bind(fs);fs.open=async function(p,...args){const h=await open(p,...args);if(${JSON.stringify(interval)}==='legacy-inner-lock'?p===${JSON.stringify(file+'.lock')}:String(p).endsWith('.tmp')){process.send('acquired');await new Promise(()=>{})}return h};const files=require('@traceai/agent-core/files');const run=async()=>{const raw=await files.readProtected(${JSON.stringify(file)});${interval==='legacy-inner-lock'?`await files.writeProtected(${JSON.stringify(file)},raw);`:`await require('proper-lockfile').lock(${JSON.stringify(file)},{realpath:false,lockfilePath:${JSON.stringify(file.replace('.json','.event-lock-v2'))}});await files.writeProtectedUnderLock(${JSON.stringify(file)},raw,files.contentHash(raw));`}};run().catch(()=>process.exit(1));`;
 const processWriter=spawn(process.execPath,['-e',script],{cwd:path.resolve(__dirname,'..'),stdio:['ignore','ignore','pipe','ipc']});
 const killed=new Promise(r=>processWriter.on('close',r));
 await new Promise((resolve,reject)=>{const timer=setTimeout(()=>{processWriter.kill('SIGKILL');reject(Error('writer did not reach interval'))},10000);processWriter.once('message',()=>{clearTimeout(timer);resolve()});processWriter.once('error',reject)});
 processWriter.kill('SIGKILL');await killed;
 const eventLock=file.replace('.json','.event-lock-v2');await fs.utimes(eventLock,new Date(0),new Date(0)).catch(()=>{});
 await f.send('PostInvocation',p);await f.send('Stop',{...p,fullyIdle:true,terminationReason:'NO_TOOL_CALL'});
 assert.equal(f.spans().filter(s=>!s.parentSpanId).length,1);assert.equal(f.spans().filter(s=>attrs(s)['fi.span.kind']==='LLM').length,1);
 assert.equal(unseal(await fs.readFile(file,'utf8'),f.config).active,undefined);
});
test('host exit fences process identity and races Stop without duplicate exports',async t=>{
 const f=await fixture(t),p=f.payload('exit'),host={pid:12345,start:'5678'};await f.rows(p,[response(1)]);
 await f.send('PreInvocation',p,{host});await f.send('PostInvocation',p);
 await f.send('HostExit',p,{host:{...host,start:'different'}});assert.equal(f.spans().length,0);assert.equal(await pendingTurns(f.config),1);
 await Promise.all([f.send('HostExit',p,{host}),f.send('Stop',{...p,fullyIdle:true,terminationReason:'USER_CANCELED'})]);
 assert.equal(f.spans().filter(s=>!s.parentSpanId).length,1);assert.equal(f.spans().filter(s=>attrs(s)['fi.span.kind']==='LLM').length,1);assert.equal(await pendingTurns(f.config),0);
});
