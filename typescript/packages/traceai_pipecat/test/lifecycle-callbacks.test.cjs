const { test } = require('node:test');
const assert = require('node:assert/strict');
const { context, SpanStatusCode } = require('@opentelemetry/api');
const { AsyncLocalStorageContextManager } = require('@opentelemetry/context-async-hooks');
const { BasicTracerProvider, SimpleSpanProcessor, InMemorySpanExporter } = require('@opentelemetry/sdk-trace-base');
const { PipecatInstrumentation } = require('../dist/src/instrumentation.js');
const { createClient } = require('./transport.cjs');
function setup(t, sdk, callbacks = {}) {
  const exporter = new InMemorySpanExporter();
  const started = [];
  const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter), {
    onStart(span) { started.push(span); }, onEnd() {}, async forceFlush() {}, async shutdown() {},
  }] });
  const manager = new AsyncLocalStorageContextManager().enable();
  context.setGlobalContextManager(manager);
  const instrumentation = new PipecatInstrumentation();
  instrumentation.setTracerProvider(provider);
  instrumentation.manuallyInstrument(sdk);
  const { client, transport } = createClient(sdk, callbacks);
  client.setLogLevel(sdk.LogLevel.NONE);
  t.after(async () => { instrumentation.disable(); await provider.shutdown(); manager.disable(); context.disable(); });
  return { exporter, provider, started, client, transport, instrumentation };
}
const connects = spans => spans.filter(s => s.attributes['pipecat.operation'] === 'client.connect');
async function ready(check) {
  for (let i = 0; i < 30 && !check(); i++) await new Promise(setImmediate);
  assert.ok(check());
}
for (const name of ['pipecat-floor', '@pipecat-ai/client-js']) {
  test(`${name}: transport-initiated disconnect completes an abandoned connect span`, async t => {
    const sdk = require(name);
    const { exporter, provider, started, client, transport } = setup(t, sdk);
    let awaitingBot = false;
    let settled = false;
    transport.sendReadyMessage = () => { awaitingBot = true; };
    client.connect().then(() => { settled = true; }, () => { settled = true; });
    await ready(() => awaitingBot);
    let disconnected = 0;
    client.on('disconnected', () => disconnected++);
    // Simulate media/network I/O dropping the transport before bot-ready.
    // No SDK module or public SDK method is replaced.
    transport.state = 'disconnected';
    transport.callbacks.onDisconnected();
    await new Promise(setImmediate);
    await provider.forceFlush();
    const spans = connects(exporter.getFinishedSpans());
    console.log(JSON.stringify({ scenario: 'transport drop', sdk: name, disconnected, settled,
      started: connects(started).length, finished: spans.length, statuses: spans.map(s => s.status.code) }));
    assert.equal(disconnected, 1);
    assert.equal(settled, false);
    assert.equal(spans.length, 1, 'transport disconnect must finish the abandoned connection');
    assert.equal(spans[0].status.code, SpanStatusCode.ERROR);
    assert.deepEqual(client.listeners('botReady'), []);
    assert.equal(client.listeners('disconnected').length, 1, 'only the application listener remains');
    transport.callbacks.onDisconnected();
    await provider.forceFlush();
    assert.equal(connects(exporter.getFinishedSpans()).length, 1, 'repeated notification cannot export twice');
  });
}

for (const name of ['pipecat-floor', '@pipecat-ai/client-js']) {
  test(`${name}: disconnect from botReady preserves the successful connection status`, async t => {
    const sdk = require(name);
    const { exporter, client } = setup(t, sdk);
    let disconnected;
    client.once('botReady', () => { disconnected = client.disconnect(); });
    const result = await client.connect();
    await disconnected;
    const spans = connects(exporter.getFinishedSpans());
    console.log(JSON.stringify({ scenario: 'bot-ready disconnect', sdk: name,
      botVersion: result.version, finished: spans.length, statuses: spans.map(s => s.status.code) }));
    assert.equal(spans.length, 1);
    assert.equal(spans[0].status.code, SpanStatusCode.OK, 'the SDK resolved connect before invoking botReady');
  });
}

test('latest: retry in disconnected callback is not mistaken for recursive endpoint delegation', async t => {
  const sdk = require('@pipecat-ai/client-js');
  const { exporter, provider, started, client, transport } = setup(t, sdk);
  const failure = new Error('synthetic first transport attempt failure');
  const connectTransport = transport.connect.bind(transport);
  let transportCalls = 0;
  transport.connect = async (...args) => {
    transportCalls++;
    if (transportCalls === 1) throw failure;
    return connectTransport(...args);
  };
  let retry;
  client.once('disconnected', () => { retry = client.connect(); });
  await assert.rejects(client.connect(), e => e === failure);
  await ready(() => retry !== undefined);
  await retry;
  await provider.forceFlush();
  const spans = connects(exporter.getFinishedSpans());
  console.log(JSON.stringify({ scenario: 'disconnected callback retry', transportCalls,
    connected: client.connected, started: connects(started).length, finished: spans.length, statuses: spans.map(s => s.status.code) }));
  await client.disconnect();
  assert.equal(transportCalls, 2);
  assert.equal(spans.length, 2, 'independent retry must have its own connection span');
  assert.deepEqual(spans.map(s => s.status.code).sort(), [SpanStatusCode.OK, SpanStatusCode.ERROR].sort());
});

for (const name of ['pipecat-floor', '@pipecat-ai/client-js']) {
  test(`${name}: constructor bot-ready callback can disconnect before event listeners run`, async t => {
    const sdk = require(name);
    let disconnected;
    const { client, exporter } = setup(t, sdk, {
      onBotReady() { disconnected = client.disconnect(); },
    });
    await client.connect();
    await disconnected;
    assert.deepEqual(connects(exporter.getFinishedSpans()).map(s => s.status.code), [SpanStatusCode.OK]);
  });

  test(`${name}: lifecycle callbacks observe the connected interval and restore on close without changing application listeners`, async t => {
    const sdk = require(name);
    const { client, transport, exporter, instrumentation } = setup(t, sdk);
    const appReady = () => {};
    const appDisconnected = () => {};
    client.on('botReady', appReady);
    client.on('disconnected', appDisconnected);
    const callbacks = client._options.callbacks;
    const originalReady = callbacks.onBotReady;
    const originalDisconnected = callbacks.onDisconnected;
    const originalStateChanged = callbacks.onTransportStateChanged;
    const assertDetached = () => {
      assert.deepEqual(client.listeners('botReady'), [appReady]);
      assert.deepEqual(client.listeners('disconnected'), [appDisconnected]);
      assert.equal(callbacks.onBotReady, originalReady);
      assert.equal(callbacks.onDisconnected, originalDisconnected);
      assert.equal(callbacks.onTransportStateChanged, originalStateChanged);
      assert.equal(instrumentation.pendingConnections.has(client), false);
    };
    await client.connect();
    // The empty attempt set must still observe the NEXT transport-state event:
    // its application callbacks can start a retry before onDisconnected arrives.
    assert.notEqual(callbacks.onTransportStateChanged, originalStateChanged);
    assert.equal(instrumentation.pendingConnections.get(client).attempts.size, 0);
    await client.disconnect();
    assertDetached();
    transport.connectError = new Error('synthetic transport failure');
    await assert.rejects(client.connect());
    assertDetached();
    delete transport.connectError;
    await client.disconnect();
    let awaitingBot = false;
    const sendReady = transport.sendReadyMessage.bind(transport);
    transport.sendReadyMessage = () => { awaitingBot = true; };
    const pending = client.connect();
    await ready(() => awaitingBot);
    assert.deepEqual(client.listeners('botReady'), [appReady]);
    assert.deepEqual(client.listeners('disconnected'), [appDisconnected]);
    assert.notEqual(callbacks.onBotReady, originalReady);
    assert.notEqual(callbacks.onDisconnected, originalDisconnected);
    instrumentation.disable();
    assertDetached();
    const spans = connects(exporter.getFinishedSpans());
    assert.deepEqual(spans.map(s => s.status.code), [SpanStatusCode.OK, SpanStatusCode.ERROR, SpanStatusCode.ERROR]);
    sendReady();
    await pending;
    assert.equal(connects(exporter.getFinishedSpans()).length, 3, 'late settlement must not end twice');
    await client.disconnect();
    instrumentation.enable();
    transport.sendReadyMessage = sendReady;
    await client.connect();
    assert.notEqual(callbacks.onTransportStateChanged, originalStateChanged);
    await client.disconnect();
    assertDetached();
    assert.equal(connects(exporter.getFinishedSpans()).length, 4);
  });
}

function mockStartBot(t) {
  const fetch = globalThis.fetch;
  globalThis.fetch = async () => new Response(JSON.stringify({ transport: 'synthetic' }), { status: 200 });
  t.after(() => { globalThis.fetch = fetch; });
}

test('latest: endpoint failure callback retries are independent of the consumed SDK delegation', async t => {
  const sdk = require('@pipecat-ai/client-js');
  const { client, transport, exporter } = setup(t, sdk);
  mockStartBot(t);
  const connect = transport.connect.bind(transport);
  let attempts = 0;
  transport.connect = async (...args) => {
    if (++attempts === 1) throw new Error('synthetic first connection failure');
    return connect(...args);
  };
  let retry;
  client.once('disconnected', () => { retry = client.connect(); });
  await assert.rejects(client.connect({ endpoint: 'http://127.0.0.1/synthetic/start' }));
  await retry;
  assert.equal(attempts, 2);
  assert.deepEqual(connects(exporter.getFinishedSpans()).map(s => s.status.code).sort(), [SpanStatusCode.OK, SpanStatusCode.ERROR]);
  await client.disconnect();
});

test('latest: an onBotStarted connection is not the SDK endpoint delegation', async t => {
  const sdk = require('@pipecat-ai/client-js');
  let independent;
  const { client, exporter } = setup(t, sdk, {
    onBotStarted(params) { independent = client.connect(params); },
  });
  mockStartBot(t);
  await assert.rejects(client.connect({ endpoint: 'http://127.0.0.1/synthetic/start' }), /already started/);
  await independent;
  assert.deepEqual(connects(exporter.getFinishedSpans()).map(s => s.status.code).sort(), [SpanStatusCode.OK, SpanStatusCode.ERROR]);
  await client.disconnect();
});

test('latest: endpoint bot-ready callback preserves one successful connection span', async t => {
  const sdk = require('@pipecat-ai/client-js');
  let disconnected;
  const { client, exporter } = setup(t, sdk, {
    onBotReady() { disconnected = client.disconnect(); },
  });
  mockStartBot(t);
  await client.connect({ endpoint: 'http://127.0.0.1/synthetic/start' });
  await disconnected;
  assert.deepEqual(connects(exporter.getFinishedSpans()).map(s => s.status.code), [SpanStatusCode.OK]);
});

for (const callbackStyle of ['listener', 'constructor']) {
  test(`latest: delayed retry from a ${callbackStyle} disconnected callback is not cancelled by the old event`, async t => {
    const sdk = require('@pipecat-ai/client-js');
    let retry;
    let retries = 0;
    const onDisconnected = () => { if (++retries === 1) retry = client.connect(); };
    const { client, transport, exporter } = setup(t, sdk,
      callbackStyle === 'constructor' ? { onDisconnected } : {});
    if (callbackStyle === 'listener') client.once('disconnected', onDisconnected);
    const connect = transport.connect.bind(transport);
    let attempts = 0;
    transport.connect = async (...args) => {
      if (++attempts === 1) throw new Error('synthetic first attempt failure');
      await new Promise(setImmediate);
      return connect(...args);
    };
    await assert.rejects(client.connect());
    await retry;
    assert.equal(attempts, 2);
    assert.deepEqual(connects(exporter.getFinishedSpans()).map(s => s.status.code), [SpanStatusCode.ERROR, SpanStatusCode.OK]);
    await client.disconnect();
  });
}
