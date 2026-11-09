const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const {join} = require('node:path');
const {tmpdir} = require('node:os');
const http = require('node:http');
const {resolveConfig,disable} = require('@traceai/agent-core');
const {createLauncherDelivery,LauncherCaptureProcessor} = require('../dist/launcher-delivery.js');
function attributes(span) {return Object.fromEntries(span.attributes.map(a=>[a.key,a.value.stringValue??a.value.intValue??a.value.boolValue]));}
function source(trace, id, parent, name='openclaw.harness.run', status=1) {
  const context={traceId:trace.repeat(32),spanId:id.repeat(16),traceFlags:1};
  return {name,spanContext:()=>context,parentSpanContext:parent?{...context,spanId:parent.repeat(16)}:undefined,
    kind:0,startTime:[100,0],endTime:[101,0],duration:[1,0],status:{code:status},
    attributes:name==='openclaw.model.call'?{'openclaw.model_call.observation_unit':'request',
      'gen_ai.usage.input_tokens':11,'gen_ai.usage.output_tokens':7}: {},events:[],links:[]};
}
test('retry trees share one prompt root without span-id collisions, usage duplication or session drift',async t=>{
  const home=await fs.mkdtemp(join(tmpdir(),'fi-prompt-')), received=[];
  const server=http.createServer(async(req,res)=>{
    let body='';for await(const chunk of req)body+=chunk;
    for(const r of JSON.parse(body).resourceSpans??[])for(const s of r.scopeSpans??[])received.push(...s.spans);
    res.setHeader('content-type','application/json');res.end('{}');
  });
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  t.after(async()=>{server.closeAllConnections();await new Promise(resolve=>server.close(resolve));await fs.rm(home,{recursive:true,force:true});});
  const options={agent:'openclaw',home,env:{FI_AGENT_ENABLED:'true',FI_API_KEY:'synthetic-public',FI_SECRET_KEY:'synthetic-private',FI_OTLP_ENDPOINT:`http://127.0.0.1:${server.address().port}/traces`}};
  const config=await resolveConfig(options);
  let previous;
  for(const [exitCode, outcome] of [[0,'success'],[1,'error'],[130,'interrupted'],[143,'interrupted'],[137,'incomplete']]) {
    const delivery=await createLauncherDelivery(config,'stable-session');
    const capture=new LauncherCaptureProcessor(config,delivery.fd,Buffer.from(delivery.key,'hex'),'stable-session');
    for(const nativeTrace of ['a','b']) {
      // Both native traces deliberately reuse span IDs; children can precede parents.
      const model=source(nativeTrace,'2','1','openclaw.model.call',nativeTrace==='a'?2:1);
      if(nativeTrace==='a') model.attributes={}; // failed attempt reported no usage
      capture.onEnd(model);capture.onEnd(model); // duplicate terminal callback
      capture.onEnd(source(nativeTrace,'1',undefined));
    }
    await delivery.finish(exitCode); await delivery.finish(exitCode);
    const batch=received.splice(0), root=batch.find(s=>s.name==='openclaw.prompt');
    assert.equal(batch.length,5);
    assert.equal(new Set(batch.map(s=>s.traceId)).size,1);
    assert.equal(new Set(batch.map(s=>s.spanId)).size,5);
    assert.equal(batch.filter(s=>!s.parentSpanId).length,1);
    assert.ok(root); assert.notEqual(root.traceId,previous);previous=root.traceId;
    assert.equal(root.status.code,exitCode===0?1:2);
    assert.equal(attributes(root)['fi.agent.outcome'],outcome);
    assert.ok(batch.every(s=>attributes(s)['session.id']==='stable-session'));
    assert.ok(batch.every(s=>attributes(s)['fi.agent.turn_correlation']==='launcher-invocation'));
    for(const child of batch.filter(s=>s!==root)) {
      assert.ok(batch.some(s=>s.spanId===child.parentSpanId));
      assert.ok(attributes(child)['fi.agent.native.trace_id']);
      assert.ok(attributes(child)['fi.agent.native.span_id']);
    }
    assert.equal(batch.reduce((n,s)=>n+Number(attributes(s)['gen_ai.usage.input_tokens']||0),0),11);
    assert.equal(attributes(root)['gen_ai.usage.input_tokens'],undefined);
  }
  for (const recover of [false, true]) {
    const delivery = await createLauncherDelivery(config, 'stable-session');
    const capture = new LauncherCaptureProcessor(config, delivery.fd, Buffer.from(delivery.key, 'hex'));
    capture.onEnd(source('c', '2', undefined, 'openclaw.model.call', 2));
    if (recover) capture.onEnd(source('c', '3', undefined, 'openclaw.model.call', 1));
    await delivery.finish(0); // Some hosts report provider failures with exit 0.
    const batch = received.splice(0);
    assert.equal(batch.find(s => s.name === 'openclaw.prompt').status.code, recover ? 1 : 2);
  }
  for (const withRequests of [false, true]) {
    const delivery = await createLauncherDelivery(config, 'stable-session');
    const capture = new LauncherCaptureProcessor(config, delivery.fd, Buffer.from(delivery.key, 'hex'));
    const turn = source('d', '2', undefined, 'openclaw.model.call');
    turn.attributes['openclaw.model_call.observation_unit'] = 'turn';
    capture.onEnd(turn);
    if (withRequests) capture.onEnd(source('d', '3', undefined, 'openclaw.model.call'));
    await delivery.finish();
    const batch = received.splice(0), root = batch.find(s => s.name === 'openclaw.prompt');
    assert.equal(batch.reduce((n, s) => n + Number(attributes(s)['gen_ai.usage.input_tokens'] || 0), 0), 11);
    assert.equal(attributes(root)['gen_ai.usage.input_tokens'], withRequests ? undefined : 11);
    assert.equal(attributes(root)['fi.usage.contribution'], withRequests ? 'none' : 'additive');
    assert.equal(attributes(root)['fi.agent.usage.plan'], withRequests ? 'request' : 'turn');
    assert.equal(batch.filter(s => attributes(s)['fi.usage.contribution'] === 'additive').length, 1);
  }
  const disabled=await createLauncherDelivery(config,'stable-session');
  const capture=new LauncherCaptureProcessor(config,disabled.fd,Buffer.from(disabled.key,'hex'));
  capture.onEnd(source('a','1'));
  await disable(options);await disabled.finish();
  assert.equal(received.length,0,'disable fences both native trees and the new root');
});
