// Exercise the built public API with the real OTel SDK and a loopback OTLP collector.
const { test, afterEach } = require('node:test');
const assert = require('node:assert/strict');
const http = require('node:http');
const path = require('node:path');
const { createRequire } = require('node:module');
const load = createRequire(process.env.FI_CORE_PACKAGE_JSON || path.resolve(__dirname, '../package.json'));
// Never inherit credentials or collector settings from a developer's shell.
for (const key of Object.keys(process.env)) {
  if (key.startsWith('FI_') || key.startsWith('OTEL_')) delete process.env[key];
}
const { context, trace, propagation, createContextKey, ROOT_CONTEXT } = load('@opentelemetry/api');
const { AsyncLocalStorageContextManager } = load('@opentelemetry/context-async-hooks');
const { suppressTracing } = load('@opentelemetry/core');
const { NodeTracerProvider } = load('@opentelemetry/sdk-trace-node');
const { InMemorySpanExporter, SimpleSpanProcessor } = load('@opentelemetry/sdk-trace-base');
const { register, FITracerProvider, FITracer, setSession, getSession, ProjectType } = load('./dist/src');

const providers = [];
const servers = [];
afterEach(async () => {
  await Promise.all(providers.splice(0).map(provider => provider.shutdown()));
  await Promise.all(servers.splice(0).map(server => new Promise(resolve => server.close(resolve))));
  trace.disable();
  context.disable();
  propagation.disable();
});

async function collector() {
  const requests = [];
  const server = http.createServer((request, response) => {
    const chunks = [];
    request.on('data', chunk => chunks.push(chunk));
    request.on('end', () => {
      requests.push({ path: request.url, body: JSON.parse(Buffer.concat(chunks).toString()) });
      response.writeHead(200, { 'Content-Type': 'application/json' });
      response.end('{}');
    });
  });
  servers.push(server);
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  return { endpoint: `http://127.0.0.1:${server.address().port}`, requests,
    spans: () => requests.flatMap(request => request.body.resourceSpans.flatMap(resource =>
      resource.scopeSpans.flatMap(scope => scope.spans))) };
}

function setup(options = {}) {
  const provider = register({ projectName: 'offline-core', projectType: ProjectType.OBSERVE,
    endpoint: 'http://127.0.0.1:9', headers: {}, ...options });
  providers.push(provider);
  return provider;
}
function memoryProcessor() {
  const exporter = new InMemorySpanExporter();
  return { exporter, processor: new SimpleSpanProcessor(exporter) };
}

for (const batch of [false, true]) {
  test(`register preserves async parents on the OTLP wire (batch=${batch})`, async () => {
    const receiver = await collector();
    const provider = setup({ endpoint: receiver.endpoint, batch });
    const tracer = trace.getTracer('public-api');
    let activeBefore, activeAfter, parentId, traceId;
    await tracer.startActiveSpan('parent', async parent => {
      parentId = parent.spanContext().spanId;
      traceId = parent.spanContext().traceId;
      activeBefore = trace.getActiveSpan() === parent;
      await new Promise(resolve => setImmediate(resolve));
      activeAfter = trace.getActiveSpan() === parent;
      tracer.startSpan('child').end();
      parent.end();
    });
    await provider.forceFlush();
    assert.equal(activeBefore, true);
    assert.equal(activeAfter, true);
    const spans = receiver.spans();
    assert.equal(spans.length, 2);
    const child = spans.find(span => span.name === 'child');
    assert.equal(child.parentSpanId, parentId);
    assert.equal(child.traceId, traceId);
    assert.equal(receiver.requests[0].path, '/tracer/v1/traces');
    assert.equal(trace.getActiveSpan(), undefined);
  });
}

test('batch:true queues spans until forceFlush, rather than exporting immediately', async () => {
  const receiver = await collector();
  const provider = setup({ endpoint: receiver.endpoint, batch: true });
  provider.getTracer('batch').startSpan('queued').end();
  await new Promise(resolve => setTimeout(resolve, 100));
  assert.equal(receiver.requests.length, 0);
  await provider.forceFlush();
  assert.deepEqual(receiver.spans().map(span => span.name), ['queued']);
});

test('legacy addSpanProcessor replaces the default and works for already-created tracers', async () => {
  const receiver = await collector();
  const provider = setup({ endpoint: receiver.endpoint });
  const tracer = provider.getTracer('legacy');
  const first = memoryProcessor();
  const second = memoryProcessor();
  provider.addSpanProcessor(first.processor);
  provider.addSpanProcessor(second.processor);
  tracer.startSpan('custom').end();
  await provider.forceFlush();
  assert.equal(first.exporter.getFinishedSpans().length, 1);
  assert.equal(second.exporter.getFinishedSpans().length, 1);
  assert.equal(receiver.requests.length, 0);
});

test('replacement retains pending default batches for flush and shutdown', async () => {
  const receiver = await collector();
  const provider = setup({ endpoint: receiver.endpoint, batch: true });
  const tracer = provider.getTracer('replace');
  tracer.startSpan('before-replacement').end();
  const custom = memoryProcessor();
  provider.addSpanProcessor(custom.processor);
  tracer.startSpan('after-replacement').end();
  await provider.shutdown();
  assert.deepEqual(receiver.spans().map(span => span.name), ['before-replacement']);
});

test('constructor processors compose multiple exporters without a hidden default exporter', async () => {
  const receiver = await collector();
  const first = memoryProcessor();
  const second = memoryProcessor();
  const provider = setup({ endpoint: receiver.endpoint, spanProcessors: [first.processor, second.processor] });
  provider.getTracer('composed').startSpan('one-span').end();
  await provider.forceFlush();
  assert.equal(first.exporter.getFinishedSpans().length, 1);
  assert.equal(second.exporter.getFinishedSpans().length, 1);
  assert.equal(receiver.requests.length, 0);
});

test('FITracerProvider accepts an empty constructor processor list', async () => {
  const receiver = await collector();
  const provider = new FITracerProvider({ endpoint: receiver.endpoint, headers: {}, spanProcessors: [] });
  providers.push(provider);
  provider.getTracer('empty').startSpan('not-exported').end();
  await provider.forceFlush();
  assert.equal(receiver.requests.length, 0);
});

test('session and suppression context survive await without manually installing a manager', async () => {
  const memory = memoryProcessor();
  const provider = setup({ spanProcessors: [memory.processor] });
  const tracer = new FITracer({ tracer: provider.getTracer('context') });
  const session = setSession(context.active(), { sessionId: 'synthetic-session' });
  let sessionAfter;
  await context.with(session, async () => {
    await new Promise(resolve => setImmediate(resolve));
    sessionAfter = getSession(context.active());
    tracer.startActiveSpan('session-span', span => span.end());
    await context.with(suppressTracing(context.active()), async () => {
      await Promise.resolve();
      tracer.startSpan('suppressed').end();
    });
  });
  await provider.forceFlush();
  assert.deepEqual(sessionAfter, { sessionId: 'synthetic-session' });
  const spans = memory.exporter.getFinishedSpans();
  assert.equal(spans.length, 1);
  assert.equal(spans[0].attributes['session.id'], 'synthetic-session');
  assert.equal(getSession(context.active()), undefined);
});

test('an application context manager and propagator are reused, not replaced or disabled', async () => {
  const manager = new AsyncLocalStorageContextManager().enable();
  let enableCalls = 0, disableCalls = 0;
  const originalDisable = manager.disable.bind(manager);
  manager.enable = () => { enableCalls++; return manager; };
  manager.disable = () => { disableCalls++; return originalDisable(); };
  assert.equal(context.setGlobalContextManager(manager), true);
  const marker = createContextKey('application-marker');
  propagation.setGlobalPropagator({ fields: () => ['app-header'], inject() {}, extract: ctx => ctx });
  await context.with(ROOT_CONTEXT.setValue(marker, 'kept'), async () => {
    const provider = setup({ spanProcessors: [] });
    await Promise.resolve();
    assert.equal(context.active().getValue(marker), 'kept');
    await provider.shutdown();
    assert.equal(context.active().getValue(marker), 'kept');
  });
  assert.equal(enableCalls, 0);
  assert.equal(disableCalls, 0);
  assert.deepEqual(propagation.fields(), ['app-header']);
});

test('register does not replace an existing application provider or duplicate its exports', async () => {
  const appMemory = memoryProcessor();
  const appProvider = new NodeTracerProvider({ spanProcessors: [appMemory.processor] });
  providers.push(appProvider);
  appProvider.register();
  const fiMemory = memoryProcessor();
  const fiProvider = setup({ spanProcessors: [fiMemory.processor] });
  await trace.getTracer('application').startActiveSpan('app-parent', async parent => {
    await Promise.resolve();
    trace.getTracer('application').startSpan('app-child').end();
    parent.end();
  });
  await appProvider.forceFlush();
  await fiProvider.forceFlush();
  assert.equal(appMemory.exporter.getFinishedSpans().length, 2);
  assert.equal(fiMemory.exporter.getFinishedSpans().length, 0);
});

test('setGlobalTracerProvider:false leaves all global state untouched', async () => {
  const marker = createContextKey('isolated-marker');
  const before = trace.getTracerProvider();
  const provider = setup({ setGlobalTracerProvider: false, spanProcessors: [] });
  assert.equal(trace.getTracerProvider(), before);
  assert.equal(context.with(ROOT_CONTEXT.setValue(marker, true), () => context.active().getValue(marker)), undefined);
  assert.deepEqual(propagation.fields(), []);
  await provider.shutdown();
});

test('forceFlush and shutdown reach all configured processors exactly once', async () => {
  const calls = [];
  const processor = name => ({
    onStart() { calls.push(`${name}:start`); }, onEnd() { calls.push(`${name}:end`); },
    async forceFlush() { calls.push(`${name}:flush`); },
    async shutdown() { calls.push(`${name}:shutdown`); }
  });
  const provider = setup({ spanProcessors: [processor('first')] });
  provider.addSpanProcessor(processor('second'));
  provider.getTracer('lifecycle').startSpan('tracked').end();
  await provider.forceFlush();
  await provider.shutdown();
  await provider.shutdown();
  for (const name of ['first', 'second']) {
    for (const hook of ['start', 'end', 'flush', 'shutdown']) {
      assert.equal(calls.filter(call => call === `${name}:${hook}`).length, 1, `${name}:${hook}`);
    }
  }
});

test('shutdown drains the default batch even without forceFlush', async () => {
  const receiver = await collector();
  const provider = setup({ endpoint: receiver.endpoint, batch: true });
  provider.getTracer('shutdown').startSpan('last-span').end();
  await provider.shutdown();
  assert.deepEqual(receiver.spans().map(span => span.name), ['last-span']);
});

test('active FITracer privacy survives async context on the OTLP wire', async () => {
  const receiver = await collector();
  const provider = setup({ endpoint: receiver.endpoint, batch: true });
  const tracer = new FITracer({ tracer: provider.getTracer('privacy'), traceConfig: { hideErrorDetails: true } });
  await context.with(setSession(context.active(), { sessionId: 'wire-session' }), async () => {
    await tracer.startActiveSpan('private-parent', async span => {
      await Promise.resolve();
      trace.getActiveSpan().recordException(new Error('synthetic-secret-error'));
      trace.getActiveSpan().setStatus({ code: 2, message: 'synthetic-secret-status' });
      tracer.startActiveSpan('private-child', child => child.end());
      span.end();
    });
  });
  await provider.forceFlush();
  const spans = receiver.spans();
  assert.equal(spans.length, 2);
  for (const span of spans) {
    assert.ok(span.attributes.some(attribute => attribute.key === 'session.id' && attribute.value.stringValue === 'wire-session'));
  }
  assert.equal(JSON.stringify(receiver.requests).includes('synthetic-secret'), false);
  assert.equal(spans.find(span => span.name === 'private-parent').status.code, 2);
});

test('gRPC batching uses the chosen processor and shuts down its channel', async () => {
  const grpc = load('@grpc/grpc-js');
  const server = new grpc.Server();
  const payloads = [];
  server.addService({ export: {
    path: '/opentelemetry.proto.collector.trace.v1.TraceService/Export',
    requestStream: false, responseStream: false,
    requestDeserialize: bytes => bytes, responseSerialize: bytes => bytes,
  } }, { export: (call, callback) => { payloads.push(call.request); callback(null, Buffer.alloc(0)); } });
  const port = await new Promise((resolve, reject) => server.bindAsync('127.0.0.1:0',
    grpc.ServerCredentials.createInsecure(), (error, bound) => error ? reject(error) : resolve(bound)));
  try {
    const provider = setup({ endpoint: `http://127.0.0.1:${port}`, transport: 'grpc', batch: true });
    provider.getTracer('grpc').startSpan('grpc-queued').end();
    await new Promise(resolve => setTimeout(resolve, 100));
    assert.equal(payloads.length, 0);
    await provider.shutdown();
    assert.equal(payloads.length, 1);
    assert.ok(payloads[0].includes(Buffer.from('grpc-queued')));
  } finally {
    server.forceShutdown();
  }
});

test('a failing processor does not starve sibling hooks or lifecycle calls', async () => {
  const calls = [];
  const failure = new Error('synthetic processor failure');
  const provider = setup({ spanProcessors: [{
    onStart() { throw failure; }, onEnd() { throw failure; },
    forceFlush() { throw failure; }, shutdown() { throw failure; }
  }, {
    onStart() { calls.push('start'); }, onEnd() { calls.push('end'); },
    async forceFlush() { calls.push('flush'); }, async shutdown() { calls.push('shutdown'); }
  }] });
  provider.getTracer('failure').startSpan('still-delivered').end();
  await assert.rejects(provider.forceFlush());
  await assert.rejects(provider.shutdown());
  providers.splice(providers.indexOf(provider), 1);
  assert.deepEqual(calls, ['start', 'end', 'flush', 'shutdown']);
});

test('optional onEnding matches the installed SDK lifecycle', async () => {
  const nativeCalls = [], fiCalls = [];
  const processor = calls => ({
    onStart() { calls.push('start'); }, onEnding() { calls.push('ending'); },
    onEnd() { calls.push('end'); }, async forceFlush() {}, async shutdown() {},
  });
  const native = new NodeTracerProvider({ spanProcessors: [processor(nativeCalls)] });
  providers.push(native);
  native.getTracer('native').startSpan('native-lifecycle').end();
  const fi = setup({ spanProcessors: [processor(fiCalls)] });
  fi.getTracer('fi').startSpan('fi-lifecycle').end();
  assert.ok(nativeCalls.length >= 2);
  assert.deepEqual(fiCalls, nativeCalls);
});
