const { test } = require('node:test');
const assert = require('node:assert/strict');
const { context, SpanStatusCode } = require('@opentelemetry/api');
const { AsyncLocalStorageContextManager } = require('@opentelemetry/context-async-hooks');
const { BasicTracerProvider, SimpleSpanProcessor } = require('@opentelemetry/sdk-trace-base');
const { PipecatInstrumentation } = require('../dist/src/instrumentation.js');
const { createClient } = require('./transport.cjs');

for (const name of ['pipecat-floor', '@pipecat-ai/client-js']) {
  for (const disposal of ['forceFlush', 'shutdown']) {
    for (const location of ['after notification', 'onDisconnected', 'onBotReady', 'terminal notification after state']) {
      test(`${name}: autonomous drop ${disposal} from ${location} includes every attempt`, async t => {
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
        const sdk = require(name);
        const instrumentation = new PipecatInstrumentation();
        instrumentation.setTracerProvider(provider);
        instrumentation.manuallyInstrument(sdk);
        let disposed;
        let dropped = false;
        const drop = () => {
          dropped = true;
          transport.state = 'disconnected';
          transport.callbacks.onDisconnected();
        };
        let sawProvisionalState = false;
        const { client, transport } = createClient(sdk, {
          onDisconnected() {
            if (location === 'terminal notification after state') assert.ok(sawProvisionalState);
            if (location === 'onDisconnected' || location === 'terminal notification after state') disposed = provider[disposal]();
          },
          onTransportStateChanged(state) {
            if (location === 'terminal notification after state' && state === 'disconnected' && dropped) {
              sawProvisionalState = true;
              // State changes can be relay retries: dispose only at the terminal
              // notification, not before the instrumentation can know it closed.
              assert.deepEqual(exported.filter(s => s.attributes['pipecat.operation'] === 'client.connect')
                .map(s => s.status.code), [SpanStatusCode.OK]);
            }
          },
          onBotReady() {
            if (location === 'onBotReady' && !dropped) {
              drop();
              disposed = provider[disposal]();
            }
          },
        });
        client.setLogLevel(sdk.LogLevel.NONE);
        t.after(async () => { instrumentation.disable(); await provider.shutdown(); manager.disable(); context.disable(); });
        client._options.enableMic = true;
        const init = transport.initDevices.bind(transport);
        let release;
        const gate = new Promise(resolve => { release = resolve; });
        transport.initDevices = async () => { await gate; return init(); };
        let firstSettled = false;
        client.connect().then(() => { firstSettled = true; }, () => { firstSettled = true; });
        const second = client.connect();
        release();
        await second;
        assert.equal(firstSettled, false, 'real SDK replaced the first connection resolver');
        // No client.disconnect(): transport callbacks are the only cancellation signal.
        if (!dropped) drop();
        await (disposed ?? provider[disposal]());
        const spans = exported.filter(s => s.attributes['pipecat.operation'] === 'client.connect');
        assert.equal(spans.length, 2, 'no timer may defer cancellation past provider disposal');
        assert.deepEqual(spans.map(s => s.status.code).sort(), [SpanStatusCode.OK, SpanStatusCode.ERROR].sort());
      });
    }
  }
}
