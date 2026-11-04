const { test } = require('node:test');
const assert = require('node:assert/strict');
const { context, SpanStatusCode } = require('@opentelemetry/api');
const { AsyncLocalStorageContextManager } = require('@opentelemetry/context-async-hooks');
const { BasicTracerProvider, SimpleSpanProcessor, InMemorySpanExporter } = require('@opentelemetry/sdk-trace-base');
const { PipecatInstrumentation } = require('../dist/src');
const { createClient } = require('./transport.cjs');

const connects = spans => spans.filter(s => s.attributes['pipecat.operation'] === 'client.connect');
function setup(t, sdk, mode = 'instrumented') {
  const exporter = new InMemorySpanExporter();
  const started = [], ended = [];
  const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter), {
    onStart(s) { started.push(s); }, onEnd(s) { ended.push(s); }, async shutdown() {}, async forceFlush() {},
  }] });
  const manager = new AsyncLocalStorageContextManager().enable();
  context.setGlobalContextManager(manager);
  const instrumentation = mode === 'instrumented' ? new PipecatInstrumentation() : undefined;
  instrumentation?.setTracerProvider(provider);
  instrumentation?.manuallyInstrument(sdk);
  t.after(async () => { instrumentation?.disable(); await provider.shutdown(); manager.disable(); context.disable(); });
  return { exporter, provider, started, ended, instrumentation };
}
async function ready(check) {
  for (let i = 0; i < 30 && !check(); i++) await new Promise(setImmediate);
  assert.ok(check(), 'transport reached the expected handshake boundary');
}

// Independent reproduction matrix: real floor/latest SDKs, both public callback
// APIs and both close paths. Only the transport's media/network I/O is replaced.
for (const name of ['pipecat-floor', '@pipecat-ai/client-js']) {
  for (const mode of ['control', 'instrumented']) {
    for (const eventSource of ['constructor', 'listener']) {
      for (const closeMode of ['disconnect-call', 'transport-drop']) {
        for (const retryTiming of ['immediate', 'gated']) {
          test(`${name} ${mode} ${eventSource} ${retryTiming} retry at disconnected state during ${closeMode}`, { timeout: 5000 }, async t => {
            const sdk = require(name);
            const { exporter, provider, started, ended, instrumentation } = setup(t, sdk, mode);
            let armed = false, retry;
            const onState = state => {
              if (armed && state === 'disconnected') {
                armed = false;
                retry = client.connect();
                retry.catch(() => {});
              }
            };
            const { client, transport } = createClient(sdk,
              eventSource === 'constructor' ? { onTransportStateChanged: onState } : {});
            client.setLogLevel(sdk.LogLevel.NONE);
            if (eventSource === 'listener') client.on('transportStateChanged', onState);
            const originalCallbacks = { ...transport.callbacks };
            await client.connect();
            let release;
            const gate = new Promise(resolve => { release = resolve; });
            const connect = transport.connect.bind(transport);
            if (retryTiming === 'gated') transport.connect = async (...args) => { await gate; return connect(...args); };
            armed = true;
            if (closeMode === 'disconnect-call') await client.disconnect();
            else { transport.state = 'disconnected'; transport.callbacks.onDisconnected(); }
            assert.ok(retry, 'public state-change callback starts the retry');
            if (retryTiming === 'gated') {
              assert.equal(client.connected, false, 'retry has installed its resolver but not connected yet');
              assert.deepEqual(connects(exporter.getFinishedSpans()).map(s => s.status.code),
                mode === 'instrumented' ? [SpanStatusCode.OK] : [], 'old notification cannot end the gated retry');
            }
            release();
            const result = await retry;
            await provider.forceFlush();
            const attempts = connects(exporter.getFinishedSpans());
            console.log(JSON.stringify({ sdk: name, mode, eventSource, closeMode, retryTiming,
              botVersion: result.version, connected: client.connected, statuses: attempts.map(s => s.status.code) }));
            assert.equal(result.version, '2.3.0');
            assert.equal(client.connected, true);
            assert.deepEqual(attempts.map(s => s.status.code),
              mode === 'instrumented' ? [SpanStatusCode.OK, SpanStatusCode.OK] : []);
            assert.deepEqual(attempts.map(s => s.spanContext().spanId), connects(started).map(s => s.spanContext().spanId));
            assert.equal(connects(ended).length, mode === 'instrumented' ? 2 : 0, 'each attempt ends exactly once');
            await client.disconnect();
            for (const callback of ['onBotReady', 'onDisconnected', 'onTransportStateChanged']) {
              assert.equal(transport.callbacks[callback], originalCallbacks[callback], `${callback} restored after close`);
            }
            assert.equal(instrumentation?.pendingConnections.size ?? 0, 0, 'closed clients are not retained');
          });
        }
      }
    }
  }
}

for (const name of ['pipecat-floor', '@pipecat-ai/client-js']) {
  for (const eventSource of ['constructor', 'listener']) {
    for (const closeMode of ['disconnect-call', 'transport-drop']) {
      test(`${name}: ${eventSource} state retry cancels only the abandoned handshake during ${closeMode}`, async t => {
        const sdk = require(name);
        const { exporter, started, ended } = setup(t, sdk);
        let armed = false, retry;
        const onState = state => {
          if (armed && state === 'disconnected') {
            armed = false;
            // A disconnected state alone may recover. Capture ownership now,
            // but wait for the terminal notification before cancelling the old dial.
            assert.deepEqual(connects(exporter.getFinishedSpans()), []);
            retry = client.connect();
          }
        };
        const { client, transport } = createClient(sdk,
          eventSource === 'constructor' ? { onTransportStateChanged: onState } : {});
        client.setLogLevel(sdk.LogLevel.NONE);
        if (eventSource === 'listener') client.on('transportStateChanged', onState);
        const sendReady = transport.sendReadyMessage.bind(transport);
        let awaitingBot = false, abandonedSettled = false;
        transport.sendReadyMessage = () => { awaitingBot = true; };
        client.connect().then(() => { abandonedSettled = true; }, () => { abandonedSettled = true; });
        await ready(() => awaitingBot);
        transport.sendReadyMessage = sendReady;
        let release;
        const gate = new Promise(resolve => { release = resolve; });
        const connect = transport.connect.bind(transport);
        transport.connect = async (...args) => { await gate; return connect(...args); };
        armed = true;
        if (closeMode === 'disconnect-call') await client.disconnect();
        else { transport.state = 'disconnected'; transport.callbacks.onDisconnected(); }
        assert.ok(retry);
        assert.equal(abandonedSettled, false, 'instrumentation must not settle the SDK-owned promise');
        assert.deepEqual(connects(exporter.getFinishedSpans()).map(s => s.status.code), [SpanStatusCode.ERROR]);
        release();
        assert.equal((await retry).version, '2.3.0');
        const attempts = connects(exporter.getFinishedSpans());
        assert.deepEqual(attempts.map(s => s.status.code), [SpanStatusCode.ERROR, SpanStatusCode.OK]);
        assert.deepEqual(attempts.map(s => s.spanContext().spanId), connects(started).map(s => s.spanContext().spanId));
        assert.equal(connects(ended).length, 2);
        assert.equal(abandonedSettled, false);
        await client.disconnect();
      });
    }
  }

  for (const eventSource of ['constructor', 'listener']) {
    test(`${name}: nested ${eventSource} state retries consume only their own close notifications`, async t => {
      const sdk = require(name);
      const { exporter, started, ended } = setup(t, sdk);
      let armed = false, notifications = 0, retry, nestedClose, abandonedSettled = false;
      const onState = state => {
        if (!armed || state !== 'disconnected') return;
        if (++notifications === 1) {
          client.connect().then(() => { abandonedSettled = true; }, () => { abandonedSettled = true; });
          nestedClose = client.disconnect();
        } else if (notifications === 2) {
          armed = false;
          retry = client.connect();
        }
      };
      const { client, transport } = createClient(sdk,
        eventSource === 'constructor' ? { onTransportStateChanged: onState } : {});
      client.setLogLevel(sdk.LogLevel.NONE);
      if (eventSource === 'listener') client.on('transportStateChanged', onState);
      await client.connect();
      armed = true;
      transport.state = 'disconnected';
      transport.callbacks.onDisconnected();
      await nestedClose;
      assert.ok(retry);
      assert.equal((await retry).version, '2.3.0');
      assert.equal(abandonedSettled, false, 'nested close must not change SDK promise ownership');
      const attempts = connects(exporter.getFinishedSpans());
      assert.deepEqual(attempts.map(s => s.status.code), [SpanStatusCode.OK, SpanStatusCode.ERROR, SpanStatusCode.OK]);
      assert.deepEqual(attempts.map(s => s.spanContext().spanId), connects(started).map(s => s.spanContext().spanId));
      assert.equal(connects(ended).length, 3);
      await client.disconnect();
    });
  }

  test(`${name}: notification-only transport drops still cancel pending attempts synchronously`, async t => {
    const sdk = require(name);
    const { exporter, instrumentation } = setup(t, sdk);
    const { client, transport } = createClient(sdk, {
      onDisconnected() {
        assert.deepEqual(connects(exporter.getFinishedSpans()).map(s => s.status.code), [SpanStatusCode.ERROR]);
      },
    });
    client.setLogLevel(sdk.LogLevel.NONE);
    let awaitingBot = false;
    transport.sendReadyMessage = () => { awaitingBot = true; };
    client.connect();
    await ready(() => awaitingBot);
    // Some transports omit the separate state event. Do not remove this cleanup.
    transport._state = 'disconnected';
    transport.callbacks.onDisconnected();
    assert.equal(instrumentation.pendingConnections.size, 0);
  });

  test(`${name}: disabling a settled connected client restores every lifecycle callback`, async t => {
    const sdk = require(name);
    const { instrumentation } = setup(t, sdk);
    const { client, transport } = createClient(sdk);
    client.setLogLevel(sdk.LogLevel.NONE);
    const originals = { ...transport.callbacks };
    await client.connect();
    instrumentation.disable();
    for (const callback of ['onBotReady', 'onDisconnected', 'onTransportStateChanged']) {
      assert.equal(transport.callbacks[callback], originals[callback]);
    }
    assert.equal(instrumentation.pendingConnections.size, 0);
    await client.disconnect();
  });
}
