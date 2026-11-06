const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs/promises');
const os=require('node:os');
const path=require('node:path');
const {applyEvent}=require('../dist/mapper');
const {enrichTranscript}=require('../dist/transcript');
async function fixture(t){
 const home=await fs.mkdtemp(path.join(os.tmpdir(),'fi-agy-transcript-'));
 t.after(()=>fs.rm(home,{recursive:true,force:true}));
 const transcriptPath=path.join(home,'.gemini/antigravity-cli/brain/test/.system_generated/logs/transcript_full.jsonl');
 await fs.mkdir(path.dirname(transcriptPath),{recursive:true,mode:0o700});
 const config={captureContent:true,hideInputs:false,hideOutputs:false,hideErrorDetails:false,
   paths:{user:path.join(home,'.gemini/config/futureagi.json')}};
 const payload={conversationId:'test',transcriptPath,initialNumSteps:1,invocationNum:0,lastUserInput:'Read README.md'};
 const state={schemaVersion:1,generation:0,touched:Date.now(),completed:[]};
 const rows=(await fs.readFile(path.join(__dirname,'fixtures/real-cli-1.5.2-transcript.jsonl'),'utf8')).trim().split('\n');
 return {home,transcriptPath,config,payload,state,rows};
}
test('real transcript token ownership, tool results, resumed injected system step and cursor dedup',async t=>{
 const f=await fixture(t);
 applyEvent(f.state,'PreInvocation',f.payload,f.config);
 applyEvent(f.state,'PostToolUse',{...f.payload,stepIdx:2,toolCall:{name:'read_file',args:{Path:'README.md'}}},f.config);
 await fs.writeFile(f.transcriptPath,f.rows.slice(0,3).join('\n')+'\n');
 await enrichTranscript(f.state,[f.state.active],f.payload,f.config);
 assert.equal(f.state.active.activities.filter(a=>a.kind==='llm').length,1);
 assert.ok(f.state.active.activities.find(a=>a.kind==='tool').output.includes('Synthetic fixture repository'));
 applyEvent(f.state,'PreInvocation',{...f.payload,initialNumSteps:3,invocationNum:1},f.config);
 await fs.appendFile(f.transcriptPath,f.rows[3]+'\n');
 await enrichTranscript(f.state,[f.state.active],f.payload,f.config);
 assert.equal(f.state.active.activities.filter(a=>a.kind==='llm').length,2);
 const first=applyEvent(f.state,'Stop',{...f.payload,fullyIdle:true,terminationReason:'NO_TOOL_CALL'},f.config)[0];
 // Runtime observes the next native USER_INPUT before applying PreInvocation.
 await fs.appendFile(f.transcriptPath,f.rows.slice(4,6).join('\n')+'\n');
 await enrichTranscript(f.state,[],f.payload,f.config);
 applyEvent(f.state,'PreInvocation',{...f.payload,initialNumSteps:5,lastUserInput:'Read missing.txt'},f.config);
 await fs.appendFile(f.transcriptPath,f.rows.slice(6,8).join('\n')+'\n');
 await enrichTranscript(f.state,[f.state.active],f.payload,f.config);
 const generation=f.state.active.activities.find(a=>a.kind==='llm');
 assert.equal(generation.id,'generation-6');assert.equal(generation.parentId,'inv-5-0');
 assert.equal(generation.usage.input,60);assert.equal(generation.usage.output,10);
 assert.equal(generation.usage.reasoningInOutput,true); // mock candidates 7 + thoughts 3 => source output_tokens 10
 assert.ok(!Object.hasOwn(generation.usage,'reasoning')); // source omits the breakdown: do not invent zero
 const cursor=f.state.transcript.offset;
 await enrichTranscript(f.state,[f.state.active],f.payload,f.config);
 assert.equal(f.state.transcript.offset,cursor);assert.equal(f.state.active.activities.filter(a=>a.kind==='llm').length,1);
 assert.equal(first.activities.filter(a=>a.kind==='llm').reduce((n,a)=>n+a.usage.input,0),60);
});
test('first capture on a resumed session finds the current user record without replaying historical generations',async t=>{
 const f=await fixture(t),payload={...f.payload,initialNumSteps:5,lastUserInput:'Read missing.txt'};
 await fs.writeFile(f.transcriptPath,f.rows.slice(0,8).join('\n')+'\n');
 await enrichTranscript(f.state,[],payload,f.config,true);
 assert.equal(f.state.latestUserStep,4);
 applyEvent(f.state,'PreInvocation',payload,f.config);
 await enrichTranscript(f.state,[f.state.active],payload,f.config);
 assert.equal(f.state.active.userStep,4);
 assert.deepEqual(f.state.active.activities.filter(a=>a.kind==='llm').map(a=>a.id),['generation-6']);
 assert.equal(f.state.active.activities.find(a=>a.kind==='llm').parentId,'inv-5-0');
});
test('partial line is retained; malformed, truncated and foreign transcript paths do not leak content',async t=>{
 const f=await fixture(t);applyEvent(f.state,'PreInvocation',f.payload,f.config);
 await fs.writeFile(f.transcriptPath,f.rows[1].slice(0,50));
 await enrichTranscript(f.state,[f.state.active],f.payload,f.config);assert.equal(f.state.transcript,undefined);
 await fs.appendFile(f.transcriptPath,f.rows[1].slice(50)+'\n');
 await enrichTranscript(f.state,[f.state.active],f.payload,f.config);assert.equal(f.state.active.activities.filter(a=>a.kind==='llm').length,1);
 await fs.writeFile(f.transcriptPath,'{}\n');
 await assert.rejects(enrichTranscript(f.state,[f.state.active],f.payload,f.config),/TRANSCRIPT_TRUNCATED/);
 const foreign=path.join(f.home,'foreign.jsonl');await fs.writeFile(foreign,f.rows[1]+'\n');
 const other={schemaVersion:1,generation:0,touched:Date.now(),completed:[],active:f.state.active};
 await enrichTranscript(other,[other.active],{...f.payload,transcriptPath:foreign},f.config);assert.equal(other.transcript,undefined);
 await fs.unlink(f.transcriptPath);await fs.symlink(foreign,f.transcriptPath);
 await assert.rejects(enrichTranscript(other,[other.active],f.payload,f.config));
 await fs.unlink(f.transcriptPath);await fs.writeFile(f.transcriptPath,'not-json\n');
 await assert.rejects(enrichTranscript(other,[other.active],f.payload,f.config),/TRANSCRIPT_MALFORMED/);
});
test('large existing history resynchronizes once and enriches current and later responses', async t => {
 const f=await fixture(t);applyEvent(f.state,'PreInvocation',f.payload,f.config);
 const old=JSON.stringify({step_index:0,status:'DONE',source:'TOOL',type:'TOOL_RESPONSE',content:'old history '.repeat(1000)})+'\n';
 await fs.writeFile(f.transcriptPath,old.repeat(110)+f.rows[1]+'\n');
 assert.equal(await enrichTranscript(f.state,[f.state.active],f.payload,f.config),true);
 assert.equal(f.state.transcript.offset,(await fs.stat(f.transcriptPath)).size);
 assert.equal(f.state.active.activities.filter(a=>a.kind==='llm').length,1);
 assert.equal(await enrichTranscript(f.state,[f.state.active],f.payload,f.config),false);
 applyEvent(f.state,'PreInvocation',{...f.payload,initialNumSteps:3,invocationNum:1},f.config);
 await fs.appendFile(f.transcriptPath,f.rows[3]+'\n');
 assert.equal(await enrichTranscript(f.state,[f.state.active],f.payload,f.config),false);
 assert.equal(f.state.active.activities.filter(a=>a.kind==='llm').length,2);
 assert.equal(f.state.transcript.offset,(await fs.stat(f.transcriptPath)).size);
});
for (const fragmented of [false,true]) test(`oversized tool record recovers without replay (fragmented=${fragmented})`, async t => {
 const f=await fixture(t);applyEvent(f.state,'PreInvocation',f.payload,f.config);
 await fs.writeFile(f.transcriptPath,f.rows[1]+'\n');
 await enrichTranscript(f.state,[f.state.active],f.payload,f.config);
 const tool=JSON.stringify({step_index:2,status:'DONE',source:'TOOL',type:'TOOL_RESPONSE',content:'x'.repeat(1200000)});
 await fs.appendFile(f.transcriptPath,tool+(fragmented?'':'\n'+f.rows[3]+'\n'));
 assert.equal(await enrichTranscript(f.state,[f.state.active],f.payload,f.config),true);
 if(fragmented){
   assert.equal(f.state.transcript.skippingLine,true);
   assert.equal(f.state.transcript.offset,(await fs.stat(f.transcriptPath)).size);
   // Persistence across hook processes must retain the partial-line discard state.
   f.state=JSON.parse(JSON.stringify(f.state));
   await fs.appendFile(f.transcriptPath,'\n'+f.rows[3]+'\n');
   assert.equal(await enrichTranscript(f.state,[f.state.active],f.payload,f.config),true);
 }
 assert.equal(f.state.transcript.skippingLine,undefined);
 assert.equal(f.state.transcript.offset,(await fs.stat(f.transcriptPath)).size);
 assert.equal(f.state.active.activities.filter(a=>a.kind==='llm').length,2);
 assert.equal(await enrichTranscript(f.state,[f.state.active],f.payload,f.config),false);
 assert.equal(f.state.active.activities.filter(a=>a.kind==='llm').length,2);
});
test('content-off and error-detail privacy survive transcript enrichment',async t=>{
 const f=await fixture(t);f.config.captureContent=false;
 applyEvent(f.state,'PreInvocation',f.payload,f.config);
 await fs.writeFile(f.transcriptPath,f.rows.slice(0,3).join('\n')+'\n');
 await enrichTranscript(f.state,[f.state.active],f.payload,f.config);
 const generation=f.state.active.activities.find(a=>a.kind==='llm');
 assert.equal(generation.input,undefined);assert.equal(generation.output,undefined);assert.equal(generation.usage.input,20);
});

test('detail capacity preserves native success and exact additive usage',()=>{
 const {emitTurn}=require('../dist/mapper');let result, start;
 const tracer={startTurn(input){start=input;return {child(){throw Error('detail cap must reserve root capacity')},end(end){result=end}}}};
 const activities=Array.from({length:300},(_,i)=>({id:'generation-'+i,kind:'llm',name:'generation',start:1,end:2,usage:{input:20,output:10},outcome:'success'}));
 emitTurn(tracer,'session',{id:'turn',start:1,end:2,outcome:'success',activities});
 assert.equal(start.usagePlan,'turn');assert.equal(result.outcome,'success');assert.equal(result.gap,'SPAN_DETAIL_LIMIT');
 assert.equal(result.usage.input,6000);assert.equal(result.usage.output,3000);
});
