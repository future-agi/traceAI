import { context, trace, SpanStatusCode } from '@opentelemetry/api';
import { BasicTracerProvider, InMemorySpanExporter, ReadableSpan, SimpleSpanProcessor } from '@opentelemetry/sdk-trace-base';
import { createEveDestination } from '../eve';
import { FISimpleSpanProcessor } from '../FISpanProcessor';

function setup() {
  const exporter = new InMemorySpanExporter();
  const other = new InMemorySpanExporter();
  const next = new SimpleSpanProcessor(exporter);
  const options = createEveDestination({ processor: next });
  const provider = new BasicTracerProvider();
  provider.addSpanProcessor(options.spanProcessors[0]);
  provider.addSpanProcessor(new SimpleSpanProcessor(other));
  return { exporter, other, next, options, provider, tracer: provider.getTracer('eve.agent') };
}

it('qualifies native Eve agents and preserves totals outside billable usage', async () => {
  const { exporter, other, provider, tracer } = setup();
  const span = tracer.startSpan('invoke_agent weather', { attributes: {
    'gen_ai.operation.name': 'invoke_agent', 'gen_ai.conversation.id': 'conversation-1',
    'gen_ai.usage.input_tokens': 12, 'gen_ai.usage.output_tokens': 4,
  } });
  span.end();
  await provider.forceFlush();
  const attributes = exporter.getFinishedSpans()[0].attributes;
  expect(attributes['fi.span.kind']).toBe('AGENT');
  expect(attributes['session.id']).toBe('conversation-1');
  expect(attributes['gen_ai.usage.input_tokens']).toBeUndefined();
  expect(attributes['eve.aggregate.usage.input_tokens']).toBe(12);
  expect(other.getFinishedSpans()[0].attributes).toEqual({
    'gen_ai.operation.name': 'invoke_agent', 'gen_ai.conversation.id': 'conversation-1',
    'gen_ai.usage.input_tokens': 12, 'gen_ai.usage.output_tokens': 4,
  });
  await provider.shutdown();
});

it.each([['workflow', 'CHAIN'], ['chat', 'LLM'], ['execute_tool', 'TOOL']])(
  'maps %s without creating another span', async (operation, kind) => {
    const { exporter, provider, tracer } = setup();
    tracer.startSpan(operation, { attributes: { 'gen_ai.operation.name': operation } }).end();
    await provider.forceFlush();
    expect(exporter.getFinishedSpans()).toHaveLength(1);
    expect(exporter.getFinishedSpans()[0].attributes['fi.span.kind']).toBe(kind);
    await provider.shutdown();
  },
);

it('maps retained messages and tools without changing native content', async () => {
  const { exporter, provider, tracer } = setup();
  const messages = '[{"role":"user","parts":[{"type":"text","content":"weather"}]}]';
  tracer.startSpan('chat model', { attributes: {
    'gen_ai.operation.name': 'chat', 'gen_ai.input.messages': messages,
    'gen_ai.output.messages': '[]', 'gen_ai.usage.input_tokens': 12,
  } }).end();
  tracer.startSpan('execute_tool weather', { attributes: {
    'gen_ai.operation.name': 'execute_tool', 'gen_ai.tool.name': 'weather',
    'gen_ai.tool.call.id': 'call-1', 'gen_ai.tool.call.arguments': '{"city":"Paris"}',
    'gen_ai.tool.call.result': 'clear',
  } }).end();
  await provider.forceFlush();
  const [model, tool] = exporter.getFinishedSpans().map(span => span.attributes);
  expect(model['input.value']).toBe(messages);
  expect(model['input.mime_type']).toBe('application/json');
  expect(model['gen_ai.input.messages']).toBe(messages);
  expect(model['gen_ai.usage.input_tokens']).toBe(12);
  expect(tool['tool.name']).toBe('weather');
  expect(tool['tool_call.id']).toBe('call-1');
  expect(tool['input.value']).toBe('{"city":"Paris"}');
  expect(tool['output.mime_type']).toBe('text/plain');
  await provider.shutdown();
});

it('does not reconstruct removed content or invent missing sessions', async () => {
  const { exporter, provider, tracer } = setup();
  const span = tracer.startSpan('chat model', { attributes: { 'gen_ai.operation.name': 'chat' } });
  span.setStatus({ code: SpanStatusCode.ERROR });
  span.end();
  await provider.forceFlush();
  const saved = exporter.getFinishedSpans()[0];
  expect(saved.attributes['input.value']).toBeUndefined();
  expect(saved.attributes['output.value']).toBeUndefined();
  expect(saved.attributes['session.id']).toBeUndefined();
  expect(saved.status.code).toBe(SpanStatusCode.ERROR);
  await provider.shutdown();
});

it('leaves custom scopes, explicit session IDs and explicit span kinds alone', async () => {
  const { exporter, provider, tracer } = setup();
  provider.getTracer('application').startSpan('custom', { attributes: {
    'gen_ai.operation.name': 'invoke_agent', 'gen_ai.usage.input_tokens': 3,
  } }).end();
  tracer.startSpan('customized', { attributes: {
    'gen_ai.operation.name': 'invoke_agent', 'session.id': 'explicit',
    'gen_ai.conversation.id': 'native', 'fi.span.kind': 'CHAIN',
  } }).end();
  await provider.forceFlush();
  const [custom, explicit] = exporter.getFinishedSpans();
  expect(custom.attributes['fi.span.kind']).toBeUndefined();
  expect(custom.attributes['gen_ai.usage.input_tokens']).toBe(3);
  expect(explicit.attributes['session.id']).toBe('explicit');
  expect(explicit.attributes['fi.span.kind']).toBe('CHAIN');
  await provider.shutdown();
});

it('filters infrastructure and reparents children through nested dropped scopes', async () => {
  const { exporter, other, provider, tracer } = setup();
  const root = tracer.startSpan('invoke_agent weather');
  const infrastructure = provider.getTracer('workflow').startSpan('plumbing', {}, trace.setSpan(context.active(), root));
  const fetch = provider.getTracer('@vercel/otel/fetch').startSpan('fetch', {}, trace.setSpan(context.active(), infrastructure));
  const child = tracer.startSpan('chat model', {}, trace.setSpan(context.active(), fetch));
  infrastructure.end();
  fetch.end();
  child.end();
  root.end();
  await provider.forceFlush();
  expect(exporter.getFinishedSpans().map(span => span.name)).toEqual(['chat model', 'invoke_agent weather']);
  expect(exporter.getFinishedSpans()[0].parentSpanId).toBe(root.spanContext().spanId);
  expect(other.getFinishedSpans().find(span => span.name === 'chat model')?.parentSpanId).toBe(fetch.spanContext().spanId);
  await provider.shutdown();
});

it('promotes a child of a dropped root without changing its trace ID', async () => {
  const { exporter, provider, tracer } = setup();
  const root = provider.getTracer('workflow').startSpan('plumbing');
  root.end();
  const child = tracer.startSpan('chat model', {}, trace.setSpan(context.active(), root));
  child.end();
  await provider.forceFlush();
  expect(exporter.getFinishedSpans()[0].parentSpanId).toBeUndefined();
  expect(exporter.getFinishedSpans()[0].spanContext().traceId).toBe(root.spanContext().traceId);
  await provider.shutdown();
});

it('pairs metadata-only native policy with the processor and delegates lifecycle', async () => {
  const processor = { onEnd: jest.fn<void, [ReadableSpan]>(), forceFlush: jest.fn(async () => {}), shutdown: jest.fn(async () => {}) };
  const options = createEveDestination({ processor });
  expect(options.exportPolicy[0].span()).toEqual({ redact: true, inputs: true, outputs: true });
  expect(options.exportPolicy[0].attribute({ key: 'agent.principal.current.id' })).toEqual({ emit: false });
  expect(options.exportPolicy[0].attribute({ key: 'agent.principal.initiator.id' })).toEqual({ emit: false });
  expect(options.exportPolicy[0].attribute({ key: 'gen_ai.conversation.id' })).toEqual({ emit: true });
  expect(createEveDestination({ processor, captureContent: true }).exportPolicy).toEqual([]);
  await options.spanProcessors[0].forceFlush();
  await options.spanProcessors[0].shutdown();
  expect(processor.forceFlush).toHaveBeenCalledTimes(1);
  expect(processor.shutdown).toHaveBeenCalledTimes(1);
});

it('keeps active child ancestry after the bounded cache rolls over', async () => {
  const { exporter, provider, tracer } = setup();
  const root = tracer.startSpan('root');
  const plumbing = provider.getTracer('workflow');
  const parent = plumbing.startSpan('parent', {}, trace.setSpan(context.active(), root));
  const child = tracer.startSpan('child', {}, trace.setSpan(context.active(), parent));
  parent.end();
  for (let index = 0; index < 4200; index++) plumbing.startSpan('noise').end();
  child.end();
  root.end();
  await provider.forceFlush();
  expect(exporter.getFinishedSpans()).toHaveLength(2);
  expect(exporter.getFinishedSpans()[0].parentSpanId).toBe(root.spanContext().spanId);
  await provider.shutdown();
});

it('propagates explicit flush and shutdown failures to Eve', async () => {
  const processor = {
    onEnd: (_span: ReadableSpan) => {},
    forceFlush: async () => { throw new Error('flush failed'); },
    shutdown: async () => { throw new Error('shutdown failed'); },
  };
  const destination = createEveDestination({ processor }).spanProcessors[0];
  await expect(destination.forceFlush()).rejects.toThrow('flush failed');
  await expect(destination.shutdown()).rejects.toThrow('shutdown failed');
});

it('retains the legacy AI SDK processor entry point', async () => {
  const exporter = new InMemorySpanExporter();
  const provider = new BasicTracerProvider();
  provider.addSpanProcessor(new FISimpleSpanProcessor({ exporter }));
  provider.getTracer('ai').startSpan('ai.generateText.doGenerate', { attributes: {
    'operation.name': 'ai.generateText.doGenerate', 'ai.model.id': 'synthetic-model',
  } }).end();
  await provider.forceFlush();
  expect(exporter.getFinishedSpans()[0].attributes['fi.span.kind']).toBe('LLM');
  await provider.shutdown();
});
