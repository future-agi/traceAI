const {test} = require('node:test');
const assert = require('node:assert/strict');
const root=require('node:path').resolve(__dirname,'..');
const {fixture, fs, path, payload, core, child, spans, attributes}=require(root+'/test/helpers.cjs');
const {TranscriptReader, correlate, CursorObserver, normalizeEvent}=require(root+'/dist/index.js');
const {appendEvent}=require(root+'/dist/state.js');
const {installRuntime,commands}=require(root+'/dist/registration.js');
const rows=(n)=>Array.from({length:n},(_,i)=>[
 {role:'user',message:{content:`unique prompt ${i}`}},
 {role:'assistant',message:{content:`answer ${i}`}},
 {type:'turn_ended'}
]).flat().map(JSON.stringify).join('\n')+'\n';
test('history boundary: current complete unique turn at 64 vs 65',async t=>{
 const f=await fixture(t);
 const file=path.join(f.home,'.cursor/projects/example/agent-transcripts/session.jsonl');
 await fs.mkdir(path.dirname(file),{recursive:true,mode:0o700});
 for (const count of [64,65]) {
  await fs.writeFile(file, rows(count),{mode:0o600});
  const reader=new TranscriptReader(f.config);
  const result=await reader.read(file);
  const last=`unique prompt ${count-1}`;
  const matched=correlate(result,last,`answer ${count-1}`);
  const before=spans(f.requests).length;
  const observer=new CursorObserver(f.config);
  for(const [event,extra] of [['beforeSubmitPrompt',{prompt:last}],['afterAgentResponse',{text:`answer ${count-1}`}],['stop',{status:'completed',input_tokens:10,output_tokens:5}]])
   await observer.accept(normalizeEvent(payload(event,{...extra,generation_id:`turn-${count}`,transcript_path:file}),event,f.config));
  await observer.close();
  const exported=spans(f.requests).slice(before);
  const llms=exported.filter(s=>attributes(s)['fi.span.kind']==='LLM');
  console.log(JSON.stringify({probe:'history',count,gap:result.gap,matched:!!matched,spans:exported.length,llms:llms.length,usage:exported.map(s=>attributes(s)['gen_ai.usage.total_tokens']).filter(x=>x!==undefined)}));
  assert.equal(llms.length,1);
  assert.equal(result.gap,undefined);
 }
});
test('completed tombstones do not block unrelated fresh turns',async t=>{
 const f=await fixture(t,false);
 const runtime=await installRuntime(f.context);
 const hooks=commands(runtime,f.config.paths.configDir);
 // Reproduce accumulated successful terminal state through the public persistence function.
 for(let i=0;i<256;i++) await appendEvent(f.config,normalizeEvent(payload('stop',{conversation_id:`old-session-${i}`,generation_id:`old-turn-${i}`,status:'completed'}),'stop',f.config));
 const send=async(event,extra)=>{
  const out=await child(hooks[event].command,payload(event,{conversation_id:'fresh-session',generation_id:'fresh-turn',...extra}),{HOME:f.home,PATH:'/missing'});
  assert.equal(out.code,0);assert.equal(out.stdout,'');assert.equal(out.stderr,'');
 };
 await send('beforeSubmitPrompt',{prompt:'ordinary new turn'});
 await send('stop',{status:'completed',input_tokens:10,output_tokens:5});
 assert.equal(spans(f.requests).length,1);
 assert.equal(attributes(spans(f.requests)[0])['gen_ai.usage.total_tokens'],15);
 await send('beforeSubmitPrompt',{prompt:'ordinary new turn'});
 await send('stop',{status:'completed',input_tokens:10,output_tokens:5});
 assert.equal(spans(f.requests).length,1,'late duplicates remain suppressed');
});
