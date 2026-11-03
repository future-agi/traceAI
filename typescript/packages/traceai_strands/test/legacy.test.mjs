import assert from 'node:assert/strict';
import { test } from 'node:test';
import { require, sdk, agent, SECRET } from './fixture.mjs';
const { NodeTracerProvider } = require('@opentelemetry/sdk-trace-node');
const { SimpleSpanProcessor, InMemorySpanExporter } = require('@opentelemetry/sdk-trace-base');
const { StrandsInstrumentation, isPatched } = process.env.STRANDS_TEST_PACKED
  ? require('@traceai/strands') : await import('../dist/src/index.js');

test('real SDK 0.1 ESM invocation, privacy config, idempotency and disable stay compatible', async () => {
  assert.equal(sdk.AgentTrace, undefined, 'this contract targets the legacy SDK');
  const memory = new InMemorySpanExporter();
  const provider = new NodeTracerProvider({ spanProcessors: [new SimpleSpanProcessor(memory)] });
  provider.register();
  const original = sdk.Agent.prototype.invoke;
  const instrumentor = new StrandsInstrumentation({ traceConfig: { hideInputs: true, hideOutputs: true } });
  instrumentor.setTracerProvider(provider);
  try {
    instrumentor.manuallyInstrument(sdk);
    instrumentor.manuallyInstrument(sdk);
    assert.ok(isPatched());
    const result = await agent('legacy').invoke(SECRET);
    assert.ok(result);
    await provider.forceFlush();
    const list = memory.getFinishedSpans();
    assert.equal(list.length, 1, 'exactly one legacy outer span');
    assert.equal(list[0].attributes['fi.span.kind'], 'AGENT');
    assert.ok(!String(list[0].attributes['input.value']).includes(SECRET));
    assert.ok(!String(list[0].attributes['output.value']).includes(SECRET));
    await assert.rejects(agent('legacy-error', 'error').invoke(SECRET));
    await provider.forceFlush();
    assert.equal(memory.getFinishedSpans().length, 2);
    assert.equal(memory.getFinishedSpans()[1].status.code, 2);
  } finally {
    instrumentor.disable();
    assert.equal(sdk.Agent.prototype.invoke, original);
    assert.equal(isPatched(), false);
    await provider.shutdown();
  }
});
