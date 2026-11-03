import assert from 'node:assert/strict';
import { after, afterEach, beforeEach, test } from 'node:test';
import { require, sdk, agent, SECRET } from './fixture.mjs';
const { Agent, Graph, Swarm } = sdk;
const { NodeTracerProvider } = require('@opentelemetry/sdk-trace-node');
const { InMemorySpanExporter, SimpleSpanProcessor } = require('@opentelemetry/sdk-trace-base');
const { StrandsSpanExporter } = process.env.STRANDS_TEST_PACKED
  ? require('@traceai/strands/native') : await import('../dist/src/native.js');
const { StrandsInstrumentation } = process.env.STRANDS_TEST_PACKED
  ? require('@traceai/strands') : await import('../dist/src/index.js');
const memory = new InMemorySpanExporter();
const raw = new InMemorySpanExporter();
const exporter = new StrandsSpanExporter(memory);
const pending = new Set();
const provider = new NodeTracerProvider({ spanProcessors: [
  { onStart(span) { pending.add(span.spanContext().spanId); },
    onEnd(span) { assert.ok(pending.delete(span.spanContext().spanId), 'span ended twice'); },
    async forceFlush() {}, async shutdown() {} },
  new SimpleSpanProcessor(exporter), new SimpleSpanProcessor(raw),
] });
provider.register();
const tracer = provider.getTracer('application');
beforeEach(() => { memory.reset(); raw.reset(); });
afterEach(() => assert.equal(pending.size, 0, 'native SDK left an unfinished span'));
after(() => provider.shutdown());

const spans = () => memory.getFinishedSpans();
const operation = (span) => span.attributes['gen_ai.operation.name'];
function assertLinks(list) {
  const ids = new Map(list.map((span) => [span.spanContext().spanId, span]));
  for (const span of list) {
    if (span.parentSpanContext) {
      const parent = ids.get(span.parentSpanContext.spanId);
      assert.ok(parent, `missing parent for ${span.name}`);
      assert.equal(span.spanContext().traceId, parent.spanContext().traceId);
    }
  }
}
function assertPrivate(list) {
  const payload = JSON.stringify(list.map((s) => ({ attributes: s.attributes, events: s.events, status: s.status, links: s.links })));
  assert.ok(!payload.includes(SECRET), 'sensitive SDK content reached the exporter');
}
function exportTo(options, list = raw.getFinishedSpans()) {
  const collected = [];
  let callback;
  const sink = { export(batch, cb) { collected.push(...batch); cb({ code: 0 }); },
    async shutdown() {}, async forceFlush() {} };
  new StrandsSpanExporter(sink, options).export(list, (r) => { callback = r; });
  assert.equal(callback.code, 0);
  return collected;
}

test('legacy entry point leaves the real native ESM SDK untouched', () => {
  const original = Agent.prototype.invoke;
  const instrumentation = new StrandsInstrumentation();
  assert.doesNotThrow(() => instrumentation.manuallyInstrument(sdk));
  assert.equal(Agent.prototype.invoke, original);
  instrumentation.disable();
});

test('real agent/tool/model spans retain parents, IDs and count tokens only once', async () => {
  await tracer.startActiveSpan('request', async (span) => {
    try { await agent('tools', 'tool').invoke(SECRET); } finally { span.end(); }
  });
  await provider.forceFlush();
  const list = spans();
  assert.equal(list.filter((s) => operation(s) === 'invoke_agent').length, 1);
  assert.equal(list.filter((s) => operation(s) === 'execute_tool').length, 1, JSON.stringify(raw.getFinishedSpans().map((s) => ({ name: s.name, attributes: s.attributes }))));
  assert.equal(list.filter((s) => operation(s) === 'chat').length, 2);
  assert.equal(list.reduce((n, s) => n + (s.attributes['gen_ai.usage.total_tokens'] ?? 0), 0), 10);
  for (const span of list) {
    if (operation(span) !== 'chat') {
      assert.ok(!Object.keys(span.attributes).some((key) => key.startsWith('gen_ai.usage.')));
    } else {
      assert.equal(span.attributes['fi.span.kind'], 'LLM');
      assert.equal(span.attributes['gen_ai.request.model'], 'fixture-model');
      assert.equal(span.attributes['gen_ai.provider.name'], undefined);
    }
  }
  assert.deepEqual(list.map((s) => s.spanContext()), raw.getFinishedSpans().map((s) => s.spanContext()));
  assert.ok(raw.getFinishedSpans().some((s) => operation(s) === 'invoke_agent' && s.attributes['gen_ai.usage.total_tokens'] === 10), 'upstream aggregate is left untouched');
  assertLinks(list);
  assertPrivate(list);
});

test('stream completion, early return, cancellation and model failure end native spans', async () => {
  for await (const _ of agent('stream').stream(SECRET)) { /* consume SDK stream */ }
  const completed = spans().length;
  assert.ok(completed >= 3);
  memory.reset(); raw.reset();
  let returnedEarly = false;
  for await (const event of agent('early-return').stream(SECRET)) {
    if (event.type === 'modelStreamUpdateEvent') { returnedEarly = true; break; }
  }
  assert.ok(returnedEarly, 'early return must interrupt an actual model stream');
  assert.equal(pending.size, 0);
  await provider.forceFlush();
  assert.equal(spans().filter((s) => operation(s) === 'invoke_agent').length, 1);
  assertPrivate(spans()); assertLinks(spans());
  memory.reset(); raw.reset();
  const cancelled = agent('cancelled');
  let didCancel = false;
  for await (const event of cancelled.stream(SECRET)) {
    if (event.type === 'modelStreamUpdateEvent') { cancelled.cancel(); didCancel = true; }
  }
  assert.ok(didCancel, 'cancellation must interrupt an actual model stream');
  assert.equal(pending.size, 0);
  await provider.forceFlush();
  assert.equal(spans().filter((s) => operation(s) === 'invoke_agent').length, 1);
  assertPrivate(spans()); assertLinks(spans());
  memory.reset(); raw.reset();
  await assert.rejects(agent('failure', 'error').invoke(SECRET), new RegExp(SECRET));
  await provider.forceFlush();
  assert.ok(spans().some((s) => s.status.code === 2));
  assertPrivate(spans()); assertLinks(spans());
});

test('parallel agents keep disjoint traces', async () => {
  await Promise.all([agent('left').invoke(SECRET), agent('right').invoke(SECRET)]);
  await provider.forceFlush();
  const roots = spans().filter((s) => operation(s) === 'invoke_agent');
  assert.equal(roots.length, 2);
  assert.notEqual(roots[0].spanContext().traceId, roots[1].spanContext().traceId);
  assertLinks(spans());
});

test('real graph and swarm preserve native orchestration and node parents', async () => {
  const graph = new Graph({ maxSteps: 4, nodes: [agent('a'), agent('b')], edges: [['a', 'b']] });
  await graph.invoke(SECRET);
  const swarm = new Swarm({ nodes: [agent('swarm-agent', 'swarm')], maxSteps: 2 });
  await swarm.invoke(SECRET);
  await provider.forceFlush();
  for (const op of ['invoke_graph', 'invoke_swarm', 'execute_node']) {
    assert.ok(spans().some((s) => operation(s) === op), `missing ${op}`);
  }
  assertLinks(spans()); assertPrivate(spans());
  for (const span of spans().filter((s) => operation(s) !== 'chat')) {
    assert.equal(span.attributes['gen_ai.usage.total_tokens'], undefined);
    assert.equal(span.attributes['gen_ai.usage.input_tokens'], undefined);
  }
});

test('opt-in maps both native event and canonical-attribute content', async () => {
  const old = process.env.OTEL_SEMCONV_STABILITY_OPT_IN;
  try {
    for (const optIn of ['', 'gen_ai_latest_experimental', 'gen_ai_latest_experimental,gen_ai_span_attributes_only']) {
      process.env.OTEL_SEMCONV_STABILITY_OPT_IN = optIn;
      raw.reset();
      await agent('content', 'tool').invoke(SECRET);
      await provider.forceFlush();
      const exported = exportTo({ captureContent: true });
      const models = exported.filter((s) => operation(s) === 'chat');
      assert.ok(models.every((s) => s.attributes['input.value']?.includes(SECRET)));
      assert.ok(models.some((s) => s.attributes['output.value']?.includes(SECRET)));
      assert.ok(exported.find((s) => operation(s) === 'execute_tool').attributes['input.value'].includes(SECRET));
      assertPrivate(exportTo({}));
    }
  } finally {
    if (old === undefined) delete process.env.OTEL_SEMCONV_STABILITY_OPT_IN;
    else process.env.OTEL_SEMCONV_STABILITY_OPT_IN = old;
  }
});

test('zero and cache usage survive projection; link payloads are private by default', () => {
  const span = provider.getTracer('strands-agents').startSpan('chat', { attributes: {
    'gen_ai.operation.name': 'chat', 'gen_ai.usage.input_tokens': 0,
    'gen_ai.usage.output_tokens': 0, 'gen_ai.usage.total_tokens': 0,
    'gen_ai.usage.cache_read.input_tokens': 0, 'gen_ai.usage.cache_creation.input_tokens': 0,
    'custom.payload': SECRET,
  } });
  span.addLink({ context: span.spanContext(), attributes: { payload: SECRET } });
  span.end();
  const [projected] = spans();
  for (const key of ['input_tokens', 'output_tokens', 'total_tokens', 'cache_read_tokens', 'cache_write_tokens']) {
    assert.equal(projected.attributes[`gen_ai.usage.${key}`], 0);
  }
  assertPrivate(spans());
  const [original] = raw.getFinishedSpans();
  assert.equal(original.links[0].attributes.payload, SECRET);
  assert.equal(projected.resource, original.resource);
  assert.deepEqual(projected.links[0].context, original.links[0].context);
});

test('custom service scope, error opt-in, unrelated spans and lifecycle delegation', async () => {
  const old = process.env.OTEL_SERVICE_NAME;
  process.env.OTEL_SERVICE_NAME = 'custom-strands';
  try {
    await assert.rejects(agent('errors', 'error').invoke(SECRET));
    const filtered = exportTo({ instrumentationScope: 'custom-strands' });
    assertPrivate(filtered);
    assert.ok(JSON.stringify(exportTo({ instrumentationScope: 'custom-strands', captureErrorDetails: true }).map((s) => s.events)).includes(SECRET));
    assert.ok(filtered.every((s) => s.attributes['fi.span.kind']));
  } finally {
    if (old === undefined) delete process.env.OTEL_SERVICE_NAME;
    else process.env.OTEL_SERVICE_NAME = old;
  }
  const unrelated = tracer.startSpan('unrelated'); unrelated.end();
  const last = raw.getFinishedSpans().at(-1);
  assert.equal(exportTo({}, [last])[0], last);
  let shutdown = 0, flush = 0;
  const wrapper = new StrandsSpanExporter({ export(_s, cb) { cb({ code: 1, error: new Error('synthetic export failure') }); },
    async shutdown() { shutdown++; }, async forceFlush() { flush++; } });
  wrapper.export([], (result) => assert.equal(result.code, 1));
  await wrapper.forceFlush(); await wrapper.shutdown();
  assert.equal(shutdown, 1); assert.equal(flush, 1);
});
