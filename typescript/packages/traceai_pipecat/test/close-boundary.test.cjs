const { test } = require('node:test');
const assert = require('node:assert/strict');
const { context, SpanStatusCode } = require('@opentelemetry/api');
const { AsyncLocalStorageContextManager } = require('@opentelemetry/context-async-hooks');
const { BasicTracerProvider, SimpleSpanProcessor, InMemorySpanExporter } = require('@opentelemetry/sdk-trace-base');
const { PipecatInstrumentation } = require('../dist/src');
const { createClient } = require('./transport.cjs');

const connects = spans => spans.filter(s => s.attributes['pipecat.operation'] === 'client.connect');
function setup(t, sdk, instrument = true) {
  const exporter = new InMemorySpanExporter();
  const started = [], ended = [];
  const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter), {
    onStart(s) { started.push(s); }, onEnd(s) { ended.push(s); }, async shutdown() {}, async forceFlush() {},
  }] });
  const manager = new AsyncLocalStorageContextManager().enable();
  context.setGlobalContextManager(manager);
  const instrumentation = instrument ? new PipecatInstrumentation() : undefined;
  instrumentation?.setTracerProvider(provider);
  instrumentation?.manuallyInstrument(sdk);
  t.after(async () => { instrumentation?.disable(); await provider.shutdown(); manager.disable(); context.disable(); });
  return { exporter, started, ended, instrumentation };
}
function assertReleased(instrumentation, transport, originals) {
  assert.equal(instrumentation?.pendingConnections.size ?? 0, 0, 'closed clients are not retained');
  for (const name of ['onBotReady', 'onDisconnected', 'onTransportStateChanged']) {
    assert.equal(transport.callbacks[name], originals[name], `${name} restored`);
  }
}
function assertConnects(exporter, started, ended, statuses) {
  const spans = connects(exporter.getFinishedSpans());
  assert.deepEqual(spans.map(s => s.status.code), statuses);
  assert.deepEqual(spans.map(s => s.spanContext().spanId), connects(started).map(s => s.spanContext().spanId));
  assert.equal(connects(ended).length, statuses.length, 'each connection span ends exactly once');
}
async function ready(check) {
  for (let i = 0; i < 30 && !check(); i++) await new Promise(setImmediate);
  assert.ok(check(), 'real SDK reached its bot-ready wait');
}

// Legacy disconnect() calls transport.initialize() after awaiting disconnect().
// The transport must notify its initial state, just like Daily/WebSocket, rather
// than silently assigning _state and hiding the SDK's trailing notification.
for (const name of ['pipecat-floor', '@pipecat-ai/client-js']) {
  for (const instrument of [false, true]) {
    for (const source of ['constructor', 'listener']) {
      for (const event of ['onDisconnected', 'onTransportStateChanged']) {
        for (const timing of ['immediate', 'gated']) {
          test(`${name}: ${source} ${event} ${timing} retry survives transport initialization; instrument=${instrument}`, { timeout: 5000 }, async t => {
            const sdk = require(name);
            const { exporter, started, ended, instrumentation } = setup(t, sdk, instrument);
            let armed = false, retry;
            const onClose = state => {
              if (armed && (event === 'onDisconnected' || state === 'disconnected')) {
                armed = false;
                retry = client.connect();
                retry.catch(() => {});
              }
            };
            const { client, transport } = createClient(sdk, source === 'constructor' ? { [event]: onClose } : {});
            client.setLogLevel(sdk.LogLevel.NONE);
            if (source === 'listener') client.on(event === 'onDisconnected' ? 'disconnected' : 'transportStateChanged', onClose);
            const originals = { ...transport.callbacks };
            await client.connect();
            let release;
            const gate = new Promise(resolve => { release = resolve; });
            const connect = transport.connect.bind(transport);
            if (timing === 'gated') transport.connect = async (...args) => { await gate; return connect(...args); };
            armed = true;
            await client.disconnect();
            assert.ok(retry);
            if (timing === 'gated') {
              assert.deepEqual(connects(exporter.getFinishedSpans()).map(s => s.status.code), instrument ? [SpanStatusCode.OK] : []);
            }
            release();
            assert.equal((await retry).version, '2.3.0');
            assert.equal(client.connected, true);
            assertConnects(exporter, started, ended, instrument ? [SpanStatusCode.OK, SpanStatusCode.OK] : []);
            await client.disconnect();
            assertReleased(instrumentation, transport, originals);
          });
        }
      }
    }
  }

  for (const source of ['constructor', 'listener']) {
    for (const close of ['disconnect-call', 'transport-drop']) {
      for (const initial of ['connected', 'handshaking']) {
        for (const retryBeforeThrow of [false, true]) {
          test(`${name}: throwing ${source} state callback during ${close}, ${initial}, retry=${retryBeforeThrow}`, { timeout: 5000 }, async t => {
            const sdk = require(name);
            const { exporter, started, ended, instrumentation } = setup(t, sdk);
            let armed = false, retry, release;
            const boom = new Error('synthetic disconnected callback failure');
            const onState = state => {
              if (!armed || state !== 'disconnected') return;
              armed = false;
              if (retryBeforeThrow) {
                retry = client.connect();
                retry.catch(() => {});
              }
              throw boom;
            };
            const { client, transport } = createClient(sdk, source === 'constructor' ? { onTransportStateChanged: onState } : {});
            client.setLogLevel(sdk.LogLevel.NONE);
            if (source === 'listener') client.on('transportStateChanged', onState);
            const originals = { ...transport.callbacks };
            const sendReady = transport.sendReadyMessage.bind(transport);
            let awaitingBot = false, initialSettled = false;
            if (initial === 'handshaking') transport.sendReadyMessage = () => { awaitingBot = true; };
            const first = client.connect();
            first.then(() => { initialSettled = true; }, () => { initialSettled = true; });
            if (initial === 'connected') await first;
            else await ready(() => awaitingBot);
            transport.sendReadyMessage = sendReady;
            const gate = new Promise(resolve => { release = resolve; });
            const connect = transport.connect.bind(transport);
            if (retryBeforeThrow) transport.connect = async (...args) => { await gate; return connect(...args); };
            armed = true;
            if (close === 'disconnect-call') await assert.rejects(client.disconnect(), error => error === boom);
            else assert.throws(() => { transport.state = 'disconnected'; transport.callbacks.onDisconnected(); }, error => error === boom);
            const expected = [initial === 'connected' ? SpanStatusCode.OK : SpanStatusCode.ERROR];
            assert.deepEqual(connects(exporter.getFinishedSpans()).map(s => s.status.code), expected);
            assert.equal(initialSettled, initial === 'connected', 'abandoned SDK promises stay untouched');
            if (retryBeforeThrow) {
              assert.ok(retry);
              const pending = instrumentation.pendingConnections.get(client);
              assert.equal(pending.attempts.size, 1, 'the genuinely active retry keeps its lifecycle observers');
              assert.equal(pending.disconnections.length, 0, 'the aborted dispatch retires its own boundary');
              release();
              assert.equal((await retry).version, '2.3.0');
              assert.equal(client.connected, true);
              expected.push(SpanStatusCode.OK);
              await client.disconnect();
            } else {
              assert.equal(client.connected, false);
              release();
            }
            assertConnects(exporter, started, ended, expected);
            assertReleased(instrumentation, transport, originals);
          });
        }
      }
    }
  }
}

for (const name of ['pipecat-floor', '@pipecat-ai/client-js']) {
  for (const source of ['constructor', 'listener']) {
    test(`${name}: nested throwing ${source} dispatch retires only its own close boundary`, { timeout: 5000 }, async t => {
      const sdk = require(name);
      const { exporter, started, ended, instrumentation } = setup(t, sdk);
      let armed = false, notifications = 0, nestedClose, retry, release;
      const boom = new Error('synthetic nested state callback failure');
      const onState = state => {
        if (!armed || state !== 'disconnected') return;
        if (++notifications === 1) {
          client.connect().catch(() => {});
          nestedClose = client.disconnect();
          nestedClose.catch(() => {});
        } else {
          armed = false;
          retry = client.connect();
          retry.catch(() => {});
          throw boom;
        }
      };
      const { client, transport } = createClient(sdk, source === 'constructor' ? { onTransportStateChanged: onState } : {});
      client.setLogLevel(sdk.LogLevel.NONE);
      if (source === 'listener') client.on('transportStateChanged', onState);
      const originals = { ...transport.callbacks };
      await client.connect();
      const gate = new Promise(resolve => { release = resolve; });
      const connect = transport.connect.bind(transport);
      transport.connect = async (...args) => { await gate; return connect(...args); };
      armed = true;
      await client.disconnect();
      await assert.rejects(nestedClose, error => error === boom);
      assert.ok(retry);
      assert.equal(instrumentation.pendingConnections.get(client).disconnections.length, 0);
      release();
      assert.equal((await retry).version, '2.3.0');
      assertConnects(exporter, started, ended, [SpanStatusCode.OK, SpanStatusCode.ERROR, SpanStatusCode.OK]);
      await client.disconnect();
      assertReleased(instrumentation, transport, originals);
    });
  }
}

for (const source of ['constructor', 'listener']) {
  test(`pipecat-floor: throwing ${source} reinitialization callback preserves a retry and restores drop observation`, { timeout: 5000 }, async t => {
    const sdk = require('pipecat-floor');
    const { exporter, started, ended, instrumentation } = setup(t, sdk);
    let armed = false, resetExpected = false, retry, release;
    const boom = new Error('synthetic reset callback failure');
    const callbacks = {
      onDisconnected() {
        if (!armed) return;
        armed = false;
        retry = client.connect();
        retry.catch(() => {});
        resetExpected = true;
      },
      onTransportStateChanged(state) {
        if (resetExpected && state === 'disconnected') {
          resetExpected = false;
          throw boom;
        }
      },
    };
    const { client, transport } = createClient(sdk, source === 'constructor' ? callbacks : {});
    client.setLogLevel(sdk.LogLevel.NONE);
    if (source === 'listener') {
      client.on('disconnected', callbacks.onDisconnected);
      client.on('transportStateChanged', callbacks.onTransportStateChanged);
    }
    const originals = { ...transport.callbacks };
    await client.connect();
    const gate = new Promise(resolve => { release = resolve; });
    const connect = transport.connect.bind(transport);
    transport.connect = async (...args) => { await gate; return connect(...args); };
    armed = true;
    await assert.rejects(client.disconnect(), error => error === boom);
    assert.ok(retry);
    const pending = instrumentation.pendingConnections.get(client);
    assert.equal(pending.attempts.size, 1);
    assert.equal(pending.disconnections.length, 0);
    release();
    assert.equal((await retry).version, '2.3.0');
    assertConnects(exporter, started, ended, [SpanStatusCode.OK, SpanStatusCode.OK]);
    transport.state = 'disconnected';
    transport.callbacks.onDisconnected();
    assertReleased(instrumentation, transport, originals);
  });
}

// Ignoring initialize()'s default state must not ignore a real drop triggered
// inside that application callback, or async work inherited from initialize().
for (const source of ['constructor', 'listener']) {
  for (const timing of ['synchronous', 'microtask']) {
    test(`pipecat-floor: genuine ${timing} drop from ${source} reinitialization callback still cancels`, { timeout: 5000 }, async t => {
      const sdk = require('pipecat-floor');
      const { exporter, started, ended, instrumentation } = setup(t, sdk);
      let armed = false, resetExpected = false, retry, release;
      const callbacks = {
        onDisconnected() {
          if (!armed) return;
          armed = false;
          retry = client.connect();
          retry.catch(() => {});
          resetExpected = true;
        },
        onTransportStateChanged(state) {
          if (!resetExpected || state !== 'disconnected') return;
          resetExpected = false;
          const drop = () => { transport.state = 'disconnected'; transport.callbacks.onDisconnected(); };
          if (timing === 'synchronous') drop();
          else queueMicrotask(drop);
        },
      };
      const { client, transport } = createClient(sdk, source === 'constructor' ? callbacks : {});
      client.setLogLevel(sdk.LogLevel.NONE);
      if (source === 'listener') {
        client.on('disconnected', callbacks.onDisconnected);
        client.on('transportStateChanged', callbacks.onTransportStateChanged);
      }
      const originals = { ...transport.callbacks };
      await client.connect();
      const gate = new Promise(resolve => { release = resolve; });
      const connect = transport.connect.bind(transport);
      transport.connect = async (...args) => { await gate; return connect(...args); };
      armed = true;
      await client.disconnect();
      assert.ok(retry);
      assert.deepEqual(connects(exporter.getFinishedSpans()).map(s => s.status.code), [SpanStatusCode.OK, SpanStatusCode.ERROR]);
      assertReleased(instrumentation, transport, originals);
      release();
      assert.equal((await retry).version, '2.3.0', 'SDK promise may still resolve after cancellation');
      assertConnects(exporter, started, ended, [SpanStatusCode.OK, SpanStatusCode.ERROR]);
      assertReleased(instrumentation, transport, originals);
      await client.disconnect();
    });
  }
}
