import { PortkeyInstrumentation, isPatched, _resetPatchedStateForTesting } from '../instrumentation';
import { VERSION } from '../version';
import { InMemorySpanExporter, SimpleSpanProcessor } from '@opentelemetry/sdk-trace-base';
import { NodeTracerProvider } from '@opentelemetry/sdk-trace-node';
import { context, trace } from '@opentelemetry/api';
import { isTracingSuppressed, suppressTracing } from '@opentelemetry/core';

const exporter = new InMemorySpanExporter();
const provider = new NodeTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
provider.register();
let inst: PortkeyInstrumentation;
const req = { model: 'test', messages: [] };
const setup = (create: Function) => {
  const client = { chat: { completions: { create } } };
  inst.manuallyInstrument(client);
  return client.chat.completions;
};
beforeEach(() => {
  exporter.reset();
  inst = new PortkeyInstrumentation();
  inst.setTracerProvider(provider);
});
afterEach(() => { inst.disable(); _resetPatchedStateForTesting(); });
afterAll(async () => { await provider.shutdown(); });

it('keeps public instrumentation metadata and rejects empty modules without claiming a patch', () => {
  expect(inst.instrumentationVersion).toBe(VERSION);
  expect(inst.instrumentationName).toBe('@traceai/fi-instrumentation-portkey');
  inst.manuallyInstrument(null);
  inst.manuallyInstrument({ Portkey: {} });
  expect(isPatched()).toBe(false);
});

it('preserves synchronous throws and their identity', () => {
  const error = new Error('synthetic');
  const resource = setup(() => { throw error; });
  expect(() => resource.create(req)).toThrow(error);
  expect(exporter.getFinishedSpans()[0].status.code).toBe(2);
});

it('keeps parent context, suppresses nested provider instrumentation, and honors suppression', async () => {
  const parent = provider.getTracer('test').startSpan('parent');
  let activeId: string | undefined;
  let suppressed = false;
  const resource = setup(async () => {
    await Promise.resolve();
    activeId = trace.getSpan(context.active())?.spanContext().spanId;
    suppressed = isTracingSuppressed(context.active());
    return {};
  });
  await context.with(trace.setSpan(context.active(), parent), () => resource.create(req));
  const [span] = exporter.getFinishedSpans();
  expect(span.parentSpanContext?.spanId).toBe(parent.spanContext().spanId);
  expect(activeId).toBe(span.spanContext().spanId);
  expect(suppressed).toBe(true);
  await context.with(suppressTracing(context.active()), () => resource.create(req));
  expect(exporter.getFinishedSpans()).toHaveLength(1);
  parent.end();
});

it('preserves stream class methods, context, return before next and errors', async () => {
  const error = new Error('stream-failure');
  let activeId: string | undefined;
  let closed = false;
  class Stream {
    marker() { return 'same-stream-api'; }
    [Symbol.asyncIterator]() {
      return {
        async next(): Promise<IteratorResult<any>> {
          activeId = trace.getSpan(context.active())?.spanContext().spanId;
          throw error;
        },
        async return() { closed = true; return { done: true, value: undefined }; },
      };
    }
  }
  const resource = setup(async () => new Stream());
  const stream = await resource.create(req);
  expect(stream).toBeInstanceOf(Stream);
  expect(stream.marker()).toBe('same-stream-api');
  await expect(stream[Symbol.asyncIterator]().next()).rejects.toBe(error);
  const [span] = exporter.getFinishedSpans();
  expect(span.status.code).toBe(2);
  expect(activeId).toBe(span.spanContext().spanId);
  const fresh = await resource.create(req);
  await fresh[Symbol.asyncIterator]().return();
  expect(closed).toBe(true);
  expect(exporter.getFinishedSpans()).toHaveLength(2);
});

it('bounds streaming content without buffering all chunks', async () => {
  const resource = setup(async () => (async function* () {
    for (let i = 0; i < 100; i++) yield { choices: [{ delta: { content: 'x'.repeat(1024) } }] };
  })());
  let count = 0;
  for await (const _ of await resource.create(req)) count++;
  const [span] = exporter.getFinishedSpans();
  expect(count).toBe(100);
  expect(span.attributes['portkey.output_truncated']).toBe(true);
  expect((span.attributes['output.value'] as string).length).toBe(65536);
});

it('does not duplicate spans with two instrumentors or independently loaded modules', async () => {
  const second = new PortkeyInstrumentation();
  second.setTracerProvider(provider);
  try {
    const a = setup(async () => ({}));
    second.manuallyInstrument({ chat: { completions: a } });
    const b = setup(async () => ({}));
    await a.create(req);
    await b.create(req);
    expect(exporter.getFinishedSpans()).toHaveLength(2);
  } finally { second.disable(); }
});
