// An application with its own OpenTelemetry provider and AI calls. MODE picks the case:
// app-after  the application starts its SDK after the agent's preload
// app-first  the application's SDK is preloaded first (-r app-otel.cjs)
// forward    as app-first, with an application span that another AI tracer wrote
// plain-request  as app-first, with a plain request to a non-AI URL next to the AI call
// integration-status  print report() for the integration named by STATUS_OF
// globals    an explicit start with no application SDK
// keys-only  FI keys in the environment and the package loaded, but never started
const { trace, context, propagation } = require('@opentelemetry/api');
const http = require('node:http');
const mode = process.env.MODE;
const PRIVATE = { prompt: 'PRIVATE-FOREIGN-PROMPT', answer: 'PRIVATE-FOREIGN-ANSWER', tool: 'PRIVATE-FOREIGN-TOOL' };
async function ai() {
  const response = await fetch(process.env.MOCK_URL + '/v1/chat/completions', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ model: 'gpt-fixture', messages: [{ role: 'user', content: 'hello' }] }) });
  await response.text();
}
// The test server answers this non-AI route with the request headers it received.
function plain() {
  return new Promise((resolve, reject) => http.get(process.env.MOCK_URL + '/echo-headers', { headers: { 'x-plain': 'application' } }, res => {
    const chunks = []; res.on('data', chunk => chunks.push(chunk)); res.on('error', reject);
    res.on('end', () => { try { resolve(JSON.parse(Buffer.concat(chunks).toString())); } catch (error) { reject(error); } });
  }).on('error', reject));
}
function globals() {
  return {
    trace: (trace.getTracerProvider().getDelegate?.() ?? trace.getTracerProvider()).constructor.name,
    context: context._getContextManager().constructor.name,
    propagation: propagation._getGlobalPropagator().constructor.name,
  };
}
(async () => {
  if (mode === 'keys-only') {
    const fetchBefore = globalThis.fetch, emitBefore = process.emit;
    const auto = require('@traceai/auto'); require('@traceai/fi-core');
    await ai();
    console.log(JSON.stringify({ active: auto.report().active, fetchUnchanged: globalThis.fetch === fetchBefore, emitUnchanged: process.emit === emitBefore, sigterm: process.listenerCount('SIGTERM'), globals: globals() }));
    return;
  }
  if (mode === 'ignored-provider') {
    const { BasicTracerProvider } = require('@opentelemetry/sdk-trace-base');
    const auto = require('@traceai/auto'), supplied = new BasicTracerProvider();
    const returned = auto.init({ provider: supplied });
    console.log(JSON.stringify({ returnedSupplied: returned === supplied, active: auto.report().active }));
    await auto.shutdown();
    return;
  }
  if (mode === 'integration-status') {
    const status = require('@traceai/auto').report().integrations.find(integration => integration.name === process.env.STATUS_OF);
    console.log(JSON.stringify({ status: status?.status ?? null, reason: status?.reason ?? null }));
    return;
  }
  if (mode === 'globals') {
    await ai();
    console.log(JSON.stringify({ active: require('@traceai/auto').report().active, globals: globals() }));
    return;
  }
  if (mode === 'app-after') require('./app-otel.cjs');
  const app = globalThis.__app;
  // Ordinary application code: the global API, as with NodeSDK.
  const tracer = trace.getTracer('own-app');
  const work = {};
  await tracer.startActiveSpan('app-work', { attributes: { 'db.system': 'postgresql', 'app.order': 'order-1' } }, async span => {
    work.spanId = span.spanContext().spanId; work.traceId = span.spanContext().traceId;
    if (mode === 'forward') {
      // Prompt and response keys as other AI tracers write them on application spans.
      span.setAttributes({
        'ai.prompt.messages': JSON.stringify([{ role: 'user', content: PRIVATE.prompt }]), 'ai.response.text': PRIVATE.answer, 'ai.response.model': 'gpt-fixture',
        'ai.toolCall.args': JSON.stringify({ q: PRIVATE.tool }), 'ai.toolCall.result': PRIVATE.tool,
        'gen_ai.prompt.0.content': PRIVATE.prompt, 'gen_ai.completion.0.content': PRIVATE.answer,
        'llm.prompts': [PRIVATE.prompt], 'traceloop.entity.input': PRIVATE.prompt, 'traceloop.entity.output': PRIVATE.answer,
      });
    }
    await ai();
    if (mode === 'plain-request') work.plainHeaders = await plain();
    span.end();
  });
  if (process.env.APP_HTTP === 'true') {
    const server = http.createServer(async (_req, res) => { await ai(); res.end('ok'); });
    await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
    await new Promise((resolve, reject) => http.get(`http://127.0.0.1:${server.address().port}/handler`, res => { res.resume(); res.on('end', resolve); }).on('error', reject));
    server.closeAllConnections(); await new Promise(resolve => server.close(resolve));
  }
  process.on('exit', () => {
    const spans = app?.spans ?? [];
    console.log(JSON.stringify({
      registered: app?.registered ?? null, work,
      appSpans: spans.map(span => ({ name: span.name, kind: span.kind, layer: span.attributes['traceai.capture.layer'] ?? null })),
      appContent: spans.some(span => span.attributes['ai.response.text'] === PRIVATE.answer),
      http: require('@traceai/auto').report().integrations.find(integration => integration.name === 'http') ?? null,
    }));
  });
})().catch(error => { console.error(error); process.exitCode = 1; });
