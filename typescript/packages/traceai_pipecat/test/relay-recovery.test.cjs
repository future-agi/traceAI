const { test } = require('node:test');
const assert = require('node:assert/strict');
const { context, SpanStatusCode } = require('@opentelemetry/api');
const { AsyncLocalStorageContextManager } = require('@opentelemetry/context-async-hooks');
const { BasicTracerProvider, SimpleSpanProcessor, InMemorySpanExporter } = require('@opentelemetry/sdk-trace-base');
const { PipecatInstrumentation } = require('../dist/src');
const { createClient } = require('./transport.cjs');

const connects = spans => spans.filter(s => s.attributes['pipecat.operation'] === 'client.connect');
const tick = () => new Promise(setImmediate);
function setup(t, sdk, instrument = true) {
  const exporter = new InMemorySpanExporter();
  const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
  const manager = new AsyncLocalStorageContextManager().enable();
  context.setGlobalContextManager(manager);
  const instrumentation = instrument ? new PipecatInstrumentation() : undefined;
  instrumentation?.setTracerProvider(provider);
  instrumentation?.manuallyInstrument(sdk);
  t.after(async () => { instrumentation?.disable(); await provider.shutdown(); manager.disable(); context.disable(); });
  return { exporter, instrumentation };
}
function assertReleased(instrumentation, transport, originals) {
  assert.equal(instrumentation?.pendingConnections.size ?? 0, 0, 'closed clients are not retained');
  for (const name of ['onBotReady', 'onDisconnected', 'onTransportStateChanged']) {
    assert.equal(transport.callbacks[name], originals[name], `${name} restored`);
  }
}

// MoQ v0.3.2 _onRelayStatus mirrors a failed dial as disconnected, then retries
// without onDisconnected. Replay that state contract through the real SDKs;
// only transport I/O is replaced. A transient state is not a terminal close.
for (const name of ['pipecat-floor', '@pipecat-ai/client-js']) {
  for (const instrument of [false, true]) {
    for (const retries of [1, 3]) {
      test(`${name}: relay recovers after ${retries} disconnected states; instrument=${instrument}`, { timeout: 5000 }, async t => {
        const sdk = require(name);
        const { exporter, instrumentation } = setup(t, sdk, instrument);
        let terminalNotifications = 0;
        const { client, transport } = createClient(sdk, { onDisconnected() { terminalNotifications++; } });
        client.setLogLevel(sdk.LogLevel.NONE);
        const originals = { ...transport.callbacks };
        const connect = transport.connect.bind(transport);
        transport.connect = async () => {
          for (let i = 0; i < retries; i++) {
            transport.state = 'connecting';
            transport.state = 'disconnected';
            await tick();
          }
          transport.state = 'connecting';
          return connect();
        };
        const result = await client.connect();
        assert.equal(result.version, '2.3.0');
        assert.equal(client.connected, true);
        assert.equal(terminalNotifications, 0);
        const statuses = connects(exporter.getFinishedSpans()).map(s => s.status.code);
        console.log(JSON.stringify({ sdk: name, instrument, retries, connected: client.connected, statuses }));
        assert.deepEqual(statuses, instrument ? [SpanStatusCode.OK] : []);
        assert.equal(instrumentation?.pendingConnections.get(client)?.disconnections.length ?? 0, 0,
          'state-only boundaries cannot accumulate across relay dials');
        await client.disconnect();
        assertReleased(instrumentation, transport, originals);
      });
    }
  }

  test(`${name}: pending relay remains observed until terminal notification`, async t => {
    const sdk = require(name);
    const { exporter, instrumentation } = setup(t, sdk);
    const { client, transport } = createClient(sdk, {
      onDisconnected() {
        assert.deepEqual(connects(exporter.getFinishedSpans()).map(s => s.status.code), [SpanStatusCode.ERROR],
          'terminal notification finishes before application callbacks');
      },
    });
    client.setLogLevel(sdk.LogLevel.NONE);
    const originals = { ...transport.callbacks };
    let waiting = false, settled = false;
    transport.sendReadyMessage = () => { waiting = true; };
    client.connect().then(() => { settled = true; }, () => { settled = true; });
    for (let i = 0; i < 30 && !waiting; i++) await tick();
    assert.ok(waiting);
    transport.state = 'disconnected';
    await tick();
    assert.deepEqual(connects(exporter.getFinishedSpans()), [], 'recoverable state must not end the attempt');
    const pending = instrumentation.pendingConnections.get(client);
    assert.equal(pending.attempts.size, 1);
    assert.equal(pending.disconnections.length, 0, 'state-only bookkeeping is retired');
    transport.callbacks.onDisconnected();
    assert.equal(settled, false, 'instrumentation does not settle SDK-owned promises');
    assertReleased(instrumentation, transport, originals);
  });

  test(`${name}: state-only close releases settled client and cannot mask a later terminal close`, async t => {
    const sdk = require(name);
    const { exporter, instrumentation } = setup(t, sdk);
    const { client, transport } = createClient(sdk);
    client.setLogLevel(sdk.LogLevel.NONE);
    const originals = { ...transport.callbacks };
    await client.connect();
    transport.state = 'disconnected';
    await tick();
    assertReleased(instrumentation, transport, originals);
    let waiting = false;
    transport.sendReadyMessage = () => { waiting = true; };
    client.connect();
    for (let i = 0; i < 30 && !waiting; i++) await tick();
    assert.ok(waiting);
    transport._state = 'disconnected';
    transport.callbacks.onDisconnected();
    assert.deepEqual(connects(exporter.getFinishedSpans()).map(s => s.status.code), [SpanStatusCode.OK, SpanStatusCode.ERROR]);
    assertReleased(instrumentation, transport, originals);
  });
}
