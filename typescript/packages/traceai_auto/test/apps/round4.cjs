const assert = require('node:assert/strict');
const { trace, context, createTraceState } = require('@opentelemetry/api');
const { BasicTracerProvider, SimpleSpanProcessor } = require('@opentelemetry/sdk-trace-base');
const { resourceFromAttributes } = require('@opentelemetry/resources');
const { AsyncLocalStorageContextManager } = require('@opentelemetry/context-async-hooks');
const mode = process.env.MODE, app = [], fi = [];
const exporter = { export(spans, done) { fi.push(...spans); done({ code: 0 }); }, async shutdown() {} };
const provider = new BasicTracerProvider({ resource: resourceFromAttributes({ custom: 'configured-private-token' }), spanProcessors: [new SimpleSpanProcessor({ export(spans, done) { app.push(...spans); done({ code: 0 }); }, async shutdown() {} })] });
trace.setGlobalTracerProvider(provider); context.setGlobalContextManager(new AsyncLocalStorageContextManager().enable());
const options = { apiKey: 'configured-private-token', secretKey: 'configured-private-secret', exporter, instrumentations: ['wire'] };
const auto = require('@traceai/auto');
(async () => {
  if (mode === 'request-credentials') {
    const { createServer } = require('node:http');
    const source = process.env.CREDENTIAL_SOURCE, library = process.env.CREDENTIAL_LAYER === 'library', large = source === 'large-body';
    let receivedBytes = 0;
    const secret = 'request-private+/token=12345', echoed = `Invalid MCP credential ${secret}`;
    const server = createServer((request, response) => {
      let text = ''; request.on('data', chunk => { text += chunk; });
      request.on('end', () => {
        receivedBytes = Buffer.byteLength(text); const body = JSON.parse(text);
        assert(source === 'query' ? new URL(request.url, 'http://localhost').searchParams.get('key') === secret : source === 'body' || large ? body.mcp_servers[0].authorization_token === secret : body.api_key === secret);
        response.writeHead(401, { 'content-type': 'application/json' }); response.end(JSON.stringify({ error: { message: echoed } }));
      });
    });
    await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
    try {
      const local = `http://127.0.0.1:${server.address().port}/v1`;
      if (large) {
        // Keep native fetch/Response semantics and real HTTP, while routing the
        // recognized provider exclusively to loopback without external network I/O.
        const native = globalThis.fetch;
        globalThis.fetch = (input, init) => native(local + new URL(input).pathname.slice(3), init);
      }
      auto.init({ ...options, instrumentations: library ? ['openai', 'wire'] : ['wire'] });
      const body = { model: 'fixture', messages: [], ...(large ? { padding: 'x'.repeat(4194304) } : {}), ...(source === 'body' || large ? { mcp_servers: [{ authorization_token: secret }] } : source === 'api-key' ? { api_key: secret } : {}) };
      const base = large ? 'https://api.openai.com/v1' : local;
      let original;
      if (library) {
        const OpenAI = require('openai'); const client = new OpenAI({ apiKey: 'unrelated-provider-key', baseURL: base, maxRetries: 0, ...(source === 'query' ? { defaultQuery: { key: secret } } : {}) });
        try { await client.chat.completions.create(body); assert.fail('expected provider error'); } catch (error) { original = error; assert.equal(error.status, 401); assert(error.message.includes(echoed)); }
      } else {
        const response = await fetch(base + '/chat/completions' + (source === 'query' ? '?%6bey=' + encodeURIComponent(secret) : ''), { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) });
        assert.equal(response.status, 401); original = await response.json(); assert.equal(original.error.message, echoed);
      }
      await auto.forceFlush(); assert.equal(fi.length, 1); assert.equal(app.length, 1);
      if (large) { assert(receivedBytes > 4194304); assert.equal(receivedBytes, Buffer.byteLength(JSON.stringify(body))); assert.deepEqual(fi[0].attributes, {}); assert.equal(fi[0].status.message, undefined); assert.deepEqual(fi[0].startTime, app[0].startTime); assert.deepEqual(fi[0].endTime, app[0].endTime); }
      const output = JSON.stringify(fi.map(span => ({ name: span.name, attributes: span.attributes, status: span.status, events: span.events, resource: span.resource.attributes, scope: span.instrumentationScope })));
      assert(!output.includes(secret), output); assert(!output.includes(encodeURIComponent(secret)), output); assert.equal(fi[0].status.code, 2);
      if (library) { assert(!app[0].status.message.includes(secret)); assert(original.message.includes(secret)); }
      console.log(JSON.stringify({ protected: true, applicationUnchanged: true, exported: fi.length }));
    } finally { await auto.shutdown(); server.closeAllConnections(); await new Promise(resolve => server.close(resolve)); }
    return;
  }
  if (mode === 'init-once') {
    const first = auto.init(options), agent = globalThis[Symbol.for('traceai.auto.agent.v1')], fetch = globalThis.fetch;
    const warnings = [], original = console.warn; console.warn = value => warnings.push(value);
    try { assert.equal(auto.init({ ...options, instrumentations: [], projectName: 'changed', exporter: { export() { throw new Error('replacement'); } } }), first); assert.equal(auto.init({ apiKey: '' }), first); }
    finally { console.warn = original; }
    assert.equal(agent.provider, first); assert.equal(agent.provider, provider); assert.equal(globalThis.fetch, fetch); assert.equal(warnings.length, 1); assert.notEqual(auto.report().config.projectName, 'changed');
    const span = provider.getTracer('app').startSpan('first provider'); span.end(); await auto.forceFlush(); assert.equal(fi.length, 1);
    await auto.shutdown(); assert.equal(provider._activeSpanProcessor._spanProcessors.length, 1);
    console.log(JSON.stringify({ sameProvider: true, oneWarning: true, unchangedHooks: true, exported: fi.length })); return;
  }
  if (mode === 'export-surfaces') {
    auto.init({ ...options, traceConfig: { hideLLMInvocationParameters: true } });
    const dirty = 'configured-private-token https://cdn.test/?signature=signed-private data:audio/wav;base64,U0VDUkVU';
    const span = provider.getTracer(dirty, dirty).startSpan(dirty, { attributes: { custom: [dirty, NaN, Infinity, -Infinity], 'gen_ai.request.parameters': 'hidden-private', 'http.request.header.authorization': 'Bearer provider-PRIVATE-token', echoed: 'provider-PRIVATE-token' }, links: [{ context: { traceId: '3'.repeat(32), spanId: '4'.repeat(16), traceFlags: 1, traceState: createTraceState('vendor=configured-private-token') }, attributes: { custom: dirty } }] });
    span._spanContext.traceState = createTraceState('vendor=configured-private-token');
    span.setStatus({ code: 2, message: dirty }); span.addEvent(dirty, { 'exception.message': dirty }); span.end(); await auto.forceFlush();
    assert.equal(app.length, 1); assert.equal(fi.length, 1);
    const output = fi[0], json = JSON.stringify({ name: output.name, attributes: output.attributes, status: output.status, events: output.events, links: output.links, resource: output.resource.attributes, scope: output.instrumentationScope, tracestate: output.spanContext().traceState?.serialize() });
    for (const forbidden of ['configured-private-token', 'signed-private', 'U0VDUkVU', 'hidden-private', 'provider-PRIVATE-token']) assert(!json.includes(forbidden), forbidden + ' leaked');
    assert.equal(output.spanContext().traceId, span.spanContext().traceId); assert.equal(output.spanContext().spanId, span.spanContext().spanId);
    assert.equal(app[0], span); assert(app[0].status.message.includes('configured-private-token')); assert.equal(app[0].attributes.echoed, 'provider-PRIVATE-token'); assert.equal(app[0].resource.attributes.custom, 'configured-private-token');
    await auto.shutdown(); console.log(JSON.stringify({ protected: true, unchanged: true, exported: fi.length })); return;
  }
  if (mode === 'dedup-native') {
    auto.init(options); const tracer = provider.getTracer('sdk');
    const model = tracer.startSpan('library model', { attributes: { 'gen_ai.span.kind': 'LLM' } });
    let parent = trace.setSpan(context.active(), model), natives = [];
    for (let i = 0; i < 8; i++) { const span = tracer.startSpan('native ' + i, {}, parent); natives.push(span); parent = trace.setSpan(parent, span); }
    await context.with(parent, async () => { const response = await fetch(process.env.MOCK_URL + '/v1/chat/completions', { method: 'POST', body: '{"messages":[]}' }); await response.text(); });
    for (const span of natives.reverse()) span.end(); model.end(); await auto.forceFlush();
    assert.equal(fi.length, 9); assert(!fi.some(span => span.attributes['traceai.capture.layer'] === 'wire'));
    await auto.shutdown(); console.log(JSON.stringify({ nativeSpans: 8, modelSpans: 1, wireSpans: 0 })); return;
  }
  if (mode === 'registry') {
    const Module = require('node:module'), path = require('node:path'), resolve = Module._resolveFilename;
    const optional = ['@traceai/vercel', '@traceai/mastra', '@traceai/genkit'];
    let mastraLoads = 0;
    Module._resolveFilename = function(request, ...args) { if (request === '@traceai/mastra' || request === '@traceai/mastra/auto') mastraLoads++; return optional.some(name => request === name || request === name + '/auto') ? path.resolve(__dirname, 'enable.cjs') : Reflect.apply(resolve, this, [request, ...args]); };
    auto.init({ ...options, instrumentations: ['vercel', 'mastra', 'genkit'] }); const result = auto.report();
    const statuses = result.integrations.filter(i => ['vercel', 'mastra', 'genkit'].includes(i.name));
    // All three real target packages are present in the workspace test install.
    assert.equal(statuses.length, 3);
    const expected = process.env.ENABLE_MODE === 'absent' ? 'manual' : process.env.ENABLE_MODE === 'throw' ? 'failed' : 'instrumented';
    assert(statuses.filter(i => i.name !== 'mastra').every(i => i.status === expected), JSON.stringify(statuses));
    const mastra = statuses.find(i => i.name === 'mastra');
    assert.equal(mastra.status, 'manual');
    assert.equal(mastra.reason, "Mastra zero-code bridge follows after 11-11; use createFIObservability / createFIMastraExporter from @traceai/mastra");
    assert.equal(mastraLoads, 0);
    assert.equal(globalThis.__traceai_enable_calls || 0, process.env.ENABLE_MODE === 'absent' ? 0 : 2);
    await auto.shutdown(); console.log(JSON.stringify({ statuses: statuses.map(i => i.status), selectedDestination: true, mastraReason: mastra.reason, mastraLoads })); return;
  }
  if (mode === 'agents' || mode === 'cursor' || mode === 'cursor-outer') {
    // Exercise unchanged instrumentor sources even on a clean workspace build.
    // The test-only TS loader avoids introducing generated-output prerequisites.
    const fs = require('node:fs'), path = require('node:path'), Module = require('node:module'), esbuild = require('esbuild');
    const name = mode === 'agents' ? '@traceai/openai-agents' : '@traceai/cursor-sdk';
    const folder = mode === 'agents' ? 'traceai_openai_agents' : 'traceai_cursor_sdk';
    const source = path.resolve(__dirname, '../../../', folder, 'src/index.ts');
    require.extensions['.ts'] = (module, file) => module._compile(esbuild.transformSync(fs.readFileSync(file, 'utf8'), { loader: 'ts', format: 'cjs', target: 'node20' }).code, file);
    const resolve = Module._resolveFilename;
    Module._resolveFilename = function(request, ...args) { return request === name ? source : Reflect.apply(resolve, this, [request, ...args]); };
    let starts = 0, ends = 0, baseline;
    if (mode === 'agents') {
      // Complete SDK startup before app registration: its root import installs a
      // default exporter on its own. The later require is patched from the cache.
      const core = require('@openai/agents');
      core.setTracingDisabled(false); // SDK defaults to off under Jest's inherited NODE_ENV=test.
      core.setTraceProcessors([{ onTraceStart() { starts++; }, onTraceEnd() { ends++; }, onSpanStart() {}, onSpanEnd() {}, async shutdown() {}, async forceFlush() {} }]);
    } else {
      baseline = require('@cursor/sdk').Agent.create;
    }
    auto.init({ ...options, instrumentations: [mode === 'agents' ? 'openai-agents' : 'cursor-sdk'] });
    const target = require(mode === 'agents' ? '@openai/agents' : '@cursor/sdk'), instrumentor = require(name);
    if (mode === 'agents') {
      await target.withTrace('real agents regression', async () => {}); await auto.forceFlush();
      assert.deepEqual({ starts, ends, exported: fi.length, patched: instrumentor.isPatched(), status: auto.report().integrations.find(i => i.name === 'openai-agents').status }, { starts: 1, ends: 1, exported: 1, patched: true, status: 'instrumented' });
    } else { assert.notEqual(target.Agent.create, baseline); assert.equal(instrumentor.isPatched(target.Agent), true); }
    assert.equal(auto.report().integrations.find(i => i.name === (mode === 'agents' ? 'openai-agents' : 'cursor-sdk')).status, 'instrumented');
    let outer;
    if (mode === 'cursor-outer') { const inner = target.Agent.create; outer = function(...args) { return Reflect.apply(inner, this, args); }; target.Agent.create = outer; }
    await auto.shutdown();
    if (mode === 'agents') { assert.equal(instrumentor.isPatched(), false); assert.equal(target._fiPatched, false); await target.withTrace('application remains', async () => {}); assert.equal(starts, 2); assert.equal(ends, 2); assert.equal(app.length, 1); assert.equal(fi.length, 1); }
    else { assert.equal(target.Agent.create, outer || baseline); assert.equal(instrumentor.isPatched(target.Agent), false); }
    console.log(JSON.stringify({ applicationTelemetryPreserved: true, ownedPatchRemoved: true })); return;
  }
  throw new Error('Unknown round4 test mode');
})().catch(error => { console.error(error); process.exitCode = 1; });
