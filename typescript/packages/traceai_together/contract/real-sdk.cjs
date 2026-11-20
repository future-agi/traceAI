// node contract/real-sdk.cjs /absolute/path/to/node_modules/together-ai
// Real SDKs, synthetic fetch only; the sole network destination is a loopback OTLP sink.
const assert = require('node:assert/strict');
const {test, beforeEach, afterEach, after} = require('node:test');
const {createRequire} = require('node:module');
const {resolve, dirname} = require('node:path');
const {pathToFileURL} = require('node:url');
const {createServer} = require('node:http');
const {once} = require('node:events');
const net = require('node:net');
const connect = net.Socket.prototype.connect;
net.Socket.prototype.connect = function (...args) {
  // http.Agent passes an already-normalized [options, callback] array.
  const options = Array.isArray(args[0]) ? args[0][0] : net._normalizeArgs(args)[0];
  if (options?.path) return connect.apply(this, args);
  if (!['127.0.0.1', 'localhost', '::1'].includes(options?.host ?? 'localhost')) {
    throw new Error('Contract forbids outbound network');
  }
  return connect.apply(this, args);
};
const core = createRequire(resolve(__dirname, '../../fi-core/package.json'));
const {BasicTracerProvider, SimpleSpanProcessor, InMemorySpanExporter} = core('@opentelemetry/sdk-trace-base');
const {AsyncLocalStorageContextManager} = core('@opentelemetry/context-async-hooks');
const {OTLPTraceExporter} = core('@opentelemetry/exporter-trace-otlp-http');
const {context, trace, diag, DiagLogLevel} = core('@opentelemetry/api');
const {suppressTracing} = core('@opentelemetry/core');
const {SemanticConventions: S} = require('@traceai/fi-semantic-conventions');
const {TogetherInstrumentation, isPatched} = require('../dist/cjs');
// Default: the workspace devDependency. Its exports map hides package.json, so use the root entry.
const sdkPath = resolve(process.argv[2] || dirname(require.resolve('together-ai')));
const sdkRequire = createRequire(resolve(sdkPath, '../contract-resolver.cjs'));
const exporter = new InMemorySpanExporter();
const provider = new BasicTracerProvider({spanProcessors: [new SimpleSpanProcessor(exporter)]});
context.setGlobalContextManager(new AsyncLocalStorageContextManager().enable());
const instances = [];
const warnings = [];
const errors = [];
diag.setLogger({warn: (...args) => warnings.push(args.join(' ')), error: (...args) => errors.push(args.join(' ')), info() {}, debug() {}, verbose() {}}, DiagLogLevel.WARN);
function instrument(config = {}) {
  const inst = new TogetherInstrumentation(config);
  inst.setTracerProvider(provider);
  instances.push(inst);
  return inst;
}
// Loading AFTER instrumentation proves automatic resource hooking, not only manual attachment.
const automatic = instrument();
const sdk = sdkRequire('together-ai');
const Together = sdk.Together || sdk.default || sdk;
const model = 'synthetic-model';
const usage = {prompt_tokens: 2, completion_tokens: 1, total_tokens: 3};
const tool = {id: 'tool-1', type: 'function', function: {name: 'weather', arguments: '{"city":"Paris"}'}};
const chatBody = {model, messages: [{role: 'user', content: 'private-input-sentinel'}]};
let requests = [];
async function syntheticFetch(url, options) {
  const body = JSON.parse(options.body);
  requests.push({url: String(url), body, options});
  if (body.model === 'error') return new Response(JSON.stringify({error: {message: 'private-error-sentinel'}}), {status: 400, headers: {'content-type': 'application/json'}});
  const embedding = String(url).includes('/embeddings');
  const chat = String(url).includes('/chat/');
  const result = embedding
    ? {model, data: [{index: 0, embedding: [0.123456789, 0.987654321]}], usage}
    : {id: 'response-1', model, choices: [{index: 0, message: {role: 'assistant', content: 'private-output-sentinel', tool_calls: [tool]}, text: 'private-output-sentinel', finish_reason: 'stop'}], usage};
  if (body.stream) {
    const chunks = [
      {id: 'response-1', model, choices: [{index: 0, ...(chat ? {delta: {content: 'Hello', tool_calls: [{...tool, index: 0, function: {name: 'weather', arguments: '{"city":'}}]}} : {text: 'Hello'})}]},
      {id: 'response-1', model, choices: [{index: 0, ...(chat ? {delta: {content: ' world', tool_calls: [{index: 0, function: {arguments: '"Paris"}'}}]}} : {text: ' world'}), finish_reason: 'stop'}]},
      {id: 'response-1', model, choices: [], usage},
    ];
    const events = chunks.map(chunk => `data: ${JSON.stringify(chunk)}\n\n`);
    events.push(body.model === 'stream-error' ? 'data: {"error":{"message":"stream failed"}}\n\n' : 'data: [DONE]\n\n');
    return new Response(events.join(''), {headers: {'content-type': 'text/event-stream'}});
  }
  return new Response(JSON.stringify(result), {headers: {'content-type': 'application/json', 'x-request-id': 'request-1'}});
}
function client(Constructor = Together) { return new Constructor({apiKey: 'synthetic-not-a-real-key', fetch: syntheticFetch, maxRetries: 0}); }
function setup(config) { const inst = instrument(config); inst.manuallyInstrument(sdk); return {inst, c: client()}; }
function onlySpan() { assert.equal(exporter.getFinishedSpans().length, 1); return exporter.getFinishedSpans()[0]; }
// OpenTelemetry also warns when the SDK was required before a later instrumentor; only hook diagnostics matter here.
const hookWarnings = () => warnings.filter(w => w.includes('supported resource hooks'));
beforeEach(() => { exporter.reset(); requests = []; warnings.length = 0; errors.length = 0; });
afterEach(() => { for (const inst of instances.splice(0)) inst.disable(); assert.equal(isPatched(), false); });
after(async () => { context.disable(); diag.disable(); await provider.shutdown(); net.Socket.prototype.connect = connect; });

test('automatic hooks create a real Together chat span', async () => {
  assert.equal(automatic.isEnabled(), true);
  await client().chat.completions.create(chatBody);
  assert.equal(onlySpan().attributes[S.LLM_PROVIDER], 'together');
});

for (const surface of ['chat', 'completion', 'embedding']) {
  test(`${surface}: real resource calls export exactly one provider-correct span`, async () => {
    const {c} = setup();
    if (surface === 'chat') await c.chat.completions.create(chatBody);
    if (surface === 'completion') await c.completions.create({model, prompt: 'private-input-sentinel'});
    if (surface === 'embedding') await c.embeddings.create({model, input: 'private-input-sentinel'});
    const span = onlySpan();
    assert.equal(span.attributes[S.LLM_PROVIDER], 'together');
    assert.equal(span.attributes[S.LLM_TOKEN_COUNT_PROMPT], 2);
    assert.equal(span.attributes[surface === 'embedding' ? S.EMBEDDING_MODEL_NAME : S.LLM_MODEL_NAME], model);
    assert.equal(requests.length, 1);
  });
}

test('missing resource layout warns without claiming success', () => {
  const inst = instrument();
  inst.manuallyInstrument(Object.freeze({default: class Legacy {}}));
  assert.equal(isPatched(), false);
  assert.ok(warnings.some(w => w.includes('0/3 supported resource hooks')));
});

test('partial resource layouts report their actual hook count', () => {
  const inst = instrument();
  inst.manuallyInstrument({Together: {Embeddings: class {create() {return Promise.resolve({});}}}});
  assert.equal(isPatched(), true);
  assert.ok(warnings.some(w => w.includes('1/3 supported resource hooks')));
});

test('repeated attachment and a second instrumentor do not duplicate spans', async () => {
  const {inst, c} = setup();
  inst.manuallyInstrument(Object.freeze({default: Together}));
  const second = instrument(); second.manuallyInstrument(sdk); second.disable();
  await c.chat.completions.create(chatBody);
  onlySpan();
});

test('disable and enable restore existing clients and manual registrations', async () => {
  const {inst, c} = setup();
  inst.disable();
  await c.chat.completions.create(chatBody);
  assert.equal(exporter.getFinishedSpans().length, 0);
  inst.enable();
  await c.chat.completions.create(chatBody);
  onlySpan();
});

test('disabled instrumentation waits for enable before patching', async () => {
  const {inst, c} = setup({instrumentationConfig: {enabled: false}});
  assert.equal(isPatched(), false);
  inst.enable(); await c.chat.completions.create(chatBody); onlySpan();
});

test('ESM namespace and default constructor work without namespace writes', async () => {
  const esm = await import(pathToFileURL(resolve(sdkPath, 'index.mjs')).href);
  const inst = instrument(); inst.manuallyInstrument(esm);
  await client(esm.default).chat.completions.create(chatBody);
  onlySpan(); assert.deepEqual(hookWarnings(), []);
});

test('the default-exported Together class can be passed directly', async () => {
  const inst = instrument(); inst.manuallyInstrument(Together);
  await client().embeddings.create({model, input: 'x'});
  onlySpan(); assert.deepEqual(hookWarnings(), []);
});

test('parent trace and explicit suppression are respected', async () => {
  const {c} = setup();
  const parent = provider.getTracer('contract').startSpan('parent');
  await context.with(trace.setSpan(context.active(), parent), () => c.chat.completions.create(chatBody));
  assert.equal(onlySpan().spanContext().traceId, parent.spanContext().traceId);
  assert.equal(onlySpan().parentSpanContext.spanId, parent.spanContext().spanId);
  exporter.reset();
  await context.with(suppressTracing(context.active()), () => c.chat.completions.create(chatBody));
  assert.equal(exporter.getFinishedSpans().length, 0);
  parent.end();
});

test('APIPromise withResponse and repeated await retain metadata and one span', async () => {
  const {c} = setup(); const promise = c.chat.completions.create(chatBody);
  assert.equal(typeof promise.withResponse, 'function');
  const response = await promise.withResponse();
  assert.equal(response.response.headers.get('x-request-id'), 'request-1');
  assert.equal(await promise, response.data);
  assert.equal(requests.length, 1); onlySpan();
});

test('asResponse leaves the raw body unconsumed', async () => {
  const {c} = setup(); const response = await c.chat.completions.create(chatBody).asResponse();
  assert.equal(response.bodyUsed, false);
  assert.equal((await response.json()).id, 'response-1'); onlySpan();
});

test('nonstream tool calls remain structured', async () => {
  const {c} = setup(); await c.chat.completions.create(chatBody);
  const messages = JSON.parse(onlySpan().attributes[S.LLM_OUTPUT_MESSAGES]);
  assert.equal(messages[0].tool_calls[0].function.arguments, tool.function.arguments);
});

for (const surface of ['chat', 'completion']) {
  for (const early of [false, true]) {
    test(`${surface} streaming ${early ? 'early break' : 'full consumption'} closes one span and preserves controller`, async () => {
      const {c} = setup();
      const stream = surface === 'chat' ? await c.chat.completions.create({...chatBody, stream: true}) : await c.completions.create({model, prompt: 'x', stream: true});
      assert.equal(typeof stream.controller.abort, 'function');
      assert.equal(typeof stream.tee, 'function');
      let count = 0;
      for await (const chunk of stream) { count++; if (early) break; }
      assert.equal(count, early ? 1 : 3);
      const span = onlySpan();
      assert.equal(span.attributes[S.OUTPUT_VALUE], early ? 'Hello' : 'Hello world');
      if (!early) assert.equal(span.attributes[S.LLM_TOKEN_COUNT_TOTAL], 3);
      if (!early && surface === 'chat') assert.equal(JSON.parse(span.attributes[S.LLM_OUTPUT_MESSAGES])[0].tool_calls[0].function.arguments, tool.function.arguments);
    });
  }
}

test('stream tee retains exactly one upstream trace', async () => {
  const {c} = setup(); const stream = await c.chat.completions.create({...chatBody, stream: true});
  const [left, right] = stream.tee();
  const consume = async s => { let n = 0; for await (const chunk of s) n++; return n; };
  assert.deepEqual(await Promise.all([consume(left), consume(right)]), [3, 3]); onlySpan();
});

test('SDK HTTP rejection preserves the exception and error span', async () => {
  const {c} = setup();
  await assert.rejects(c.chat.completions.create({...chatBody, model: 'error'}), /private-error-sentinel/);
  assert.equal(onlySpan().status.code, 2);
});

test('stream rejection ends a single error span', async () => {
  const {c} = setup(); const stream = await c.chat.completions.create({...chatBody, model: 'stream-error', stream: true});
  await assert.rejects(async () => {for await (const chunk of stream) {}}, /stream failed/);
  assert.equal(onlySpan().status.code, 2);
});

test('OTLP wire contains Together metadata but no hidden request, response, vector or error payload', async () => {
  const payloads = [];
  const server = createServer(async (req, res) => {
    const chunks = []; for await (const chunk of req) chunks.push(chunk);
    payloads.push(JSON.parse(Buffer.concat(chunks).toString()));
    res.setHeader('content-type', 'application/json'); res.end('{}');
  });
  server.listen(0, '127.0.0.1'); await once(server, 'listening');
  const wireProvider = new BasicTracerProvider({spanProcessors: [new SimpleSpanProcessor(new OTLPTraceExporter({url: `http://127.0.0.1:${server.address().port}/v1/traces`}))]});
  try {
    const inst = instrument({traceConfig: {hideInputs: true, hideOutputs: true, hideEmbeddingVectors: true, hideErrorDetails: true}});
    inst.setTracerProvider(wireProvider);
    inst.manuallyInstrument(sdk);
    const c = client();
    await c.chat.completions.create({...chatBody, temperature: 0.25, stop: ['private-input-sentinel'],
      tools: [{type: 'function', function: {name: 'lookup', description: 'private-input-sentinel'}}]});
    await c.completions.create({model, prompt: 'private-input-sentinel'});
    await c.embeddings.create({model, input: 'private-input-sentinel'});
    await assert.rejects(c.chat.completions.create({...chatBody, model: 'error'}));
    await wireProvider.forceFlush();
    assert.deepEqual(errors, []);
    assert.ok(payloads.length >= 1);
    const wire = JSON.stringify(payloads);
    for (const secret of ['private-input-sentinel', 'private-output-sentinel', 'private-error-sentinel', '0.123456789']) assert.ok(!wire.includes(secret), secret);
    const spans = payloads.flatMap(p => p.resourceSpans.flatMap(r => r.scopeSpans.flatMap(s => s.spans)));
    assert.equal(spans.length, 4);
    for (const span of spans) assert.ok(span.attributes.some(a => a.key === S.LLM_PROVIDER && a.value.stringValue === 'together'));
    assert.ok(spans.some(s => s.status.code === 2));
    const parameters = spans[0].attributes.find(a => a.key === S.LLM_INVOCATION_PARAMETERS);
    assert.equal(JSON.parse(parameters.value.stringValue).temperature, 0.25);
  } finally {
    await wireProvider.shutdown(); server.close(); await once(server, 'close');
  }
});
