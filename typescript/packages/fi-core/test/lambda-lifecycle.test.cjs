const { test, afterEach } = require('node:test');
const assert = require('node:assert/strict');
const http = require('node:http');
const path = require('node:path');
const { createRequire } = require('node:module');
const load = createRequire(process.env.FI_CORE_PACKAGE_JSON || path.resolve(__dirname, '../package.json'));
for (const key of Object.keys(process.env)) if (/^(FI_|OTEL_)/.test(key)) delete process.env[key];
const { context, diag, DiagLogLevel, trace, ROOT_CONTEXT, propagation } = load('@opentelemetry/api');
const { NodeTracerProvider } = load('@opentelemetry/sdk-trace-node');
const { SimpleSpanProcessor, BatchSpanProcessor, InMemorySpanExporter } = load('@opentelemetry/sdk-trace-base');
const { OTLPTraceExporter } = load('@opentelemetry/exporter-trace-otlp-proto');
const proto = load('@opentelemetry/otlp-transformer/build/src/generated/root').opentelemetry.proto.collector.trace.v1.ExportTraceServiceRequest;
const { traceLambdaHandler, FITracer, register, ProjectType } = load('@traceai/fi-core');
const providers = [], servers = [];
const invocation = (remaining = 10000) => ({ functionName: 'orders', awsRequestId: 'synthetic-id', getRemainingTimeInMillis: () => remaining });
const parent = '00-11111111111111111111111111111111-2222222222222222-01';
const httpEvent = (headers = { traceparent: parent }) => ({ version: '2.0', requestContext: { http: { method: 'GET' } }, headers });
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
function memory() {
  const exporter = new InMemorySpanExporter();
  const provider = new NodeTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
  provider.register(); providers.push(provider);
  return { provider, exporter, spans: () => exporter.getFinishedSpans() };
}
async function collector(mode = 'success') {
  const requests = [];
  const server = http.createServer((req, res) => {
    const chunks = [];
    req.on('data', chunk => chunks.push(chunk));
    req.on('end', () => {
      const bytes = Buffer.concat(chunks);
      requests.push({ path: req.url, headers: req.headers, bytes, data: proto.toObject(proto.decode(bytes), { bytes: String, longs: String }) });
      if (mode === 'stall') return;
      const respond = () => { res.writeHead(mode === 'reject' ? 400 : 200, { 'Content-Type': 'application/x-protobuf' }); res.end(); };
      if (mode === 'delay') setTimeout(respond, 60); else respond();
    });
  });
  servers.push(server);
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  return { url: `http://127.0.0.1:${server.address().port}/tracer/v1/traces`, requests,
    spans: () => requests.flatMap(r => r.data.resourceSpans.flatMap(r => r.scopeSpans.flatMap(s => s.spans))) };
}
function wired(receiver) {
  const exporter = new OTLPTraceExporter({ url: receiver.url, headers: { 'x-api-key': 'synthetic-api', 'x-secret-key': 'synthetic-secret' }, timeoutMillis: 120 });
  const provider = register({ projectName: 'lambda-test', projectType: ProjectType.OBSERVE,
    spanProcessors: [new BatchSpanProcessor(exporter, { scheduledDelayMillis: 60000, exportTimeoutMillis: 200 })] });
  providers.push(provider);
  return provider;
}
afterEach(async () => {
  await Promise.all(providers.splice(0).map(p => p.shutdown().catch(() => {})));
  for (const server of servers.splice(0)) { server.closeAllConnections(); await new Promise(r => server.close(r)); }
  trace.disable(); context.disable(); propagation.disable(); diag.disable();
  for (const k of Object.keys(process.env)) if (/^(FI_HIDE_|_X_AMZN_TRACE_ID)/.test(k)) delete process.env[k];
});

test('cold/warm invocation roots and FI children survive await and restore application context', async () => {
  const receiver = await collector();
  const provider = wired(receiver);
  const tracer = new FITracer({ tracer: provider.getTracer('model-test') });
  const before = trace.getTracerProvider();
  const handler = traceLambdaHandler(async event => {
    const root = trace.getActiveSpan();
    await Promise.all([0, 1].map(async index => {
      await delay(1);
      assert.equal(trace.getActiveSpan(), root);
      tracer.startActiveSpan(`child-${index}`, child => child.end());
    }));
    return event;
  }, { provider });
  const result = { answer: 42 };
  assert.equal(await handler(result, invocation()), result);
  await handler({}, invocation());
  assert.equal(trace.getActiveSpan(), undefined);
  assert.equal(trace.getTracerProvider(), before);
  const spans = receiver.spans();
  const roots = spans.filter(s => s.name === 'aws.lambda.invoke');
  assert.equal(roots.length, 2);
  assert.notEqual(roots[0].traceId, roots[1].traceId);
  assert.deepEqual(roots.map(s => s.attributes.find(a => a.key === 'faas.coldstart').value.boolValue), [true, false]);
  for (const root of roots) {
    assert.equal(root.attributes.find(a => a.key === 'fi.span.kind').value.stringValue, 'CHAIN');
    const children = spans.filter(s => s.parentSpanId === root.spanId);
    assert.equal(children.length, 2);
    for (const child of children) assert.equal(child.traceId, root.traceId);
  }
  for (const request of receiver.requests) {
    assert.equal(request.path, '/tracer/v1/traces');
    assert.equal(request.headers['x-api-key'], 'synthetic-api');
    assert.equal(request.headers['x-secret-key'], 'synthetic-secret');
    assert.match(request.headers['content-type'], /application\/x-protobuf/);
    const attrs = Object.fromEntries(request.data.resourceSpans[0].resource.attributes.map(a => [a.key, a.value.stringValue]));
    assert.equal(attrs.project_name, 'lambda-test'); assert.equal(attrs.project_type, 'observe');
  }
});

for (const [name, event] of [
  ['REST v1 mixed case', { httpMethod: 'GET', requestContext: {}, headers: { TraceParent: parent } }],
  ['HTTP v2', httpEvent()], ['function URL', { ...httpEvent(), requestContext: { http: {}, domainName: 'synthetic.lambda-url.invalid' } }],
  ['ALB multivalue', { requestContext: { elb: {} }, multiValueHeaders: { TRACEPARENT: [parent] } }],
  ['v1 identical copies', { httpMethod: 'GET', requestContext: {}, headers: { traceparent: parent }, multiValueHeaders: { traceparent: [parent] } }],
]) test(`W3C parent extraction: ${name}`, async () => {
  const m = memory();
  await traceLambdaHandler(async () => {}, { provider: m.provider })(event, invocation());
  const span = m.spans()[0];
  assert.equal(span.spanContext().traceId, '11111111111111111111111111111111');
  assert.equal(span.parentSpanContext?.spanId ?? span.parentSpanId, '2222222222222222');
});

for (const [name, event] of [
  ['direct', { headers: { traceparent: parent } }],
  ['malformed', httpEvent({ traceparent: 'not-a-parent' })],
  ['zero ID', httpEvent({ traceparent: '00-00000000000000000000000000000000-2222222222222222-01' })],
  ['conflicting copies', { ...httpEvent(), multiValueHeaders: { traceparent: ['00-33333333333333333333333333333333-2222222222222222-01'] } }],
  ['batch', { Records: [{ messageAttributes: { traceparent: parent } }] }],
]) test(`independent root without a usable HTTP parent: ${name}`, async () => {
  const m = memory();
  process.env._X_AMZN_TRACE_ID = 'Root=1-11111111-111111111111111111111111;Parent=2222222222222222;Sampled=1';
  const ambient = m.provider.getTracer('other').startSpan('ambient');
  await context.with(trace.setSpan(ROOT_CONTEXT, ambient), async () => {
    await traceLambdaHandler(async () => {}, { provider: m.provider })(event, invocation());
    assert.equal(trace.getActiveSpan(), ambient);
  });
  ambient.end();
  const root = m.spans().find(s => s.name === 'aws.lambda.invoke');
  assert.equal(root.parentSpanContext?.spanId ?? root.parentSpanId, undefined);
  assert.notEqual(root.spanContext().traceId, ambient.spanContext().traceId);
});

test('remote unsampled parents remain unsampled; baggage and tracestate are not imported', async () => {
  const m = memory();
  await traceLambdaHandler(async () => {
    assert.equal(propagation.getBaggage(context.active()), undefined);
    assert.equal(trace.getActiveSpan().spanContext().traceState, undefined);
  }, { provider: m.provider })(httpEvent({ traceparent: parent.slice(0, -2) + '00', baggage: 'secret=PRIVATE', tracestate: 'vendor=PRIVATE' }), invocation());
  assert.equal(m.spans().length, 0);
});

for (const mode of ['resolve', 'reject', 'throw', 'throw-string', 'throw-undefined']) test(`preserves application outcome: ${mode}`, async () => {
  const receiver = await collector(); const provider = wired(receiver);
  const failure = mode === 'throw-string' ? 'PRIVATE_THROWN_STRING' : mode === 'throw-undefined' ? undefined : new TypeError('PRIVATE_ERROR');
  if (failure instanceof Error) failure.name = 'PRIVATE_ERROR_NAME';
  const result = { response: 'PRIVATE_RESULT' };
  const handler = traceLambdaHandler(() => {
    if (mode === 'resolve') return result;
    if (mode === 'reject') return Promise.reject(failure);
    throw failure;
  }, { provider });
  const event = { ...httpEvent({ traceparent: parent, authorization: 'PRIVATE_HEADER', host: 'PRIVATE_HOST' }), body: 'PRIVATE_BODY', rawPath: '/PRIVATE_PATH', rawQueryString: 'q=PRIVATE_QUERY', queryStringParameters: { q: 'PRIVATE_QUERY' } };
  if (mode === 'resolve') assert.equal(await handler(event, invocation()), result);
  else await assert.rejects(handler(event, invocation()), error => error === failure);
  const bytes = Buffer.concat(receiver.requests.map(r => r.bytes)).toString();
  assert.equal(bytes.includes('PRIVATE_'), false);
  const root = receiver.spans()[0];
  if (mode !== 'resolve') {
    assert.equal(root.status.code, 2); assert.equal(root.status.message, undefined);
    assert.equal(root.attributes.find(a => a.key === 'error.type').value.stringValue, failure instanceof Error ? 'TypeError' : '_OTHER');
  }
});

for (const hide of ['true', 'false']) test(`strict root privacy with FI_HIDE flags=${hide}; child policies remain independent`, async () => {
  process.env.FI_HIDE_ERROR_DETAILS = hide;
  process.env.FI_HIDE_INPUTS = hide; process.env.FI_HIDE_OUTPUTS = hide;
  const receiver = await collector(); const provider = wired(receiver);
  await traceLambdaHandler(async () => {
    const root = trace.getActiveSpan();
    root.recordException(new Error('PRIVATE_ROOT'));
    root.setStatus({ code: 2, message: 'PRIVATE_ROOT' });
    const child = provider.getTracer('independent').startSpan('raw-child');
    child.setAttribute('application.payload', 'CHILD_NOT_SANITIZED'); child.end();
  }, { provider })({}, invocation());
  const wire = Buffer.concat(receiver.requests.map(r => r.bytes)).toString();
  assert.equal(wire.includes('PRIVATE_ROOT'), false);
  assert.equal(wire.includes('CHILD_NOT_SANITIZED'), true);
});

for (const kind of ['throw', 'reject', 'stall', 'delay']) test(`bounded provider flushing: ${kind}`, async () => {
  const m = memory(); let calls = 0, shutdowns = 0;
  const original = m.provider.forceFlush.bind(m.provider);
  m.provider.forceFlush = () => {
    calls++; assert.ok(m.spans().length > 0, 'root ends before flushing');
    if (kind === 'throw') throw new Error('synthetic flush failure');
    if (kind === 'reject') return Promise.reject(new Error('synthetic flush failure'));
    if (kind === 'stall') return new Promise(() => {});
    return delay(60).then(original);
  };
  const savedShutdown = m.provider.shutdown.bind(m.provider);
  m.provider.shutdown = () => { shutdowns++; return savedShutdown(); };
  const a = traceLambdaHandler(async () => 42, { provider: m.provider, maxFlushWaitMillis: 20, remainingTimeReserveMillis: 250 });
  const b = traceLambdaHandler(async () => 42, { provider: m.provider, maxFlushWaitMillis: 20 });
  const start = performance.now();
  assert.equal(await a({}, invocation(260)), 42);
  assert.ok(performance.now() - start < 120);
  if (kind === 'stall') {
    for (let i = 0; i < 8; i++) assert.equal(await b({}, invocation()), 42);
    assert.equal(calls, 1, 'one pending flush shared across wrappers');
  }
  assert.equal(shutdowns, 0);
  m.provider.forceFlush = original;
});

for (const [name, options, remaining] of [
  ['configured maximum', { maxFlushWaitMillis: 20 }, 10000],
  ['remaining time minus explicit reserve', { maxFlushWaitMillis: 1000, remainingTimeReserveMillis: 1000 }, 1020],
]) test(`stalled flush respects ${name}`, async () => {
  const m = memory(); let calls = 0;
  m.provider.forceFlush = () => { calls++; return new Promise(() => {}); };
  const start = performance.now();
  assert.equal(await traceLambdaHandler(async () => 42, { provider: m.provider, ...options })({}, invocation(remaining)), 42);
  const elapsed = performance.now() - start;
  assert.equal(calls, 1);
  assert.ok(elapsed >= 10 && elapsed < 400, `expected a ~20 ms budget, observed ${elapsed} ms`);
});

for (const remaining of [0, 249, -1, NaN, Infinity, undefined, '1000']) test(`skip invalid or exhausted remaining-time budget ${remaining}`, async () => {
  const m = memory(); let calls = 0;
  m.provider.forceFlush = async () => { calls++; };
  const ctx = invocation(); ctx.getRemainingTimeInMillis = () => remaining;
  await traceLambdaHandler(async () => 1, { provider: m.provider })({}, ctx);
  assert.equal(calls, 0);
});

test('invalid options and throwing remaining-time getter skip flushing', async () => {
  const m = memory(); let calls = 0;
  m.provider.forceFlush = async () => { calls++; };
  for (const options of [{ maxFlushWaitMillis: NaN }, { remainingTimeReserveMillis: -1 }, { maxFlushWaitMillis: 0 }]) {
    await traceLambdaHandler(async () => 1, { provider: m.provider, ...options })({}, invocation());
  }
  await traceLambdaHandler(async () => 1, { provider: m.provider })({}, { getRemainingTimeInMillis() { throw new Error('synthetic'); } });
  assert.equal(calls, 0);
});

for (const mode of ['delay', 'reject', 'stall']) test(`real OTLP exporter ${mode} preserves outcomes and budgets`, async () => {
  const receiver = await collector(mode); const provider = wired(receiver);
  const error = new Error('PRIVATE_ERROR');
  const handler = traceLambdaHandler(async () => { throw error; }, { provider, maxFlushWaitMillis: 20 });
  const start = performance.now();
  await assert.rejects(handler({}, invocation()), caught => caught === error);
  assert.ok(performance.now() - start < 120);
  await delay(150); // exporter timeout also bounds the actual pending export, not only waiting
  assert.equal(receiver.requests.length, 1);
});

test('undefined/invalid/noop telemetry and context-manager faults do not reexecute application code', async () => {
  const result = {};
  for (const provider of [undefined, {}, { getTracer() { throw new Error('PRIVATE_SETUP'); } }, trace.getTracerProvider()]) {
    let calls = 0;
    assert.equal(await traceLambdaHandler(() => { calls++; return result; }, { provider })({}, invocation()), result);
    assert.equal(calls, 1);
  }
  context.setGlobalContextManager({ active: () => ROOT_CONTEXT, with(ctx, fn) { fn(); throw new Error('PRIVATE_CONTEXT'); }, bind: (_, fn) => fn, enable() { return this; }, disable() { return this; } });
  let calls = 0;
  assert.equal(await traceLambdaHandler(() => { calls++; return result; }, {})({}, invocation()), result);
  assert.equal(calls, 1);
});

test('span hook failures are isolated and application this is preserved', async () => {
  const provider = { getTracer: () => ({ startSpan() { throw new Error('PRIVATE_SAMPLER'); } }), forceFlush: async () => {} };
  const owner = { value: 42, handler: traceLambdaHandler(function () { return this.value; }, { provider }) };
  assert.equal(await owner.handler({}, invocation()), 42);
});

test('an explicit no-op provider never falls back to the application global provider', async () => {
  const noop = trace.getTracerProvider();
  trace.disable(); // Leave the captured provider unregistered when the application registers its own.
  noop.forceFlush = async () => {};
  const m = memory();
  assert.equal(await traceLambdaHandler(async () => 42, { provider: noop })({}, invocation()), 42);
  assert.equal(m.spans().length, 0);
});

test('telemetry diagnostics contain no exception/configuration data and a broken logger is isolated', async () => {
  const messages = [];
  const logger = Object.fromEntries(['debug', 'info', 'warn', 'error', 'verbose'].map(method => [method, (...args) => messages.push(args)]));
  diag.setLogger(logger, DiagLogLevel.DEBUG);
  const provider = { getTracer() { throw new Error('PRIVATE_SETUP'); }, forceFlush() { throw new Error('PRIVATE_FLUSH'); } };
  assert.equal(await traceLambdaHandler(async () => 42, { provider })({}, invocation()), 42);
  assert.match(JSON.stringify(messages), /provider initialization failed/);
  assert.match(JSON.stringify(messages), /provider flush failed/);
  assert.equal(JSON.stringify(messages).includes('PRIVATE_'), false);
  logger.debug = () => { throw new Error('PRIVATE_LOGGER'); };
  assert.equal(await traceLambdaHandler(async () => 42, { provider })({}, invocation()), 42);
});

test('same wrapper is not nested twice; streaming handler identity and marker are preserved', async () => {
  const m = memory();
  const handler = traceLambdaHandler(async () => 1, { provider: m.provider });
  assert.equal(traceLambdaHandler(handler, { provider: m.provider }), handler);
  await handler({}, invocation()); assert.equal(m.spans().length, 1);
  const stream = async () => {};
  stream[Symbol.for('aws.lambda.runtime.handler.streaming')] = 'response';
  assert.equal(traceLambdaHandler(stream, { provider: m.provider }), stream);
});

for (const flag of [undefined, true]) test(`callback tracing bypass preserves application semantics when event-loop flag is ${flag}`, async () => {
  const m = memory(); let flushCalls = 0;
  m.provider.forceFlush = async () => { flushCalls++; };
  const ctx = { ...invocation(), callbackWaitsForEmptyEventLoop: flag };
  const event = {}, callback = () => {}, result = {};
  const owner = { handler: traceLambdaHandler(function(e, c, cb) {
    assert.equal(this, owner); assert.equal(e, event); assert.equal(c, ctx); assert.equal(cb, callback);
    return result;
  }, { provider: m.provider, completion: 'callback' }) };
  assert.equal(owner.handler(event, ctx, callback), result);
  const failure = new Error('synthetic');
  assert.throws(() => traceLambdaHandler(() => { throw failure; }, { provider: m.provider, completion: 'callback' })(event, ctx, callback), error => error === failure);
  assert.equal(ctx.callbackWaitsForEmptyEventLoop, flag);
  assert.equal(m.spans().length, 0); assert.equal(flushCalls, 0);
});

test('a skipped callback invocation still consumes the cold-start flag', async () => {
  const m = memory();
  const handler = traceLambdaHandler((_event, _ctx, callback) => callback(null, 42), { provider: m.provider, completion: 'callback' });
  handler({}, invocation(), () => {});
  await new Promise(resolve => handler({}, { ...invocation(), callbackWaitsForEmptyEventLoop: false }, resolve));
  assert.equal(m.spans().length, 1);
  assert.equal(m.spans()[0].attributes['faas.coldstart'], false);
});

for (const mode of ['success', 'error', 'throw']) test(`Node 22 callback completion ${mode}`, { skip: Number(process.versions.node.split('.')[0]) !== 22 }, async () => {
  const m = memory(); const failure = new Error('PRIVATE_CALLBACK');
  let completionCalls = 0;
  const handler = traceLambdaHandler((_event, _ctx, callback) => {
    if (mode === 'throw') throw failure;
    setImmediate(() => { callback(mode === 'error' ? failure : null, 42); callback(null, 99); });
  }, { provider: m.provider, completion: 'callback' });
  const outcome = await new Promise(resolve => handler({}, { ...invocation(), callbackWaitsForEmptyEventLoop: false }, (error, result) => {
    completionCalls++; resolve({ error, result });
  }));
  await delay(1);
  assert.equal(completionCalls, 1);
  assert.equal(outcome.error, mode === 'success' ? null : failure);
  if (mode === 'success') assert.equal(outcome.result, 42);
  assert.equal(m.spans().length, 1);
});
