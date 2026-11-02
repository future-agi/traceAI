const { test } = require('node:test');
const assert = require('node:assert/strict');
const { context, SpanStatusCode } = require('@opentelemetry/api');
const { AsyncLocalStorageContextManager } = require('@opentelemetry/context-async-hooks');
const { BasicTracerProvider, SimpleSpanProcessor, InMemorySpanExporter } = require('@opentelemetry/sdk-trace-base');
const { PipecatInstrumentation } = require('../dist/src/instrumentation.js');
const { createClient } = require('./transport.cjs');
const turn = () => new Promise(setImmediate);
const connects = exporter => exporter.getFinishedSpans().filter(s => s.attributes['pipecat.operation'] === 'client.connect');
async function wait(check) {
  const deadline = Date.now() + 3000;
  while (!check() && Date.now() < deadline) await new Promise(resolve => setTimeout(resolve, 1));
  assert.ok(check(), 'timed out waiting for connection cleanup');
}
test('wait observes cleanup scheduled on a later timer turn', async () => {
  let completed = false;
  setTimeout(() => { completed = true; }, 500);
  await wait(() => completed);
});
function setup(t, name, callbacks = {}) {
  const sdk = require(name);
  const exporter = new InMemorySpanExporter();
  const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
  const manager = new AsyncLocalStorageContextManager().enable();
  context.setGlobalContextManager(manager);
  const instrumentation = new PipecatInstrumentation();
  instrumentation.setTracerProvider(provider);
  instrumentation.manuallyInstrument(sdk);
  const { client, transport } = createClient(sdk, callbacks);
  client.setLogLevel(sdk.LogLevel.NONE);
  t.after(async () => { instrumentation.disable(); await provider.shutdown(); manager.disable(); context.disable(); });
  return { client, transport, exporter, provider, instrumentation };
}
for (const name of ['pipecat-floor', '@pipecat-ai/client-js']) {
  test(`${name}: autonomous drop must not cancel its constructor callback's successful retry`, async t => {
    let retry;
    let count = 0;
    const { client, transport, exporter } = setup(t, name, {
      onDisconnected() { if (++count === 1) retry = client.connect(); },
    });
    await client.connect();
    const original = transport.connect.bind(transport);
    transport.connect = async (...args) => { await turn(); return original(...args); };
    transport.state = 'disconnected';
    transport.callbacks.onDisconnected();
    const result = await retry;
    const statuses = connects(exporter).map(s => s.status.code);
    console.log(JSON.stringify({ scenario: 'autonomous callback retry', sdk: name, botVersion: result.version, connected: client.connected, statuses }));
    await client.disconnect();
    assert.deepEqual(statuses, [SpanStatusCode.OK, SpanStatusCode.OK]);
  });

  test(`${name}: old bot-ready must not finish a new failed connection as successful`, async t => {
    let retry;
    let first = true;
    let disconnect;
    const { client, transport, exporter } = setup(t, name, {
      onBotReady() {
        if (!first) return;
        first = false;
        disconnect = client.disconnect();
      },
      onDisconnected() {
        if (!retry) {
          retry = client.connect();
          retry.catch(() => {}); // The awaited disconnect now includes deferred cleanup.
        }
      },
    });
    const original = transport.connect.bind(transport);
    const failure = new Error('synthetic retry transport failure');
    let attempts = 0;
    transport.connect = async (...args) => {
      if (++attempts === 1) return original(...args);
      await turn();
      throw failure;
    };
    await client.connect();
    await disconnect;
    await assert.rejects(retry, e => e === failure);
    await turn();
    const statuses = connects(exporter).map(s => s.status.code);
    console.log(JSON.stringify({ scenario: 'old bot-ready affects retry', sdk: name, attempts, statuses }));
    assert.deepEqual(statuses, [SpanStatusCode.OK, SpanStatusCode.ERROR]);
  });

  test(`${name}: failed disconnect must not label a later successful handshake as cancelled`, async t => {
    const { client, transport, exporter } = setup(t, name);
    let awaitingReady = false;
    const sendReady = transport.sendReadyMessage.bind(transport);
    transport.sendReadyMessage = () => { awaitingReady = true; };
    const pending = client.connect();
    await wait(() => awaitingReady);
    const failure = new Error('synthetic disconnect failure before transport closes');
    transport.disconnectError = failure;
    await assert.rejects(client.disconnect(), e => e === failure);
    const stillConnected = transport.state;
    assert.equal(stillConnected, 'connected');
    assert.equal(connects(exporter).length, 0, 'a rejected disconnect cannot finish the pending handshake');
    const disconnectSpan = exporter.getFinishedSpans().find(s => s.attributes['pipecat.operation'] === 'client.disconnect');
    assert.equal(disconnectSpan.status.code, SpanStatusCode.ERROR);
    sendReady();
    const result = await pending;
    const statuses = connects(exporter).map(s => s.status.code);
    console.log(JSON.stringify({ scenario: 'disconnect rejected', sdk: name, stillConnected, botVersion: result.version, statuses }));
    delete transport.disconnectError;
    await client.disconnect();
    assert.deepEqual(statuses, [SpanStatusCode.OK]);
  });
}

for (const name of ['pipecat-floor', '@pipecat-ai/client-js']) {
  test(`${name}: autonomous drop cancels only the abandoned attempt before a gated callback retry`, async t => {
    let retry;
    let count = 0;
    const { client, transport, exporter } = setup(t, name, {
      onDisconnected() { if (++count === 1) retry = client.connect(); },
    });
    const sendReady = transport.sendReadyMessage.bind(transport);
    let awaitingReady = false;
    transport.sendReadyMessage = () => { awaitingReady = true; };
    let settled = false;
    client.connect().finally(() => { settled = true; });
    await wait(() => awaitingReady);
    const original = transport.connect.bind(transport);
    let release;
    const gate = new Promise(resolve => { release = resolve; });
    transport.connect = async (...args) => { await gate; return original(...args); };
    transport.sendReadyMessage = sendReady;
    transport.state = 'disconnected';
    transport.callbacks.onDisconnected();
    await turn();
    assert.equal(settled, false, 'the SDK promise is still abandoned, not changed by instrumentation');
    assert.deepEqual(connects(exporter).map(s => s.status.code), [SpanStatusCode.ERROR]);
    release();
    await retry;
    assert.deepEqual(connects(exporter).map(s => s.status.code), [SpanStatusCode.ERROR, SpanStatusCode.OK]);
    await client.disconnect();
  });

  test(`${name}: disconnect rejection after a real close still cancels the abandoned attempt`, async t => {
    const { client, transport, exporter } = setup(t, name);
    const originalReady = transport.callbacks.onBotReady;
    const originalDisconnected = transport.callbacks.onDisconnected;
    let awaitingReady = false;
    transport.sendReadyMessage = () => { awaitingReady = true; };
    client.connect();
    await wait(() => awaitingReady);
    const disconnect = transport.disconnect.bind(transport);
    const failure = new Error('synthetic post-close failure');
    transport.disconnect = async () => { await disconnect(); throw failure; };
    await assert.rejects(client.disconnect(), e => e === failure);
    assert.deepEqual(connects(exporter).map(s => s.status.code), [SpanStatusCode.ERROR]);
    assert.equal(transport.callbacks.onBotReady, originalReady);
    assert.equal(transport.callbacks.onDisconnected, originalDisconnected);
  });

  test(`${name}: successful disconnect without a transport notification cancels its snapshot`, async t => {
    const { client, transport, exporter } = setup(t, name);
    let awaitingReady = false;
    transport.sendReadyMessage = () => { awaitingReady = true; };
    client.connect();
    await wait(() => awaitingReady);
    transport.disconnect = async () => { transport.state = 'disconnected'; };
    await client.disconnect();
    assert.deepEqual(connects(exporter).map(s => s.status.code), [SpanStatusCode.ERROR]);
  });

  test(`${name}: callback exceptions propagate while the actual drop still completes telemetry`, async t => {
    const failure = new Error('synthetic callback failure');
    const { client, transport, exporter } = setup(t, name, {
      onDisconnected() { throw failure; },
    });
    let awaitingReady = false;
    transport.sendReadyMessage = () => { awaitingReady = true; };
    client.connect();
    await wait(() => awaitingReady);
    transport.state = 'disconnected';
    assert.throws(() => transport.callbacks.onDisconnected(), e => e === failure);
    assert.deepEqual(connects(exporter).map(s => s.status.code), [SpanStatusCode.ERROR]);
  });

  test(`${name}: cleanup does not overwrite a later callback replacement`, async t => {
    const { client, transport, instrumentation } = setup(t, name);
    let awaitingReady = false;
    transport.sendReadyMessage = () => { awaitingReady = true; };
    client.connect();
    await wait(() => awaitingReady);
    const replacement = () => {};
    transport.callbacks.onDisconnected = replacement;
    instrumentation.disable();
    assert.equal(transport.callbacks.onDisconnected, replacement);
  });
}

test('latest: old endpoint bot-ready cannot mark a callback-created failed endpoint retry OK', async t => {
  let retry;
  let first = true;
  let disconnect;
  const { client, transport, exporter } = setup(t, '@pipecat-ai/client-js', {
    onBotReady() {
      if (!first) return;
      first = false;
      disconnect = client.disconnect();
    },
    onDisconnected() {
      if (!retry) {
        retry = client.connect({ endpoint: 'http://127.0.0.1/synthetic/retry' });
        retry.catch(() => {}); // The awaited disconnect now includes deferred cleanup.
      }
    },
  });
  const fetch = globalThis.fetch;
  globalThis.fetch = async () => new Response(JSON.stringify({ transport: 'synthetic' }), { status: 200 });
  t.after(() => { globalThis.fetch = fetch; });
  const original = transport.connect.bind(transport);
  const failure = new Error('synthetic endpoint retry failure');
  let attempts = 0;
  transport.connect = async (...args) => {
    if (++attempts === 1) return original(...args);
    await turn();
    throw failure;
  };
  await client.connect({ endpoint: 'http://127.0.0.1/synthetic/start' });
  await disconnect;
  await assert.rejects(retry, e => e === failure);
  assert.equal(attempts, 2);
  assert.deepEqual(connects(exporter).map(s => s.status.code), [SpanStatusCode.OK, SpanStatusCode.ERROR]);
});

for (const name of ['pipecat-floor', '@pipecat-ai/client-js']) {
  test(`${name}: bot-ready never overrides the rejected overlapping call's own result`, async t => {
    let disconnect;
    const { client, transport, exporter } = setup(t, name, {
      onBotReady() { disconnect = client.disconnect(); },
    });
    const sendReady = transport.sendReadyMessage.bind(transport);
    let awaitingReady = false;
    transport.sendReadyMessage = () => { awaitingReady = true; };
    const first = client.connect();
    await wait(() => awaitingReady);
    // The floor rejects asynchronously; latest throws synchronously. Preserve both.
    let rejected;
    if (name === 'pipecat-floor') rejected = assert.rejects(client.connect(), /already been started/);
    else assert.throws(() => client.connect(), /already started/);
    sendReady();
    await first;
    await rejected;
    await disconnect;
    assert.deepEqual(connects(exporter).map(s => s.status.code).sort(), [SpanStatusCode.OK, SpanStatusCode.ERROR]);
  });

  test(`${name}: a non-writable callback table cannot break connection behavior`, async t => {
    const { client, transport, exporter } = setup(t, name);
    const originalReady = transport.callbacks.onBotReady;
    const originalDisconnected = transport.callbacks.onDisconnected;
    // Force a partial installation failure after onBotReady was wrapped.
    Object.defineProperty(transport.callbacks, 'onDisconnected', { writable: false });
    await client.connect();
    assert.equal(transport.callbacks.onBotReady, originalReady);
    assert.equal(transport.callbacks.onDisconnected, originalDisconnected);
    assert.deepEqual(connects(exporter).map(s => s.status.code), [SpanStatusCode.OK]);
    await client.disconnect();
  });
}

for (const name of ['pipecat-floor', '@pipecat-ai/client-js']) {
  test(`${name}: overlapping media initialization does not retain the abandoned connection`, async t => {
    const { client, transport, exporter, provider, instrumentation } = setup(t, name);
    // The latest SDK initializes only enabled devices. Both real SDK versions
    // permit concurrent connect() calls while media initialization is pending.
    client._options.enableMic = true;
    const originalDisconnected = transport.callbacks.onDisconnected;
    const init = transport.initDevices.bind(transport);
    let release;
    const gate = new Promise(resolve => { release = resolve; });
    transport.initDevices = async () => { await gate; return init(); };
    let firstSettled = false;
    let secondSettled = false;
    client.connect().then(() => { firstSettled = true; }, () => { firstSettled = true; });
    client.connect().then(() => { secondSettled = true; }, () => { secondSettled = true; });
    release();
    await wait(() => secondSettled);
    assert.equal(firstSettled, false, 'the earlier SDK promise is abandoned');
    await client.disconnect();
    await wait(() => connects(exporter).length === 2);
    await provider.forceFlush();
    assert.deepEqual(connects(exporter).map(s => s.status.code).sort(),
      [SpanStatusCode.ERROR, SpanStatusCode.OK].sort());
    assert.equal(instrumentation.pendingConnections.has(client), false);
    assert.equal(transport.callbacks.onDisconnected, originalDisconnected);
    await client.connect();
    await client.disconnect();
    assert.equal(connects(exporter).length, 3, 'a later cycle must not retain the abandoned attempt');
  });
}

// Disposal is allowed immediately after awaited disconnect; no caller should
// need to know that instrumentation scheduled a zero-delay cleanup timer.
for (const name of ['pipecat-floor', '@pipecat-ai/client-js']) {
  for (const disposal of ['forceFlush', 'shutdown']) {
    test(`${name}: ${disposal} after disconnect includes an abandoned bot-ready attempt`, async t => {
      const sdk = require(name);
      const exported = [];
      let closed = false;
      const exporter = {
        export(spans, done) {
          if (!closed) exported.push(...spans);
          done({ code: closed ? 1 : 0 });
        },
        shutdown() { closed = true; return Promise.resolve(); },
      };
      const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
      const manager = new AsyncLocalStorageContextManager().enable();
      context.setGlobalContextManager(manager);
      const instrumentation = new PipecatInstrumentation();
      instrumentation.setTracerProvider(provider);
      instrumentation.manuallyInstrument(sdk);
      const { client, transport } = createClient(sdk);
      client.setLogLevel(sdk.LogLevel.NONE);
      t.after(async () => { instrumentation.disable(); await provider.shutdown(); manager.disable(); context.disable(); });
      client._options.enableMic = true;
      const originalInit = transport.initDevices.bind(transport);
      let release;
      const gate = new Promise(resolve => { release = resolve; });
      transport.initDevices = async () => { await gate; return originalInit(); };
      let firstSettled = false, secondSettled = false;
      client.connect().then(() => { firstSettled = true; }, () => { firstSettled = true; });
      client.connect().then(() => { secondSettled = true; }, () => { secondSettled = true; });
      release();
      await wait(() => secondSettled);
      assert.equal(firstSettled, false, 'SDK abandoned the earlier resolver');
      await client.disconnect();
      await provider[disposal]();
      const spans = exported.filter(s => s.attributes['pipecat.operation'] === 'client.connect');
      assert.equal(spans.length, 2, 'provider disposal must see both attempts');
      assert.deepEqual(spans.map(s => s.status.code).sort(), [SpanStatusCode.OK, SpanStatusCode.ERROR].sort());
    });
  }
}
