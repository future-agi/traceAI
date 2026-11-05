const { test } = require('node:test');
const assert = require('node:assert/strict');
const { context, SpanStatusCode } = require('@opentelemetry/api');
const { AsyncLocalStorageContextManager } = require('@opentelemetry/context-async-hooks');
const { BasicTracerProvider, SimpleSpanProcessor, InMemorySpanExporter } = require('@opentelemetry/sdk-trace-base');
const { PipecatInstrumentation } = require('../dist/src/instrumentation.js');
const { createClient } = require('./transport.cjs');

const connects = spans => spans.filter(s => s.attributes['pipecat.operation'] === 'client.connect');
function setup(t, name, callbacks) {
  const sdk = require(name);
  const exporter = new InMemorySpanExporter();
  const started = [];
  const ended = [];
  const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter), {
    onStart(span) { started.push(span); }, onEnd(span) { ended.push(span); }, async forceFlush() {}, async shutdown() {},
  }] });
  const manager = new AsyncLocalStorageContextManager().enable();
  context.setGlobalContextManager(manager);
  const instrumentation = new PipecatInstrumentation();
  instrumentation.setTracerProvider(provider);
  instrumentation.manuallyInstrument(sdk);
  // Use the real SDK's public constructor options and synchronous initialized event.
  const { client, transport } = createClient(sdk, callbacks, { enableMic: true });
  client.setLogLevel(sdk.LogLevel.NONE);
  t.after(async () => { instrumentation.disable(); await provider.shutdown(); manager.disable(); context.disable(); });
  return { client, transport, exporter, started, ended, instrumentation };
}

for (const name of ['pipecat-floor', '@pipecat-ai/client-js']) {
  for (const callbackStyle of ['listener', 'constructor']) {
    for (const cleanup of ['disconnect', 'disable']) {
      test(`${name}: synchronous initialized ${callbackStyle} reentry retains exclusive resolver ownership until ${cleanup}`, async t => {
        let nested;
        let reentered = false;
        let outerSettled = false;
        const onInitialized = state => {
          if (state === 'initialized' && !reentered) {
            reentered = true;
            nested = client.connect();
          }
        };
        const { client, transport, exporter, started, ended, instrumentation } = setup(t, name,
          callbackStyle === 'constructor' ? { onTransportStateChanged: onInitialized } : {});
        if (callbackStyle === 'listener') client.on('transportStateChanged', onInitialized);
        const originalReady = transport.callbacks.onBotReady;
        const originalDisconnected = transport.callbacks.onDisconnected;
        client.connect().then(() => { outerSettled = true; }, () => { outerSettled = true; });
        assert.ok(nested, 'initialization must reenter connect before the outer call returns');
        assert.equal((await nested).version, '2.3.0');
        await new Promise(setImmediate);
        assert.equal(outerSettled, false, 'upstream abandons the outer promise; tracing must not settle it');
        const attempts = connects(started);
        assert.equal(attempts.length, 2);
        const finished = connects(exporter.getFinishedSpans());
        assert.equal(finished.length, 1, 'only the nested resolver owner may finish at bot-ready');
        assert.equal(finished[0].spanContext().spanId, attempts[1].spanContext().spanId);
        assert.equal(finished[0].parentSpanId, attempts[0].spanContext().spanId);
        assert.equal(finished[0].status.code, SpanStatusCode.OK);
        if (cleanup === 'disconnect') await client.disconnect();
        else instrumentation.disable();
        const closed = connects(exporter.getFinishedSpans());
        assert.deepEqual(closed.map(s => s.status.code), [SpanStatusCode.OK, SpanStatusCode.ERROR]);
        assert.equal(closed[1].spanContext().spanId, attempts[0].spanContext().spanId);
        assert.equal(outerSettled, false, 'cancellation must not alter the abandoned SDK promise');
        assert.equal(transport.callbacks.onBotReady, originalReady);
        assert.equal(transport.callbacks.onDisconnected, originalDisconnected);
        // Repeated bot-ready and disconnect notifications must never end either span again.
        transport.sendReadyMessage();
        await client.disconnect();
        assert.equal(connects(ended).length, 2);
        assert.equal(outerSettled, false);
        instrumentation.enable();
        await client.connect();
        await client.disconnect();
        assert.deepEqual(connects(exporter.getFinishedSpans()).map(s => s.status.code),
          [SpanStatusCode.OK, SpanStatusCode.ERROR, SpanStatusCode.OK]);
        assert.equal(connects(ended).length, 3, 'later connections must start with fresh ownership');
      });
    }
  }
}
