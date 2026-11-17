import { PortkeyInstrumentation, isPatched, _resetPatchedStateForTesting } from '../instrumentation';
import * as instrumentationModule from '../instrumentation';
import * as publicModule from '../index';
import { SemanticConventions as S, FISpanKind, MimeType } from '@traceai/fi-semantic-conventions';
import { VERSION } from '../version';
import { InMemorySpanExporter, SimpleSpanProcessor } from '@opentelemetry/sdk-trace-base';
import { NodeTracerProvider } from '@opentelemetry/sdk-trace-node';
import { context, trace, SpanKind, SpanStatusCode } from '@opentelemetry/api';
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

describe('integration call hook', () => {
  const hookKey = Symbol.for('traceai.auto.integration-call.v1');
  const request = { model: 'synthetic-model', messages: [{ role: 'user', content: 'hello' }] };
  const reply = {
    id: 'chat-synthetic', model: 'synthetic-model',
    choices: [{ index: 0, message: { role: 'assistant', content: 'hello' }, finish_reason: 'stop' }],
    usage: { prompt_tokens: 2, completion_tokens: 1, total_tokens: 3 },
  };
  const config = { provider: 'synthetic-provider' };
  const options = { headers: { 'x-synthetic': 'private-header-value' } };
  const spanData = () => exporter.getFinishedSpans().map(span => ({
    name: span.name, kind: span.kind, attributes: { ...span.attributes }, status: { ...span.status },
  }));
  const installHook = () => {
    let calls = 0;
    let result: unknown;
    Object.defineProperty(inst, hookKey, {
      value: <T>(fn: () => T): T => { calls++; const value = fn(); result = value; return value; },
    });
    return { calls: () => calls, result: () => result };
  };
  const expectOriginalCall = (original: jest.Mock, receiver: unknown, args: unknown[]) => {
    expect(original).toHaveBeenCalledTimes(1);
    expect(original.mock.contexts[0]).toBe(receiver);
    expect(original.mock.calls[0]).toHaveLength(args.length);
    args.forEach((arg, index) => expect(original.mock.calls[0][index]).toBe(arg));
  };

  it.each([false, true])('preserves chat results and span data with an identity hook=%s', async withHook => {
    const sdkPromise = Promise.resolve(reply);
    const original = jest.fn(function () { return sdkPromise; });
    const resource = setup(original);
    const args = [request, config, options];
    expect(hookKey in inst).toBe(false);
    const baseline = resource.create(...args);
    expect(baseline).toBeInstanceOf(Promise);
    // Portkey already observes promises through a proxy, without copying parsed results.
    expect(baseline).not.toBe(sdkPromise);
    expect(await baseline).toBe(reply);
    expect(await baseline).toBe(reply);
    expectOriginalCall(original, resource, args);
    const before = spanData();
    expect(before).toHaveLength(1);
    expect(before[0]).toMatchObject({
      name: 'Portkey Chat Completions', kind: SpanKind.CLIENT, status: { code: SpanStatusCode.OK },
      attributes: {
        [S.FI_SPAN_KIND]: FISpanKind.LLM, [S.LLM_PROVIDER]: 'portkey',
        [S.LLM_MODEL_NAME]: reply.model, [S.GEN_AI_OPERATION_NAME]: 'chat',
        [S.INPUT_VALUE]: JSON.stringify(request), [S.INPUT_MIME_TYPE]: MimeType.JSON,
        [S.RAW_INPUT]: JSON.stringify(request), [S.LLM_INPUT_MESSAGES]: JSON.stringify(request.messages),
        [S.OUTPUT_VALUE]: 'hello', [S.LLM_OUTPUT_MESSAGES]: JSON.stringify([reply.choices[0].message]),
        [S.RAW_OUTPUT]: JSON.stringify(reply), [S.GEN_AI_RESPONSE_ID]: reply.id,
        [S.GEN_AI_RESPONSE_MODEL]: reply.model, [S.GEN_AI_RESPONSE_FINISH_REASONS]: JSON.stringify(['stop']),
        [S.LLM_TOKEN_COUNT_PROMPT]: 2, [S.LLM_TOKEN_COUNT_COMPLETION]: 1, [S.LLM_TOKEN_COUNT_TOTAL]: 3,
      },
    });
    expect(JSON.stringify(before)).not.toContain('private-header-value');

    exporter.reset();
    original.mockClear();
    const hook = withHook ? installHook() : undefined;
    const pending = resource.create(...args);
    if (hook) {
      expect(hook.calls()).toBe(1);
      expect(pending).toBe(hook.result());
    } else expect(hookKey in inst).toBe(false);
    expect(pending).toBeInstanceOf(Promise);
    expect(pending).not.toBe(sdkPromise);
    expect(await pending).toBe(reply);
    expect(await pending).toBe(reply);
    expectOriginalCall(original, resource, args);
    expect(spanData()).toEqual(before);
    if (hook) expect(hook.calls()).toBe(1);
  });

  it('preserves Responses promise helpers and span data with and without the hook', async () => {
    const params = { model: 'synthetic-model', input: 'hello' };
    const result = {
      id: 'resp-synthetic', model: 'synthetic-model', status: 'completed',
      output: [{ type: 'message', role: 'assistant', content: [{ type: 'output_text', text: 'hello' }] }],
      usage: { input_tokens: 2, output_tokens: 1, total_tokens: 3 },
    };
    const raw = new Response(JSON.stringify(result), { headers: { 'x-request-id': 'synthetic-request' } });
    const sdkPromise = Object.assign(Promise.resolve(result), {
      asResponse: async () => raw,
      withResponse: async () => ({ data: result, response: raw, request_id: 'synthetic-request' }),
    });
    const original = jest.fn(function () { return sdkPromise; });
    const resource = { create: original as Function };
    inst.manuallyInstrument({ chat: { completions: {} }, responses: resource });
    const args = [params, config, options];
    let before: ReturnType<typeof spanData> | undefined;
    for (const withHook of [false, true]) {
      exporter.reset();
      original.mockClear();
      expect(hookKey in inst).toBe(false);
      const hook = withHook ? installHook() : undefined;
      const pending = resource.create(...args);
      if (hook) {
        expect(hook.calls()).toBe(1);
        expect(pending).toBe(hook.result());
      }
      expect(pending).toBeInstanceOf(Promise);
      expect(pending).not.toBe(sdkPromise);
      expect(exporter.getFinishedSpans()).toHaveLength(0);
      const response = await pending.withResponse();
      expect(response.data).toBe(result);
      expect(response.response).toBe(raw);
      expect(response.request_id).toBe('synthetic-request');
      expect(await pending).toBe(result);
      expect(await pending.asResponse()).toBe(raw);
      expect(raw.bodyUsed).toBe(false);
      expectOriginalCall(original, resource, args);
      const spans = spanData();
      expect(spans).toHaveLength(1);
      expect(spans[0]).toMatchObject({
        name: 'Portkey Responses', kind: SpanKind.CLIENT, status: { code: SpanStatusCode.OK },
        attributes: {
          [S.FI_SPAN_KIND]: FISpanKind.LLM, [S.LLM_PROVIDER]: 'portkey',
          [S.GEN_AI_OPERATION_NAME]: 'generate_content', [S.LLM_MODEL_NAME]: result.model,
          [S.INPUT_VALUE]: JSON.stringify(params), [S.RAW_OUTPUT]: JSON.stringify(result),
          [S.OUTPUT_VALUE]: JSON.stringify([{ role: 'assistant', content: [{ type: 'text', text: 'hello' }] }]),
          [S.LLM_TOKEN_COUNT_TOTAL]: 3,
        },
      });
      if (withHook) expect(spans).toEqual(before);
      else before = spans;
      if (hook) expect(hook.calls()).toBe(1);
    }
  });

  it('preserves stream objects, chunks and span data with and without the hook', async () => {
    const chunk = { choices: [{ index: 0, delta: { content: 'hello' }, finish_reason: 'stop' }] };
    class Stream {
      marker() { return 'same-stream-api'; }
      async *[Symbol.asyncIterator]() { yield chunk; }
    }
    const sdkStream = new Stream();
    const sdkPromise = Promise.resolve(sdkStream);
    const original = jest.fn(function () { return sdkPromise; });
    const resource = setup(original);
    const params = { ...request, stream: true };
    const args = [params, config, options];
    let before: ReturnType<typeof spanData> | undefined;
    for (const withHook of [false, true]) {
      exporter.reset();
      original.mockClear();
      expect(hookKey in inst).toBe(false);
      const hook = withHook ? installHook() : undefined;
      const pending = resource.create(...args);
      if (hook) {
        expect(hook.calls()).toBe(1);
        expect(pending).toBe(hook.result());
      }
      const stream = await pending;
      expect(stream).toBeInstanceOf(Stream);
      // The existing stream proxy is cached by the observed promise.
      expect(stream).not.toBe(sdkStream);
      expect(await pending).toBe(stream);
      expect(stream.marker()).toBe('same-stream-api');
      expect(exporter.getFinishedSpans()).toHaveLength(0);
      const chunks = [];
      for await (const value of stream) chunks.push(value);
      expect(chunks).toHaveLength(1);
      expect(chunks[0]).toBe(chunk);
      expectOriginalCall(original, resource, args);
      const spans = spanData();
      expect(spans).toHaveLength(1);
      expect(spans[0]).toMatchObject({
        name: 'Portkey Chat Completions', kind: SpanKind.CLIENT,
        attributes: {
          [S.FI_SPAN_KIND]: FISpanKind.LLM, [S.LLM_PROVIDER]: 'portkey',
          [S.LLM_MODEL_NAME]: params.model, [S.INPUT_VALUE]: JSON.stringify(params),
          [S.OUTPUT_VALUE]: 'hello', [S.GEN_AI_RESPONSE_FINISH_REASONS]: JSON.stringify(['stop']),
        },
      });
      if (withHook) expect(spans).toEqual(before);
      else before = spans;
      if (hook) expect(hook.calls()).toBe(1);
    }
  });

  it.each(['disabled', 'suppressed'])('bypasses the hook for a %s call', async mode => {
    const sdkPromise = Promise.resolve(reply);
    const original = jest.fn(function () { return sdkPromise; });
    const resource = setup(original);
    const wrapped = resource.create;
    const hook = installHook();
    const args = [request, config, options];
    if (mode === 'disabled') inst.disable();
    // A saved wrapper must bypass the hook too, not just the restored method.
    const call = () => wrapped.apply(resource, args);
    const pending = mode === 'suppressed' ? context.with(suppressTracing(context.active()), call) : call();
    expect(pending).toBe(sdkPromise);
    expect(await pending).toBe(reply);
    expect(hook.calls()).toBe(0);
    expectOriginalCall(original, resource, args);
    expect(exporter.getFinishedSpans()).toHaveLength(0);
  });

  it('keeps the hook on its own instance, not prototypes, modules or globalThis', () => {
    const second = new PortkeyInstrumentation();
    const shared = [
      PortkeyInstrumentation, PortkeyInstrumentation.prototype,
      instrumentationModule, publicModule, globalThis, second,
    ];
    try {
      expect(hookKey in inst).toBe(false);
      for (const target of shared) expect(hookKey in target).toBe(false);
      const hook = installHook();
      expect(Object.hasOwn(inst, hookKey)).toBe(true);
      expect(Object.getOwnPropertyDescriptor(inst, hookKey)?.enumerable).toBe(false);
      for (const target of shared) expect(hookKey in target).toBe(false);
      expect(hook.calls()).toBe(0);
    } finally { second.disable(); }
  });
});
