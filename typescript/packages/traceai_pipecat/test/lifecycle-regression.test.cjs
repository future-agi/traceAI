// Real SDK regressions: only media I/O is stubbed; HTTP stays on loopback.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const http = require('node:http');
const req = require;
const { context, trace, SpanKind, SpanStatusCode } = req('@opentelemetry/api');
const { AsyncLocalStorageContextManager } = req('@opentelemetry/context-async-hooks');
const { BasicTracerProvider, InMemorySpanExporter, SimpleSpanProcessor } = req('@opentelemetry/sdk-trace-base');
const { PipecatInstrumentation, observePipecatClient } = req('../dist/src/index.js');
const { createClient } = req('./transport.cjs');
const op = (spans, name) => spans.filter(s => s.attributes['pipecat.operation'] === name);
async function server(t) {
  const received = [];
  const s = http.createServer((req,res) => {
    const chunks=[];req.on('data',c=>chunks.push(c));
    req.on('end',()=>{ received.push({url:req.url, body:Buffer.concat(chunks).toString()});
      res.setHeader('content-type','application/json');res.end(JSON.stringify({result:'synthetic-private-output',transport:'synthetic'})); });
  });
  await new Promise(resolve=>s.listen(0,'127.0.0.1',resolve));
  t.after(()=>new Promise(resolve=>{s.close(resolve);s.closeAllConnections();}));
  return { url:`http://127.0.0.1:${s.address().port}`, received };
}
function setup(t,sdk,{instrument=true}={}) {
  const exporter = new InMemorySpanExporter();const started=[];
  const provider = new BasicTracerProvider({spanProcessors:[new SimpleSpanProcessor(exporter),{onStart(s){started.push(s);},onEnd(){},async forceFlush(){},async shutdown(){}}]});
  const manager = new AsyncLocalStorageContextManager().enable();context.setGlobalContextManager(manager);
  let instrumentation;
  if(instrument){
    instrumentation = new PipecatInstrumentation();instrumentation.setTracerProvider(provider);instrumentation.manuallyInstrument(sdk);
  }
  const {client,transport} = createClient(sdk);client.setLogLevel(sdk.LogLevel.NONE);
  t.after(async()=>{instrumentation?.disable();await provider.shutdown();manager.disable();context.disable();});
  return {client,transport,exporter,provider,started,instrumentation};
}
for(const name of ['pipecat-floor','@pipecat-ai/client-js']) {
  const sdk=req(name);
  test(`smoke ${name}: real SDK sends to local server; parent, kind, privacy, success and failure`,async t=>{
    const {url,received}=await server(t);
    const {client,transport,exporter,provider,started}=setup(t,sdk);
    const parent=provider.getTracer('app').startSpan('application');
    const outgoing=[];
    transport.sendMessage=message=>{
      outgoing.push(message);
      if(message.type==='action'||message.type==='client-message'){
        fetch(`${url}/request`,{method:'POST',body:JSON.stringify(message)}).then(r=>r.json()).then(payload=>{
          const current=message.type==='client-message';
          transport.onMessage(new sdk.RTVIMessage(current?'server-response':'action-response',current?{t:message.data.t,d:payload}:payload,message.id));
        });
      }
    };
    await context.with(trace.setSpan(context.active(),parent),async()=>{
      await client.connect();
      if(sdk.PipecatClient){
        const response=await client.sendClientRequest('synthetic-private-type',{input:'synthetic-private-input'});
        assert.equal(response.result,'synthetic-private-output');
        assert.equal(typeof await client.sendText('synthetic-private-text'),'string');
      }else{
        await client.action({service:'synthetic-private-service',action:'synthetic-private-action',arguments:[]});
      }
      const failure=new Error('synthetic-private-error');
      transport.sendMessage=()=>{throw failure;};
      assert.throws(()=>sdk.PipecatClient?client.sendClientMessage('synthetic-private-type',{}):client.sendMessage(new sdk.RTVIMessage('synthetic-private-type',{})),e=>e===failure);
      transport.sendMessage=()=>{};
      await client.disconnect();
    });
    const spans=exporter.getFinishedSpans();
    assert.ok(received.length>=1);assert.ok(outgoing.length>=1);
    assert.equal(op(spans,'client.connect').length,1);
    assert.equal(op(spans,'client.disconnect').length,1);
    assert.ok(spans.some(s=>s.status.code===SpanStatusCode.ERROR));
    assert.equal(started.length-1,spans.length,'All operation spans end at disconnect');
    for(const s of spans){
      assert.equal(s.kind,SpanKind.CLIENT);assert.equal(s.attributes['fi.span.kind'],'CHAIN');
      assert.equal(s.attributes['pipecat.telemetry_source'],'client');
      // RTVIClient.action() legitimately delegates to the distinct send_message operation.
      const expectedParent = !sdk.PipecatClient && s.attributes['pipecat.operation']==='send_message' && s.status.code===SpanStatusCode.OK
        ? op(spans,'action')[0].spanContext().spanId : parent.spanContext().spanId;
      assert.equal(s.parentSpanId,expectedParent);assert.equal(s.spanContext().traceId,parent.spanContext().traceId);
      // This client-only instrumentor intentionally does not invent model/provider/token/tool/retrieval data.
      assert.ok(!Object.keys(s.attributes).some(k=>/model|provider|token|cost|input|output|tool|retrieval/.test(k)));
    }
    assert.ok(!JSON.stringify(spans.map(s=>[s.name,s.attributes,s.events,s.status])).includes('synthetic-private'));
    parent.end();
  });

  test(`regression ${name}: cancelled handshake must finish before disconnect + forceFlush`,async t=>{
    const {client,transport,exporter,provider,started}=setup(t,sdk);
    let ready=false;transport.sendReadyMessage=()=>{ready=true;};
    let settled=false;client.connect().then(()=>{settled=true;},()=>{settled=true;});
    const deadline=Date.now()+2000;
    while(!ready&&Date.now()<deadline)await new Promise(resolve=>setTimeout(resolve,1));
    assert.equal(ready,true,'Reached actual SDK bot-ready wait');
    await client.disconnect();await provider.forceFlush();
    const spans=exporter.getFinishedSpans();
    assert.equal(settled, false, 'Instrumentation must not settle an abandoned SDK promise');
    assert.equal(started.length, spans.length, 'Disconnect finalizes all started spans before flushing');
    assert.equal(op(spans,'client.connect').length,1,'An abandoned SDK promise must not leave its connect span open');
    assert.equal(op(spans,'client.connect')[0].status.code,SpanStatusCode.ERROR);
  });
}

test('regression latest: endpoint connection emits one operation, not duplicate nested connects',async t=>{
  const sdk=req('@pipecat-ai/client-js');const {url,received}=await server(t);
  const {client,transport,exporter}=setup(t,sdk);
  let transportCalls=0;const original=transport.connect.bind(transport);
  transport.connect=async(...args)=>{transportCalls++;return original(...args);};
  await client.connect({endpoint:`${url}/start-bot`});await client.disconnect();
  const spans=op(exporter.getFinishedSpans(),'client.connect');
  assert.equal(received.length,1);assert.equal(transportCalls,1);
  assert.equal(spans.length,1,'One external connection attempt must produce one connection span');
});

test('regression floor: handshake timeout must export ERROR in browser lifecycle telemetry',async t=>{
  const sdk=req('pipecat-floor');const {transport,provider,exporter}=setup(t,sdk,{instrument:false});
  transport.sendReadyMessage=()=>{};
  const client=new sdk.RTVIClient({transport,enableMic:false,enableCam:false,timeout:20,params:{baseUrl:'http://127.0.0.1/unused'},customConnectHandler:async()=>({})});
  client.setLogLevel(sdk.LogLevel.NONE);const states=[];client.on('transportStateChanged',state=>states.push(state));
  const detach=observePipecatClient(client,{tracer:provider.getTracer('browser-events')});t.after(detach);
  await assert.rejects(client.connect());await provider.forceFlush();
  const spans=exporter.getFinishedSpans();
  assert.ok(states.includes('error'),'Upstream generated a genuine error-state event');
  assert.ok(spans.some(s=>s.status.code===SpanStatusCode.ERROR),'Failed handshake cannot be reported as all OK');
});
