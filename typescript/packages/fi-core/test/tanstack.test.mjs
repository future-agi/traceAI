import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { createRequire } from 'node:module';
import { resolve, join } from 'node:path';
import { pathToFileURL } from 'node:url';
import { test } from 'node:test';

// Each fixture is a clean install of the exact packed core and registry SDKs:
// node test/install-tanstack-fixture.cjs DIR floor|current CORE_TGZ SEMCONV_TGZ
if (!process.env.TANSTACK_FIXTURE) {
  console.error('Set TANSTACK_FIXTURE to a directory prepared by test/install-tanstack-fixture.cjs');
  process.exit(2);
}
const fixture = resolve(process.env.TANSTACK_FIXTURE);
const require = createRequire(join(fixture, 'package.json'));
const native = async (pkg, path = 'index.js') => import(pathToFileURL(join(fixture, 'node_modules', pkg, 'dist/esm', path)));
const { otelMiddleware } = await native('@tanstack/ai', 'middlewares/otel.js');
const { chat, toolDefinition } = await native('@tanstack/ai');
const { createOpenaiChatCompletions } = await native('@tanstack/ai-openai');
const { BasicTracerProvider, SimpleSpanProcessor, BatchSpanProcessor, InMemorySpanExporter } = require('@opentelemetry/sdk-trace-base');
const { SpanStatusCode, context, trace } = require('@opentelemetry/api');
const { resourceFromAttributes } = require('@opentelemetry/resources');
const { OTLPTraceExporter } = require('@opentelemetry/exporter-trace-otlp-http');
const { TransformingSpanExporter } = require('@traceai/fi-core');
const {
  tanStackOtelOptions, normalizeTanStackSpan, registerTanStackTracing,
  flushTraces, shutdownTraces,
} = require('@traceai/fi-core/tanstack');
// Customers on ESM-only TanStack load the ESM build.
const esm = await import(pathToFileURL(join(fixture, 'node_modules/@traceai/fi-core/dist/esm/tanstack.js')));
const esmByName = await import(pathToFileURL(join(fixture, 'esm-import.mjs')));
const secret = 'private-payload-7e925';
const sdkVersion = require('@opentelemetry/sdk-trace-base/package.json').version;
const tanstackVersion = require(join(fixture, 'node_modules/@tanstack/ai/package.json')).version;
console.log(`fixture: @tanstack/ai ${tanstackVersion}, @opentelemetry/sdk-trace-base ${sdkVersion}`);
const spanJSON = spans => JSON.stringify(spans.map(({ name, attributes, status, events, links }) => ({ name, attributes, status, events, links })));

function opaque(tracer) {
  return { startSpan(...args) {
    const span = tracer.startSpan(...args);
    return Object.fromEntries(['spanContext', 'setAttribute', 'setAttributes', 'addEvent', 'addLink', 'addLinks', 'setStatus', 'updateName', 'end', 'isRecording', 'recordException'].map(k => [k, span[k].bind(span)]));
  }, startActiveSpan: tracer.startActiveSpan.bind(tracer) };
}
function memory({ options, publicOnly = true, batch = false, raw = false } = {}) {
  const sink = new InMemorySpanExporter();
  const rawSink = new InMemorySpanExporter();
  const Processor = batch ? BatchSpanProcessor : SimpleSpanProcessor;
  const provider = new BasicTracerProvider({ spanProcessors: [
    new Processor(new TransformingSpanExporter(sink, normalizeTanStackSpan)),
    ...(raw ? [new SimpleSpanProcessor(rawSink)] : []),
  ] });
  const tracer = provider.getTracer('tanstack-contract');
  return { provider, sink, rawSink, tracer,
    middleware: otelMiddleware(tanStackOtelOptions(publicOnly ? opaque(tracer) : tracer, options)),
  };
}
function ctx(extra = {}) {
  return { activity: 'chat', provider: 'openai-chat', model: 'gpt-4o-mini', threadId: 'opaque-session',
    phase: 'beforeModel', iteration: 0, messages: [{ role: 'user', content: secret }], ...extra };
}
const usage = { promptTokens: 11, completionTokens: 7, totalTokens: 18, cost: 0.25,
  promptTokensDetails: { cachedTokens: 3, cacheWriteTokens: 2 }, completionTokensDetails: { reasoningTokens: 1 } };
async function hooks(middleware, { turns = 1, run = ctx(), withUsage = true, fallback } = {}) {
  await middleware.onStart(run);
  for (let i = 0; i < turns; i++) {
    run.iteration = i;
    await middleware.onConfig(run, { messages: run.messages, systemPrompts: [] });
    if (withUsage) await middleware.onUsage(run, usage);
  }
  await middleware.onFinish(run, { duration: 1, ...(fallback ? { usage: fallback } : {}) });
}
const rootOf = spans => spans.find(s => s.attributes['gen_ai.span.kind'] === 'AGENT' || s.attributes['gen_ai.span.kind'] === 'CHAIN');
const sum = (spans, key) => spans.reduce((n, s) => n + (s.attributes[key] ?? 0), 0);

for (const batch of [false, true]) {
  for (const turns of [1, 2]) test(`public-only tracer counts tokens once: turns=${turns}, batch=${batch}`, async () => {
    const h = memory({ batch, raw: true, options: { threadIdAsSession: true } });
    await hooks(h.middleware, { turns });
    await h.provider.forceFlush();
    const spans = h.sink.getFinishedSpans(), root = rootOf(spans);
    assert.equal(spans.length, turns + 1);
    assert.equal(root.attributes['gen_ai.span.kind'], turns === 1 ? 'CHAIN' : 'AGENT');
    for (const [key, value] of Object.entries({ input_tokens: 11, output_tokens: 7, total_tokens: 18, cost: 0.25,
      'cache_read.input_tokens': 3, 'cache_creation.input_tokens': 2, 'reasoning.output_tokens': 1 })) {
      assert.equal(sum(spans, `gen_ai.usage.${key}`), turns * value, key);
      assert.equal(root.attributes[`tanstack.ai.root_usage.${key}`], turns * value, key);
    }
    assert.equal(root.attributes['traceai.tanstack.aggregate_usage'], undefined);
    for (const span of spans) {
      assert.equal(span.attributes['session.id'], 'opaque-session');
      assert.equal(span.attributes['gen_ai.provider.name'], 'openai');
      assert.equal(span.spanContext().traceId, root.spanContext().traceId);
      if (span !== root) assert.equal(span.parentSpanContext?.spanId ?? span.parentSpanId, root.spanContext().spanId);
    }
    const rawRoot = rootOf(h.rawSink.getFinishedSpans());
    assert.equal(rawRoot.attributes['gen_ai.usage.input_tokens'], turns * 11, 'other exporters see original usage');
    assert.equal(rawRoot.attributes['traceai.tanstack.aggregate_usage'], true);
    assert.equal(spanJSON(spans).includes(secret), false);
    await h.provider.shutdown();
  });
}

test('negative control: bare native middleware double counts root usage', async () => {
  const sink = new InMemorySpanExporter();
  const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(sink)] });
  await hooks(otelMiddleware({ tracer: opaque(provider.getTracer('bare')) }), { turns: 2 });
  assert.equal(sum(sink.getFinishedSpans(), 'gen_ai.usage.input_tokens'), 44);
  await provider.shutdown();
});

test('ESM entry point exposes the same maintained API', () => {
  for (const name of ['tanStackOtelOptions', 'normalizeTanStackSpan', 'registerTanStackTracing', 'flushTraces', 'shutdownTraces']) {
    assert.equal(typeof esm[name], 'function', name);
    assert.equal(typeof esmByName[name], 'function', name);
  }
  assert.equal(esm.futureAgiSpanKinds, undefined, 'hooks are bound to the tracer facade, not exported alone');
});

test('normalizer handles frozen ReadableSpan snapshots', () => {
  const frozen = Object.freeze({ name: 'chat', attributes: Object.freeze({ 'traceai.tanstack.aggregate_usage': true, 'gen_ai.usage.input_tokens': 4 }),
    spanContext: () => ({ spanId: 'frozen' }), status: { code: 0 }, links: [], events: [] });
  const output = normalizeTanStackSpan(frozen);
  assert.deepEqual(output.attributes, { 'tanstack.ai.root_usage.input_tokens': 4 });
  assert.equal(output.spanContext().spanId, 'frozen');
});

test('no child usage: retain root-only fallback, including zero usage', async () => {
  for (const fallback of [usage, { promptTokens: 0, completionTokens: 0, totalTokens: 0 }]) {
    const h = memory();
    await hooks(h.middleware, { withUsage: false, fallback });
    await h.provider.forceFlush();
    assert.equal(rootOf(h.sink.getFinishedSpans()).attributes['gen_ai.usage.input_tokens'], fallback.promptTokens);
    assert.equal(rootOf(h.sink.getFinishedSpans()).attributes['session.id'], undefined);
    await h.provider.shutdown();
  }
});

test('shared middleware isolates concurrent runs and session opt-in', async () => {
  const h = memory({ options: { threadIdAsSession: true } });
  await Promise.all(Array.from({ length: 30 }, (_, i) => hooks(h.middleware, { run: ctx({ threadId: `session-${i}` }), turns: i % 2 + 1 })));
  await h.provider.forceFlush();
  const spans = h.sink.getFinishedSpans();
  assert.equal(new Set(spans.map(s => s.spanContext().traceId)).size, 30);
  assert.equal(sum(spans, 'gen_ai.usage.input_tokens'), 45 * 11);
  await h.provider.shutdown();
});

for (const joinActiveTrace of [false, true]) test(`active app parent is ${joinActiveTrace ? 'joined' : 'isolated'}`, async () => {
  const h = memory({ options: { joinActiveTrace } });
  const parent = h.tracer.startSpan('application');
  // A context-manager-free tracer still gets the explicit current parent.
  const withParent = { startSpan: (name, options, explicit) => h.tracer.startSpan(name, options, explicit ?? trace.setSpan(context.active(), parent)),
    startActiveSpan: h.tracer.startActiveSpan.bind(h.tracer) };
  await hooks(otelMiddleware(tanStackOtelOptions(withParent, { joinActiveTrace })));
  await h.provider.forceFlush();
  const root = rootOf(h.sink.getFinishedSpans());
  assert.equal(root.spanContext().traceId === parent.spanContext().traceId, joinActiveTrace);
  parent.end();
  await h.provider.shutdown();
});

for (const captureContent of [false, true]) test(`model and tool errors obey captureContent=${captureContent}`, async () => {
  const h = memory({ options: { captureContent, redact: text => text.replaceAll(secret, '[hidden]') } });
  const run = ctx();
  await h.middleware.onStart(run);
  await h.middleware.onConfig(run, { messages: run.messages, systemPrompts: [] });
  await h.middleware.onBeforeToolCall(run, { toolName: 'get_weather', toolCallId: 'call-1', args: { city: secret } });
  const error = Object.assign(new Error(secret), { code: 'SYNTHETIC' });
  await h.middleware.onAfterToolCall(run, { toolName: 'get_weather', toolCallId: 'call-1', ok: false, error, result: secret });
  await h.middleware.onError(run, { error, duration: 1 });
  await h.provider.forceFlush();
  const spans = h.sink.getFinishedSpans();
  assert.equal(spans.length, 3);
  assert.equal(spans.filter(s => s.status.code === SpanStatusCode.ERROR).length, 3);
  const encoded = spanJSON(spans);
  assert.ok(!encoded.includes(secret));
  assert.ok(!encoded.includes('exception.stacktrace'));
  assert.equal(encoded.includes('[hidden]'), captureContent);
  assert.ok(encoded.includes('SYNTHETIC'));
  await h.provider.shutdown();
});

test('redactor failures fail closed for content and exceptions', async () => {
  const h = memory({ options: { captureContent: true, redact: () => { throw new Error(secret); } } });
  const run = ctx();
  await h.middleware.onStart(run);
  await h.middleware.onConfig(run, { messages: run.messages, systemPrompts: [] });
  await h.middleware.onError(run, { error: new Error(secret), duration: 1 });
  await h.provider.forceFlush();
  const encoded = spanJSON(h.sink.getFinishedSpans());
  assert.ok(encoded.includes('[redaction_failed]'));
  assert.ok(!encoded.includes(secret));
  await h.provider.shutdown();
});

test('all active-span overloads apply the same privacy facade', async () => {
  const h = memory();
  const { tracer } = tanStackOtelOptions(h.tracer);
  const callback = span => { span.recordException(new Error(secret)); span.setStatus({ code: SpanStatusCode.ERROR, message: secret }); span.end(); return 42; };
  assert.equal(tracer.startActiveSpan('a', callback), 42);
  assert.equal(tracer.startActiveSpan('b', {}, callback), 42);
  assert.equal(tracer.startActiveSpan('c', {}, context.active(), callback), 42);
  await h.provider.forceFlush();
  assert.equal(h.sink.getFinishedSpans().length, 3);
  assert.ok(!spanJSON(h.sink.getFinishedSpans()).includes(secret));
  await h.provider.shutdown();
});

test('normalization is immutable, idempotent and scoped; preserves future cost keys', () => {
  const attributes = Object.freeze({ 'traceai.tanstack.aggregate_usage': true,
    'gen_ai.cost.total': 1.25, 'gen_ai.usage.future_tokens': 6, unrelated: 'keep' });
  const span = { attributes, spanContext() { assert.equal(this, span); return { spanId: 'id' }; } };
  const output = normalizeTanStackSpan(span);
  assert.deepEqual(output.attributes, { 'tanstack.ai.root_usage.cost.total': 1.25, 'tanstack.ai.root_usage.future_tokens': 6, unrelated: 'keep' });
  assert.deepEqual(output.spanContext(), { spanId: 'id' });
  assert.equal(normalizeTanStackSpan(output), output);
  assert.equal(span.attributes, attributes);
  const unrelated = { attributes: { 'gen_ai.usage.input_tokens': 12 } };
  assert.equal(normalizeTanStackSpan(unrelated), unrelated);
});

test('export-transform failure drops the batch without forwarding private text', async () => {
  let exports = 0, flushes = 0, shutdowns = 0;
  const exporter = new TransformingSpanExporter({ export() { exports++; }, async forceFlush() { flushes++; }, async shutdown() { shutdowns++; } }, () => { throw new Error(secret); });
  let result;
  exporter.export([{}], r => { result = r; });
  assert.equal(exports, 0);
  assert.equal(result.code, 1);
  assert.ok(!result.error.message.includes(secret));
  await exporter.forceFlush(); await exporter.shutdown();
  assert.equal(flushes, 1); assert.equal(shutdowns, 1);
});

test('bounded flush and shutdown tolerate collector failures', async () => {
  await flushTraces({ forceFlush: async () => { throw new Error(secret); } });
  await flushTraces({ forceFlush: () => new Promise(() => {}) }, { timeoutMs: 5 });
  await shutdownTraces({ shutdown: async () => { throw new Error(secret); } });
});

async function server(handler) {
  const server = createServer(handler);
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  return { url: `http://127.0.0.1:${server.address().port}`, close: () => new Promise(resolve => { server.closeAllConnections(); server.close(resolve); }) };
}
async function body(req) { const parts = []; for await (const part of req) parts.push(part); return JSON.parse(Buffer.concat(parts)); }
const attrs = pairs => Object.fromEntries((pairs ?? []).map(({ key, value }) => [key, value.stringValue ?? value.boolValue ?? Number(value.intValue ?? value.doubleValue)]));

for (const mode of ['tools', 'tool-error', 'model-error', 'abort', 'content']) test(`real streamed chat and OTLP wire: ${mode}`, async () => {
  const requests = [], bodies = [], wire = [];
  const collector = await server(async (req, res) => {
    requests.push({ path: req.url, headers: req.headers });
    const payload = await body(req);
    bodies.push(payload);
    for (const resource of payload.resourceSpans) {
      assert.equal(attrs(resource.resource.attributes).project_name, 'tanstack-contract');
      for (const scope of resource.scopeSpans) wire.push(...scope.spans);
    }
    res.writeHead(200, { 'content-type': 'application/json' }); res.end('{}');
  });
  const model = await server(async (req, res) => {
    const request = await body(req);
    if (mode === 'model-error') {
      res.writeHead(400, { 'content-type': 'application/json' });
      res.end(JSON.stringify({ error: { message: secret, type: 'invalid_request_error', code: 'SYNTHETIC' } })); return;
    }
    const usedTool = request.messages.some(m => m.role === 'tool');
    const wantsTool = ['tools', 'tool-error'].includes(mode) && !usedTool;
    const event = (delta, finish_reason = null, usage) => `data: ${JSON.stringify({ id: 'synthetic', object: 'chat.completion.chunk', created: 1700000000, model: 'gpt-4o-mini',
      choices: delta === null ? [] : [{ index: 0, delta, finish_reason }], ...(usage ? { usage } : {}) })}\n\n`;
    res.writeHead(200, { 'content-type': 'text/event-stream' });
    if (wantsTool) {
      res.write(event({ role: 'assistant', tool_calls: [{ index: 0, id: 'call-1', type: 'function', function: { name: 'get_weather', arguments: JSON.stringify({ city: secret }) } }] }));
    } else res.write(event({ role: 'assistant', content: secret }));
    if (mode === 'abort') return; // client's abort closes this connection
    res.end(event({}, wantsTool ? 'tool_calls' : 'stop') + event(null, null, { prompt_tokens: 11, completion_tokens: 7, total_tokens: 18 }) + 'data: [DONE]\n\n');
  });
  const provider = registerTanStackTracing({ projectName: 'tanstack-contract', endpoint: collector.url,
    headers: { 'x-api-key': 'synthetic-key', 'x-secret-key': 'synthetic-secret' } });
  try {
    const abortController = new AbortController();
    const middleware = otelMiddleware(tanStackOtelOptions(provider.getTracer('tanstack'), {
      threadIdAsSession: true, captureContent: mode === 'content', redact: text => text.replaceAll(secret, '[hidden]'),
    }));
    const weather = toolDefinition({ name: 'get_weather', description: 'Get weather', inputSchema: { type: 'object', properties: { city: { type: 'string' } }, required: ['city'] } })
      .server(async ({ city }) => { if (mode === 'tool-error') throw new Error(secret); return { city, forecast: 'sunny' }; });
    const stream = chat({ adapter: createOpenaiChatCompletions('gpt-4o-mini', 'synthetic-model-key', { baseURL: `${model.url}/v1`, maxRetries: 0 }),
      messages: [{ role: 'user', content: secret }], tools: ['tools', 'tool-error'].includes(mode) ? [weather] : [],
      middleware: [middleware], threadId: 'session-wire', abortController });
    const chunks = [];
    for await (const chunk of stream) {
      chunks.push(chunk);
      if (mode === 'abort' && chunk.type === 'TEXT_MESSAGE_CONTENT') abortController.abort();
    }
    await provider.forceFlush();
    assert.equal(wire.length, ['tools', 'tool-error'].includes(mode) ? 4 : 2);
    assert.equal(new Set(wire.map(s => s.spanId)).size, wire.length);
    const spans = wire.map(s => ({ ...s, attributes: attrs(s.attributes) }));
    const root = rootOf(spans);
    assert.ok(root);
    assert.equal(root.parentSpanId ?? '', '');
    for (const span of spans) {
      assert.equal(span.traceId, root.traceId);
      assert.equal(span.attributes['session.id'], 'session-wire');
      if (span !== root) assert.ok(spans.some(p => p.spanId === span.parentSpanId));
    }
    for (const req of requests) {
      assert.equal(req.path, '/tracer/v1/traces');
      assert.equal(req.headers['x-api-key'], 'synthetic-key');
      assert.equal(req.headers['x-secret-key'], 'synthetic-secret');
    }
    const encoded = JSON.stringify(bodies);
    assert.ok(!encoded.includes(secret));
    assert.ok(!encoded.includes('exception.stacktrace'));
    assert.ok(!encoded.includes('synthetic-key'));
    if (mode === 'content') assert.ok(encoded.includes('[hidden]'));
    if (['tools', 'tool-error', 'content'].includes(mode)) assert.equal(sum(spans, 'gen_ai.usage.input_tokens'), mode === 'content' ? 11 : 22);
    if (mode === 'model-error') assert.ok(spans.every(s => s.status.code === 2));
    if (mode === 'tool-error') assert.equal(spans.filter(s => s.status.code === 2).length, 1);
    if (mode === 'abort') assert.ok(spans.every(s => s.status.code === 2 && s.status.message === 'cancelled'));
  } finally { await provider.shutdown(); await model.close(); await collector.close(); }
});

test('existing SDK can compose the normalizer without replacing its provider', async () => {
  const h = memory();
  const other = { attributes: { unrelated: true } };
  assert.equal(normalizeTanStackSpan(other), other);
  await hooks(h.middleware);
  await h.provider.forceFlush();
  assert.equal(sum(h.sink.getFinishedSpans(), 'gen_ai.usage.input_tokens'), 11);
  await h.provider.shutdown();
});
