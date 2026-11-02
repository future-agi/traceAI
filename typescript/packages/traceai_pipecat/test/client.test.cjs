const { test } = require('node:test');
const assert = require('node:assert/strict');
const { context, trace, SpanStatusCode } = require('@opentelemetry/api');
const { AsyncLocalStorageContextManager } = require('@opentelemetry/context-async-hooks');
const { InMemorySpanExporter, SimpleSpanProcessor, BasicTracerProvider } = require('@opentelemetry/sdk-trace-base');
const { PipecatInstrumentation, isPatched, VERSION } = require('../dist/src/index.js');
const { observePipecatClient } = require('../dist/src/client.js');
const { createClient } = require('./transport.cjs');

function setup(t) {
  const exporter = new InMemorySpanExporter();
  const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
  const instrumentation = new PipecatInstrumentation();
  instrumentation.setTracerProvider(provider);
  t.after(async () => { instrumentation.disable(); await provider.shutdown(); });
  return { exporter, provider, instrumentation };
}

for (const name of ['pipecat-floor', '@pipecat-ai/client-js']) {
  const sdk = require(name);
  test(`${name}: public events preserve callbacks, omit payloads and detach`, async t => {
    const { exporter, provider } = setup(t);
    let callbackCount = 0;
    const { client, transport } = createClient(sdk, { onBotReady: () => callbackCount++ });
    const parent = provider.getTracer('app').startSpan('application');
    const stop = observePipecatClient(client, { tracer: provider.getTracer('events'), parentContext: trace.setSpan(context.active(), parent) });
    t.after(stop);
    assert.equal(observePipecatClient(client), stop);
    await client.connect();
    assert.equal(callbackCount, 1);
    transport.onMessage(new sdk.RTVIMessage('error', { error: 'synthetic-private-error' }));
    await client.disconnect();
    const spans = exporter.getFinishedSpans();
    for (const event of ['connected', 'disconnected', 'botReady', 'error']) {
      assert.equal(spans.filter(s => s.attributes['pipecat.event'] === event).length, 1);
    }
    assert.equal(spans.find(s => s.attributes['pipecat.event'] === 'error').status.code, SpanStatusCode.ERROR);
    assert.ok(spans.every(s => s.spanContext().traceId === parent.spanContext().traceId));
    assert.ok(!JSON.stringify(spans.map(s => [s.attributes, s.status, s.events])).includes('synthetic-private'));
    stop();
    stop();
    assert.equal(client.listenerCount('connected'), 0);
    const count = spans.length;
    await client.connect();
    await client.disconnect();
    assert.equal(exporter.getFinishedSpans().length, count);
    assert.equal(callbackCount, 2);
    parent.end();
  });

  test(`${name}: real operations preserve return/throw behavior and export no content`, async t => {
    const { exporter, instrumentation } = setup(t);
    instrumentation.manuallyInstrument(sdk);
    instrumentation.manuallyInstrument(sdk);
    const { client, transport } = createClient(sdk);
    t.after(async () => { transport.sendError = undefined; await client.disconnect(); });
    await client.connect();
    if (sdk.PipecatClient) {
      const result = client.sendClientMessage('synthetic-private-type', { secret: 'synthetic-private-input' });
      assert.equal(result, undefined);
      assert.equal(typeof await client.sendText('synthetic-private-input'), 'string');
      const reply = await client.sendClientRequest('synthetic-private-type', {});
      assert.equal(reply.result, 'ok');
    } else {
      assert.equal(client.sendMessage(new sdk.RTVIMessage('synthetic-private-type', {})), undefined);
      await client.action({ service: 'synthetic-private-service', action: 'synthetic-private-action', arguments: [] });
    }
    const failure = new Error('synthetic-private-error');
    transport.sendError = failure;
    assert.throws(() => sdk.PipecatClient ? client.sendClientMessage('test', {}) : client.sendMessage(new sdk.RTVIMessage('test', {})), e => e === failure);
    if (sdk.PipecatClient) await assert.rejects(client.sendText('synthetic-private-input'), e => e === failure);
    transport.sendError = undefined;
    await client.disconnect();
    const spans = exporter.getFinishedSpans();
    assert.equal(spans.filter(s => s.attributes['pipecat.operation'] === 'client.connect').length, 1);
    assert.equal(spans.filter(s => s.attributes['pipecat.operation'] === 'client.disconnect').length, 1);
    assert.ok(spans.some(s => s.status.code === SpanStatusCode.ERROR));
    assert.ok(spans.every(s => s.attributes['fi.span.kind'] === 'CHAIN'));
    assert.ok(!JSON.stringify(spans.map(s => [s.name, s.attributes, s.status, s.events])).includes('synthetic-private'));
  });

  test(`${name}: disable, re-enable, multiple instances and modules have independent ownership`, async t => {
    const { instrumentation, provider, exporter } = setup(t);
    const proto = (sdk.PipecatClient ?? sdk.RTVIClient).prototype;
    const original = proto.connect;
    instrumentation.manuallyInstrument(Object.freeze({ ...sdk }));
    assert.equal(isPatched(), true);
    const wrapped = proto.connect;
    assert.notEqual(wrapped, original);
    const second = new PipecatInstrumentation();
    second.setTracerProvider(provider);
    second.manuallyInstrument(sdk);
    second.disable();
    assert.equal(proto.connect, wrapped);
    instrumentation.disable();
    assert.equal(proto.connect, original);
    assert.equal(isPatched(), false);
    instrumentation.enable();
    const { client } = createClient(sdk);
    await client.connect();
    await client.disconnect();
    assert.equal(exporter.getFinishedSpans().length, 2);
    instrumentation.disable();
    assert.equal(proto.connect, original);
  });

  test(`${name}: connection and disconnection rejections complete error spans`, async t => {
    const { instrumentation, exporter } = setup(t);
    instrumentation.manuallyInstrument(sdk);
    const { client, transport } = createClient(sdk);
    const failure = new Error('synthetic-private-failure');
    transport.connectError = failure;
    await assert.rejects(client.connect(), e => e === failure);
    transport.connectError = undefined;
    transport.disconnectError = failure;
    await assert.rejects(client.disconnect(), e => e === failure);
    const errors = exporter.getFinishedSpans().filter(s => s.status.code === SpanStatusCode.ERROR);
    assert.equal(errors.length, 2);
    assert.ok(errors.every(s => !s.status.message && !s.events.length));
  });
}

test('null/absent exports are not patched; runtime version follows package version', t => {
  const { instrumentation } = setup(t);
  instrumentation.manuallyInstrument(null);
  instrumentation.manuallyInstrument({});
  assert.equal(isPatched(), false);
  assert.equal(VERSION, require('../package.json').version);
});

test('one instrumentation can instrument both installed SDK versions', async t => {
  const { instrumentation, exporter } = setup(t);
  for (const name of ['pipecat-floor', '@pipecat-ai/client-js']) {
    const sdk = require(name);
    instrumentation.manuallyInstrument(sdk);
    const { client } = createClient(sdk);
    await client.connect();
    await client.disconnect();
  }
  assert.equal(exporter.getFinishedSpans().length, 4);
});

test('operations inherit the active async context', async t => {
  const manager = new AsyncLocalStorageContextManager().enable();
  context.setGlobalContextManager(manager);
  t.after(() => { manager.disable(); context.disable(); });
  const { instrumentation, provider, exporter } = setup(t);
  const sdk = require('@pipecat-ai/client-js');
  instrumentation.manuallyInstrument(sdk);
  const { client } = createClient(sdk);
  const parent = provider.getTracer('app').startSpan('parent');
  await context.with(trace.setSpan(context.active(), parent), async () => {
    await client.connect();
    await client.sendText('private');
    await client.disconnect();
  });
  assert.ok(exporter.getFinishedSpans().every(s => s.parentSpanId === parent.spanContext().spanId));
  parent.end();
});

test('telemetry failures do not break a real client', async t => {
  const { instrumentation } = setup(t);
  const sdk = require('@pipecat-ai/client-js');
  instrumentation.manuallyInstrument(sdk);
  const { client } = createClient(sdk);
  const tracer = { startSpan() { throw new Error('private diagnostic'); } };
  instrumentation.setTracerProvider({ getTracer: () => tracer });
  const stop = observePipecatClient(client, { tracer });
  t.after(stop);
  await client.connect();
  await client.disconnect();
});
