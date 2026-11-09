const assert = require('node:assert/strict');
const { trace, context, propagation } = require('@opentelemetry/api');
const { BasicTracerProvider, SimpleSpanProcessor } = require('@opentelemetry/sdk-trace-base');
const { AsyncLocalStorageContextManager } = require('@opentelemetry/context-async-hooks');
const positiveImageCap = process.env.MODE === 'library-error-positive-cap';
const mode = positiveImageCap ? 'library-error' : process.env.MODE;
const apiKey = 'FI-API-PRIVATE-12345', secretKey = 'FI-SECRET-PRIVATE-12345', providerKey = 'provider-PRIVATE-12345';
const options = { apiKey, secretKey, instrumentations: ['wire'], ...(positiveImageCap ? {traceConfig:{base64ImageMaxLength:4096}} : {}) };
const exported = [], appSpans = [];
const exporter = { export(spans, done) { exported.push(...spans); done({code:0}); }, async shutdown() {} };
function installProvider() {
  const provider = new BasicTracerProvider({spanProcessors:[new SimpleSpanProcessor({export(spans,done) {appSpans.push(...spans);done({code:0});},async shutdown(){}})]});
  trace.setGlobalTracerProvider(provider); context.setGlobalContextManager(new AsyncLocalStorageContextManager().enable()); return provider;
}
(async () => {
  if (mode === 'wire-context-privacy') {
    installProvider(); const {setSession} = require('@traceai/fi-core');
    globalThis.fetch = async () => new Response('{"choices":[{"message":{"role":"assistant","content":"answer"}}]}', {headers:{'content-type':'application/json'}});
    const auto = require('@traceai/auto');auto.init({...options,exporter});
    await context.with(setSession(context.active(),{sessionId:providerKey}),async () => {
      const response=await fetch('https://api.openai.com/v1/chat/completions',{method:'POST',headers:{authorization:`Bearer ${providerKey}`},body:'{"messages":[]}'});await response.text();
    });
    await auto.forceFlush();assert.equal(exported.length,1);assert.equal(exported[0].attributes['session.id'],'<redacted>');
    assert(!JSON.stringify(exported[0].attributes).includes(providerKey));await auto.shutdown();
    console.log(JSON.stringify({protected:true,applicationUnchanged:true,exported:exported.length}));return;
  }
  if (mode === 'shutdown-decode') {
    const provider = installProvider();
    const decoding = require('../../dist/src/wire/encoding-node.js'), original = decoding.decodeNodeBody;
    let entered, release;
    const started = new Promise(resolve => { entered = resolve; }), gate = new Promise(resolve => {release = resolve;});
    decoding.decodeNodeBody = async (...args) => { entered(); await gate; return original(...args); };
    const bytes = require('node:zlib').gzipSync('{"choices":[{"message":{"role":"assistant","content":"answer"}}]}');
    globalThis.fetch = async () => new Response(bytes, {headers:{'content-type':'application/json','content-encoding':'gzip'}});
    const auto = require('@traceai/auto'); auto.init({...options,exporter});
    const response = await fetch('https://api.openai.com/v1/chat/completions',{method:'POST',body:'{"messages":[]}'});
    assert.deepEqual(Buffer.from(await response.arrayBuffer()),bytes); await started;
    assert.equal(globalThis[Symbol.for('traceai.auto.agent.v1')].fetch.pending.size,1);
    const closing = auto.shutdown(), processorsDuringDrain = provider._activeSpanProcessor._spanProcessors.length;
    release(); await closing;
    console.log(JSON.stringify({exported:exported.length,appSpans:appSpans.length,processorsDuringDrain,processorsAfter:provider._activeSpanProcessor._spanProcessors.length,output:exported[0]?.attributes['output.value']})); return;
  }
  if (mode === 'sigterm-order') {
    const keepalive = setInterval(() => {},1000);
    process.once('SIGTERM', () => console.log('application-once'));
    exporter.forceFlush = async () => { console.log('flush-start'); await new Promise(resolve => setImmediate(resolve)); console.log('flush-finished'); };
    const auto = require('@traceai/auto'); auto.init({...options,exporter});
    process.on('SIGTERM', () => { console.log('application-handler');clearInterval(keepalive);process.exit(0); });
    process.kill(process.pid,'SIGTERM'); return;
  }
  if (mode === 'owned-privacy') {
    const provider = installProvider(), auto = require('@traceai/auto');
    auto.init({...options,exporter,traceConfig:{hideInputs:true,hideOutputs:true}});
    const span = provider.getTracer('application').startSpan('app');
    span.setAttributes({'gen_ai.input.messages':'[{"role":"user","content":"PRIVATE-PROMPT"}]','gen_ai.output.messages':'[{"role":"assistant","content":"PRIVATE-ANSWER"}]','raw.input':'PRIVATE-PROMPT','raw.output':'PRIVATE-ANSWER'});
    span.addEvent('exception',{'exception.message':'PRIVATE-PROMPT','exception.type':'Error','custom':`https://cdn.example/?sig=SIGNED-PRIVATE data:image/png;base64,U0VDUkVU ${apiKey}`});
    span.setStatus({code:2,message:'PRIVATE-PROMPT'});span.end(); await auto.forceFlush();
    const copy = exported[0]; assert.equal(appSpans[0].status.message,'PRIVATE-PROMPT');
    assert.equal(appSpans[0].events[0].attributes['exception.message'],'PRIVATE-PROMPT');
    assert.notEqual(copy.attributes,appSpans[0].attributes);
    const data = JSON.stringify({attributes:copy.attributes,status:copy.status,events:copy.events});
    for (const value of ['PRIVATE-PROMPT','PRIVATE-ANSWER','SIGNED-PRIVATE','U0VDUkVU',apiKey]) assert(!data.includes(value),value);
    await auto.shutdown(); console.log(JSON.stringify({protected:true,applicationUnchanged:true,appSpans:appSpans.length,exported:exported.length})); return;
  }
  if (mode === 'propagator-preserved') {
    const custom = {inject(){},extract(ctx){return ctx;},fields(){return ['x-application'];}};propagation.setGlobalPropagator(custom);
    const auto = require('@traceai/auto');auto.init({...options,exporter});
    assert.equal(propagation._getGlobalPropagator(),custom); await auto.shutdown();
    console.log(JSON.stringify({preserved:propagation._getGlobalPropagator()===custom}));return;
  }
  const provider = installProvider();
  let manual;
  if (mode === 'manual-ownership') {
    const {OpenAIInstrumentation} = require('@traceai/openai');
    manual = new OpenAIInstrumentation({instrumentationConfig:{enabled:false}});manual.setTracerProvider(provider);manual.enable();
    require('openai');
  }
  const {createServer} = require('node:http');
  const completion = {id:'test',model:'m',choices:[{message:{role:'assistant',content:'PRIVATE-ANSWER'},finish_reason:'stop'}]};
  const dirty = `${apiKey} ${secretKey} ${providerKey} https://cdn.example/?token=SIGNED-PRIVATE data:image/png;base64,U0VDUkVU`;
  let requests = 0;
  const server = createServer((req,res) => {req.resume();req.on('end',() => {requests++;res.writeHead(mode === 'library-error' ? 401 : 200,{'content-type':'application/json'});res.end(JSON.stringify(mode === 'library-error' ? {error:{message:dirty}} : completion));});});
  await new Promise(resolve => server.listen(0,'127.0.0.1',resolve));
  const auto = require('@traceai/auto');auto.init({...options,exporter,instrumentations:['openai','wire'],...(mode === 'library-hide' ? {traceConfig:{hideInputs:true,hideOutputs:true}} : {})});
  const OpenAI = require('openai'), client = new OpenAI({apiKey:providerKey,baseURL:`http://127.0.0.1:${server.address().port}/v1`,maxRetries:0});
  const wrappedBefore = OpenAI.Chat.Completions.prototype.create;
  try {
    if (mode === 'manual-ownership') {
      const result1 = await client.chat.completions.create({model:'m',messages:[{role:'user',content:'hello'}]});assert.equal(result1.choices[0].message.content,'PRIVATE-ANSWER');
      await auto.shutdown();
      const wrappedAfter = OpenAI.Chat.Completions.prototype.create;
      const result2 = await client.chat.completions.create({model:'m',messages:[{role:'user',content:'hello'}]});assert.equal(result2.choices[0].message.content,'PRIVATE-ANSWER');
      await provider.forceFlush();
      console.log(JSON.stringify({sameWrapper:wrappedBefore===wrappedAfter,wrapped:!!wrappedAfter.__wrapped,manualEnabled:manual.isEnabled(),appSpans:appSpans.length,requests}));manual.disable();
    } else {
      let originalError;
      try { const result = await client.chat.completions.create({model:'m',messages:[{role:'user',content:'PRIVATE-PROMPT'}]}); assert.equal(result.choices[0].message.content,'PRIVATE-ANSWER'); }
      catch(error) {originalError=error;if(mode !== 'library-error')throw error;assert.equal(error.status,401);for(const value of [apiKey,secretKey,providerKey])assert(error.message.includes(value));}
      await auto.forceFlush();assert.equal(exported.length,1);
      const span = exported[0], data = JSON.stringify({attributes:span.attributes,status:span.status,events:span.events});
      for(const value of [apiKey,secretKey,providerKey,'SIGNED-PRIVATE','U0VDUkVU',...(mode === 'library-hide' ? ['PRIVATE-PROMPT','PRIVATE-ANSWER'] : [])]) assert(!data.includes(value),value);
      assert.equal(appSpans.length,1);
      // Q7b, 2026-11-05: only APPLICATION-authored data has the native echo guarantee.
      if(originalError) {
        const beforeError = Object.getOwnPropertyDescriptors(originalError);
        for(const value of [apiKey,secretKey,providerKey]) assert(originalError.message.includes(value));
        assert(!appSpans[0].status.message.includes(providerKey));
        for(const value of [apiKey,secretKey,'SIGNED-PRIVATE','U0VDUkVU']) assert(!appSpans[0].status.message.includes(value),value);
        // F2 cap-0 whole-scope replacement precedes I1 media markers. Only the
        // explicit positive-cap twin retains the original partial-marker check.
        if(positiveImageCap) {
          assert(appSpans[0].status.message.includes('<redacted>'));
          assert(span.status.message.includes('<redacted>'));
        } else {
          assert.equal(appSpans[0].status.message,'[image-redacted]');
          assert.equal(span.status.message,'[image-redacted]');
        }
        await auto.forceFlush();
        assert.deepEqual(Object.getOwnPropertyDescriptors(originalError),beforeError);
      }
      await auto.shutdown();console.log(JSON.stringify({protected:true,applicationUnchanged:true,requests,exported:exported.length,events:span.events.length}));
    }
  } finally { server.closeAllConnections();await new Promise(resolve => server.close(resolve)); }
})().catch(error => {console.error(error);process.exitCode=1;});
