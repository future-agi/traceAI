import { context, ROOT_CONTEXT, SpanKind, SpanStatusCode } from "@opentelemetry/api";
import { suppressTracing } from "@opentelemetry/core";
import { NodeTracerProvider } from "@opentelemetry/sdk-trace-node";
import { InMemorySpanExporter, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { SemanticConventions as S } from "@traceai/fi-semantic-conventions";
import { GroqInstrumentation } from "../instrumentation";
import * as instrumentationModule from "../instrumentation";
import * as packageExports from "../index";
import * as responseObserver from "../response";

const INTEGRATION_CALL = Symbol.for("traceai.auto.integration-call.v1");
const params = { model: "requested-model", messages: [{ role: "user", content: "private-input" }] };
const options = { headers: { "x-test": "forwarded" } };
const completion = {
  id: "chat-test", object: "chat.completion", created: 1, model: "actual-model",
  choices: [{ index: 0, message: { role: "assistant", content: "answer" }, finish_reason: "stop" }],
  usage: { prompt_tokens: 8, completion_tokens: 5, total_tokens: 13 },
};
const chunks = [
  { id: completion.id, model: completion.model, choices: [
    { index: 0, delta: { role: "assistant", content: "answer" }, finish_reason: "stop" },
  ] },
  { id: completion.id, model: completion.model, choices: [], x_groq: { usage: completion.usage } },
];

class TestStream<T> implements AsyncIterable<T> {
  constructor(private readonly iterator: () => AsyncIterator<T>, readonly controller: AbortController) {}
  [Symbol.asyncIterator]() { return this.iterator(); }
}

interface ResponsePromise<T> extends Promise<T> {
  asResponse(): Promise<object>;
  withResponse(): Promise<{ data: T; response: object }>;
}

let provider: NodeTracerProvider;
let exporter: InMemorySpanExporter;
let instrumentation: GroqInstrumentation;
let observer: jest.SpyInstance;

beforeEach(() => {
  exporter = new InMemorySpanExporter();
  provider = new NodeTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
  instrumentation = new GroqInstrumentation({ instrumentationConfig: { enabled: false } });
  instrumentation.setTracerProvider(provider);
  instrumentation.enable();
  // Observe the real response handling without replacing it or registering globals.
  observer = jest.spyOn(responseObserver, "observeResponse");
});

afterEach(async () => {
  instrumentation.disable();
  jest.restoreAllMocks();
  await provider.shutdown();
});

function mockModule<T>(promise: ResponsePromise<T>) {
  const original = jest.fn(function (this: unknown, ..._args: unknown[]) { return promise; });
  const prototype = { create: original };
  const sdk = { Groq: { Chat: { Completions: { prototype } } } };
  instrumentation.manuallyInstrument(sdk);
  const receiver = Object.create(prototype) as { create: (...args: unknown[]) => ResponsePromise<T> };
  return { original, prototype, sdk, receiver };
}

function responsePromise<T>(data: T) {
  const raw = { requestId: "request-test" };
  const promise: ResponsePromise<T> = Object.assign(Promise.resolve(data), {
    asResponse: jest.fn(async () => raw),
    withResponse: jest.fn(async () => ({ data, response: raw })),
  });
  return { promise, raw };
}

function installHook() {
  const results: unknown[] = [];
  const hook = jest.fn((fn: () => unknown) => {
    const result = fn();
    results.push(result);
    return result;
  });
  Object.defineProperty(instrumentation, INTEGRATION_CALL, { value: hook });
  return { hook, results };
}

function expectNoHook(target: object) {
  expect(INTEGRATION_CALL in target).toBe(false);
  expect(Object.getOwnPropertyDescriptor(target, INTEGRATION_CALL)).toBeUndefined();
}

async function tracedCall(streaming: boolean) {
  let reads = 0;
  const source = new TestStream(async function* () {
    for (const chunk of chunks) { reads++; yield chunk; }
  }, new AbortController());
  const data = streaming ? source : completion;
  const { promise, raw } = responsePromise(data);
  const { original, receiver } = mockModule(promise);
  const body = streaming ? { ...params, stream: true } : params;
  const observationsBefore = observer.mock.calls.length;
  const spansBefore = exporter.getFinishedSpans().length;

  const returned = receiver.create(body, options);
  expect(original).toHaveBeenCalledTimes(1);
  expect(original.mock.contexts[0]).toBe(receiver);
  expect(original.mock.calls[0]).toEqual([body, options]);
  expect(original.mock.calls[0][0]).toBe(body);
  expect(original.mock.calls[0][1]).toBe(options);
  expect(observer).toHaveBeenCalledTimes(observationsBefore + 1);
  expect(observer.mock.calls[observationsBefore][0]).toBe(promise);
  // The existing response observer returns a proxy. The call must return that
  // exact object, not another promise introduced by the optional hook.
  expect(returned).toBe(observer.mock.results[observationsBefore].value);
  expect(returned).toBeInstanceOf(Promise);
  expect(typeof returned.asResponse).toBe("function");
  expect(typeof returned.withResponse).toBe("function");
  expect(reads).toBe(0);
  expect(exporter.getFinishedSpans()).toHaveLength(spansBefore);

  const withResponse = await returned.withResponse();
  expect(withResponse.response).toBe(raw);
  expect(await returned).toBe(withResponse.data);
  expect((await returned.withResponse()).data).toBe(withResponse.data);
  expect(reads).toBe(0);
  if (streaming) {
    const stream = withResponse.data as TestStream<typeof chunks[number]>;
    expect(stream).toBeInstanceOf(TestStream);
    expect(stream.controller).toBe(source.controller);
    expect(exporter.getFinishedSpans()).toHaveLength(spansBefore);
    const received = [];
    for await (const chunk of stream) received.push(chunk);
    expect(received).toEqual(chunks);
    received.forEach((chunk, index) => expect(chunk).toBe(chunks[index]));
    expect(reads).toBe(chunks.length);
    expect(await returned).toBe(stream);
  } else {
    expect(withResponse.data).toBe(completion);
  }
  expect(await returned.asResponse()).toBe(raw);
  expect(original).toHaveBeenCalledTimes(1);

  const spans = exporter.getFinishedSpans();
  expect(spans).toHaveLength(spansBefore + 1);
  const span = spans[spansBefore];
  expect(span.name).toBe("Groq Chat Completions");
  expect(span.kind).toBe(SpanKind.INTERNAL);
  expect(span.status).toEqual({ code: SpanStatusCode.OK });
  expect(span.events).toEqual([]);
  expect(span.attributes).toMatchObject({
    [S.FI_SPAN_KIND]: "LLM",
    [S.LLM_PROVIDER]: "groq",
    [S.GEN_AI_OPERATION_NAME]: "chat",
    [S.LLM_MODEL_NAME]: params.model,
    [S.INPUT_VALUE]: JSON.stringify(body),
    [S.RAW_INPUT]: JSON.stringify(body),
    [S.INPUT_MIME_TYPE]: "application/json",
    [S.LLM_INPUT_MESSAGES]: JSON.stringify(params.messages),
    [S.LLM_INVOCATION_PARAMETERS]: JSON.stringify({ model: params.model, ...(streaming ? { stream: true } : {}) }),
    [S.GEN_AI_RESPONSE_MODEL]: completion.model,
    [S.GEN_AI_RESPONSE_ID]: completion.id,
    [S.GEN_AI_RESPONSE_FINISH_REASONS]: JSON.stringify(["stop"]),
    [S.LLM_OUTPUT_MESSAGES]: JSON.stringify(completion.choices.map(choice => choice.message)),
    [S.LLM_TOKEN_COUNT_PROMPT]: 8,
    [S.LLM_TOKEN_COUNT_COMPLETION]: 5,
    [S.LLM_TOKEN_COUNT_TOTAL]: 13,
    [S.OUTPUT_VALUE]: streaming ? "answer" : JSON.stringify(completion),
    [S.OUTPUT_MIME_TYPE]: streaming ? "text/plain" : "application/json",
  });
  return {
    returned,
    span: { name: span.name, kind: span.kind, attributes: span.attributes, status: span.status, events: span.events },
  };
}

describe("optional integration call hook", () => {
  it.each([false, true])("preserves standalone results and span data with stream=%s", async streaming => {
    expectNoHook(instrumentation);
    await tracedCall(streaming);
    expectNoHook(instrumentation);
  });

  it.each([false, true])("preserves results and identical span data through an identity hook with stream=%s", async streaming => {
    expectNoHook(instrumentation);
    const standalone = await tracedCall(streaming);
    const { hook, results } = installHook();
    const first = await tracedCall(streaming);
    expect(hook).toHaveBeenCalledTimes(1);
    expect(results[0]).toBe(first.returned);
    expect(first.span).toEqual(standalone.span);
    const second = await tracedCall(streaming);
    expect(hook).toHaveBeenCalledTimes(2);
    expect(results[1]).toBe(second.returned);
    expect(second.span).toEqual(standalone.span);
  });

  it.each(["disabled", "suppressed"])("does not call the hook when tracing is %s", async state => {
    const { hook } = installHook();
    const { promise } = responsePromise(completion);
    const { original, receiver } = mockModule(promise);
    // Keep a captured wrapper so disable tests its bypass, not just unpatching.
    const wrapped = receiver.create;
    if (state === "disabled") instrumentation.disable();
    else jest.spyOn(context, "active").mockReturnValue(suppressTracing(ROOT_CONTEXT));

    const returned = wrapped.call(receiver, params, options);
    expect(returned).toBe(promise);
    expect(await returned).toBe(completion);
    expect(original).toHaveBeenCalledTimes(1);
    expect(original.mock.contexts[0]).toBe(receiver);
    expect(original.mock.calls[0]).toEqual([params, options]);
    expect(original.mock.calls[0][0]).toBe(params);
    expect(original.mock.calls[0][1]).toBe(options);
    expect(hook).not.toHaveBeenCalled();
    expect(observer).not.toHaveBeenCalled();
    expect(exporter.getFinishedSpans()).toHaveLength(0);
  });

  it("keeps the hook on one instance, not prototypes, modules or globals", async () => {
    const { hook } = installHook();
    const other = new GroqInstrumentation({ instrumentationConfig: { enabled: false } });
    const { promise } = responsePromise(completion);
    const { sdk, prototype, receiver } = mockModule(promise);
    try {
      expect(Object.getOwnPropertyDescriptor(instrumentation, INTEGRATION_CALL)).toEqual({
        value: hook, enumerable: false, configurable: false, writable: false,
      });
      const targets = [
        other, GroqInstrumentation, GroqInstrumentation.prototype, Object.getPrototypeOf(GroqInstrumentation.prototype),
        instrumentationModule, packageExports, sdk, sdk.Groq, prototype, globalThis,
      ];
      targets.forEach(expectNoHook);
      expect(await receiver.create(params, options)).toBe(completion);
      expect(hook).toHaveBeenCalledTimes(1);
      targets.forEach(expectNoHook);
    } finally {
      other.disable();
    }
  });
});
