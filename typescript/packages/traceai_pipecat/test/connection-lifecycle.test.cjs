const { test } = require('node:test');
const assert = require('node:assert/strict');
const { context, trace, SpanStatusCode } = require('@opentelemetry/api');
const { AsyncLocalStorageContextManager } = require('@opentelemetry/context-async-hooks');
const { BasicTracerProvider, SimpleSpanProcessor, InMemorySpanExporter } = require('@opentelemetry/sdk-trace-base');
const { PipecatInstrumentation, observePipecatClient } = require('../dist/src');
const { createClient } = require('./transport.cjs');

function setup(t, sdk, instrument = true) {
  const exporter = new InMemorySpanExporter();
  const ended = [];
  const provider = new BasicTracerProvider({ spanProcessors: [
    new SimpleSpanProcessor(exporter),
    { onStart() {}, onEnd(span) { ended.push(span); }, async forceFlush() {}, async shutdown() {} },
  ] });
  const manager = new AsyncLocalStorageContextManager().enable();
  context.setGlobalContextManager(manager);
  let instrumentation;
  if (instrument) {
    instrumentation = new PipecatInstrumentation();
    instrumentation.setTracerProvider(provider);
    instrumentation.manuallyInstrument(sdk);
  }
  const { client, transport } = createClient(sdk);
  client.setLogLevel(sdk.LogLevel.NONE);
  t.after(async () => { instrumentation?.disable(); await provider.shutdown(); manager.disable(); context.disable(); });
  return { exporter, provider, client, transport, ended, instrumentation };
}
const connects = exporter => exporter.getFinishedSpans().filter(s => s.attributes['pipecat.operation'] === 'client.connect');
async function untilReady(check) {
  for (let i = 0; i < 20 && !check(); i++) await new Promise(setImmediate);
  assert.equal(check(), true, 'real SDK reached the bot-ready handshake');
}

for (const name of ['pipecat-floor', '@pipecat-ai/client-js']) {
  const sdk = require(name);
  test(`${name}: disconnect completes an abandoned handshake span without settling the SDK promise`, async t => {
    const { client, transport, exporter, provider } = setup(t, sdk);
    let awaitingReady = false;
    transport.sendReadyMessage = () => { awaitingReady = true; };
    let settled = false;
    client.connect().then(() => { settled = true; }, () => { settled = true; });
    await untilReady(() => awaitingReady);
    assert.equal(connects(exporter).length, 0);
    await client.disconnect();
    await provider.forceFlush();
    assert.equal(settled, false, 'instrumentation must not alter SDK cancellation behavior');
    assert.equal(connects(exporter).length, 1);
    assert.equal(connects(exporter)[0].status.code, SpanStatusCode.ERROR);
    assert.deepEqual(connects(exporter)[0].events, []);
    assert.equal(connects(exporter)[0].status.message, undefined);
    await client.disconnect();
    assert.equal(connects(exporter).length, 1, 'repeated disconnect must not export again');
  });

  test(`${name}: late bot-ready after cancellation cannot overwrite ERROR or duplicate the span`, async t => {
    const { client, transport, exporter, ended } = setup(t, sdk);
    const ready = transport.sendReadyMessage.bind(transport);
    let awaitingReady = false;
    transport.sendReadyMessage = () => { awaitingReady = true; };
    const pending = client.connect();
    await untilReady(() => awaitingReady);
    await client.disconnect();
    ready();
    await pending;
    assert.equal(connects(exporter).length, 1);
    assert.equal(connects(exporter)[0].status.code, SpanStatusCode.ERROR);
    assert.equal(ended.filter(s => s.attributes['pipecat.operation'] === 'client.connect').length, 1);
    transport.sendReadyMessage = ready;
    await client.disconnect();
    await client.connect();
    await client.disconnect();
    assert.deepEqual(connects(exporter).map(s => s.status.code), [SpanStatusCode.ERROR, SpanStatusCode.OK]);
  });

  test(`${name}: disconnecting one client does not end another client's pending connection`, async t => {
    const { client, transport, exporter } = setup(t, sdk);
    const other = createClient(sdk);
    let firstReady = false;
    let secondReady = false;
    transport.sendReadyMessage = () => { firstReady = true; };
    const ready = other.transport.sendReadyMessage.bind(other.transport);
    other.transport.sendReadyMessage = () => { secondReady = true; };
    client.connect();
    const second = other.client.connect();
    await untilReady(() => firstReady && secondReady);
    await client.disconnect();
    assert.equal(connects(exporter).length, 1);
    assert.equal(connects(exporter)[0].status.code, SpanStatusCode.ERROR);
    ready();
    await second;
    await other.client.disconnect();
    assert.deepEqual(connects(exporter).map(s => s.status.code), [SpanStatusCode.ERROR, SpanStatusCode.OK]);
  });

  test(`${name}: browser transport state errors are classified without capturing payloads`, async t => {
    const { client, exporter, provider } = setup(t, sdk, false);
    const stop = observePipecatClient(client, { tracer: provider.getTracer('events') });
    t.after(stop);
    for (const state of ['error', 'connected', 'synthetic-private-state', { toString() { throw new Error('must not coerce'); } }]) {
      client.emit('transportStateChanged', state, { token: 'synthetic-private-token' });
    }
    const spans = exporter.getFinishedSpans();
    assert.deepEqual(spans.map(s => s.status.code), [SpanStatusCode.ERROR, SpanStatusCode.OK, SpanStatusCode.OK, SpanStatusCode.OK]);
    assert.ok(!JSON.stringify(spans.map(s => [s.attributes, s.status, s.events])).includes('synthetic-private'));
  });
}

test('separate calls on the same client are not suppressed while a handshake is pending', async t => {
  const sdk = require('@pipecat-ai/client-js');
  const { client, transport, exporter } = setup(t, sdk);
  let awaitingReady = false;
  transport.sendReadyMessage = () => { awaitingReady = true; };
  client.connect();
  await untilReady(() => awaitingReady);
  assert.throws(() => client.connect(), /already started/);
  assert.equal(connects(exporter).length, 1, 'the independent rejected call has its own span');
  await client.disconnect();
  assert.equal(connects(exporter).length, 2);
  assert.ok(connects(exporter).every(s => s.status.code === SpanStatusCode.ERROR));
});

for (const fails of [false, true]) {
  test(`cancelled endpoint delegation stays one span when the SDK later ${fails ? 'rejects' : 'resolves'}`, async t => {
    const sdk = require('@pipecat-ai/client-js');
    const { client, transport, exporter, ended } = setup(t, sdk);
    const failure = new Error('synthetic-private-transport-failure');
    if (fails) transport.connectError = failure;
    let respond;
    const fetch = globalThis.fetch;
    globalThis.fetch = () => new Promise(resolve => { respond = resolve; });
    t.after(() => { globalThis.fetch = fetch; });
    const pending = client.connect({ endpoint: 'http://127.0.0.1/synthetic/start' });
    await untilReady(() => !!respond);
    await client.disconnect();
    assert.equal(connects(exporter).length, 1);
    respond(new Response(JSON.stringify({ transport: 'synthetic' }), { status: 200 }));
    if (fails) await assert.rejects(pending, error => error === failure);
    else await pending;
    await client.disconnect();
    assert.equal(connects(exporter).length, 1);
    assert.equal(connects(exporter)[0].status.code, SpanStatusCode.ERROR);
    assert.equal(ended.filter(s => s.attributes['pipecat.operation'] === 'client.connect').length, 1);
    assert.ok(!JSON.stringify(connects(exporter).map(s => [s.attributes, s.status, s.events])).includes('synthetic-private'));
  });
}

test('a client started inside another connection retains its independent span', async t => {
  const sdk = require('@pipecat-ai/client-js');
  const { client, transport, exporter } = setup(t, sdk);
  const other = createClient(sdk);
  const original = transport.connect.bind(transport);
  transport.connect = async (...args) => { await other.client.connect(); return original(...args); };
  await client.connect();
  await client.disconnect();
  await other.client.disconnect();
  const spans = connects(exporter);
  assert.equal(spans.length, 2);
  assert.equal(spans[0].parentSpanId, spans[1].spanContext().spanId);
});

test('floor browser observer reports a real handshake timeout as ERROR', async t => {
  const sdk = require('pipecat-floor');
  const { provider, exporter, transport } = setup(t, sdk, false);
  transport.sendReadyMessage = () => {};
  const client = new sdk.RTVIClient({
    transport, enableMic: false, enableCam: false, timeout: 5,
    params: { baseUrl: 'http://127.0.0.1/unused' }, customConnectHandler: async () => ({}),
  });
  client.setLogLevel(sdk.LogLevel.NONE);
  const states = [];
  client.on('transportStateChanged', state => states.push(state));
  const stop = observePipecatClient(client, { tracer: provider.getTracer('events') });
  t.after(stop);
  await assert.rejects(client.connect());
  assert.equal(states.at(-1), 'error');
  const errors = exporter.getFinishedSpans().filter(s => s.status.code === SpanStatusCode.ERROR);
  assert.equal(errors.length, 1);
  assert.equal(errors[0].attributes['pipecat.event'], 'transportStateChanged');
});

test('latest endpoint connect exports one span per application attempt, including concurrent clients', async t => {
  const sdk = require('@pipecat-ai/client-js');
  const { client, transport, exporter, provider } = setup(t, sdk);
  const other = createClient(sdk);
  const parent = provider.getTracer('app').startSpan('application');
  let fetchCalls = 0;
  let transportCalls = 0;
  const fetch = globalThis.fetch;
  globalThis.fetch = async () => {
    fetchCalls++;
    await new Promise(setImmediate);
    return new Response(JSON.stringify({ transport: 'synthetic' }), { status: 200 });
  };
  t.after(() => { globalThis.fetch = fetch; });
  for (const target of [transport, other.transport]) {
    const original = target.connect.bind(target);
    target.connect = async (...args) => { transportCalls++; return original(...args); };
  }
  await context.with(trace.setSpan(context.active(), parent), () => Promise.all([
    client.connect({ endpoint: 'http://127.0.0.1/synthetic/start' }),
    other.client.connect({ endpoint: 'http://127.0.0.1/synthetic/start' }),
  ]));
  await client.disconnect();
  await other.client.disconnect();
  assert.equal(fetchCalls, 2);
  assert.equal(transportCalls, 2);
  assert.equal(connects(exporter).length, 2);
  assert.ok(connects(exporter).every(s => s.parentSpanId === parent.spanContext().spanId));
  assert.ok(connects(exporter).every(s => s.status.code === SpanStatusCode.OK));
  parent.end();
});
