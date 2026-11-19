import { afterEach, beforeEach, describe, expect, it, jest } from "@jest/globals";
import { context, ROOT_CONTEXT, SpanKind, SpanStatusCode } from "@opentelemetry/api";
import { suppressTracing } from "@opentelemetry/core";
import { BasicTracerProvider, InMemorySpanExporter, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { FISpanKind, LLMProvider, SemanticConventions as S } from "@traceai/fi-semantic-conventions";
import * as mistralModule from "../index";
import * as instrumentationModule from "../instrumentation";
import { MistralInstrumentation } from "../instrumentation";

const integrationCall = Symbol.for("traceai.auto.integration-call.v1");
const request = { model: "mistral-test", messages: [{ role: "user", content: "synthetic input" }] };
const options = { serverURL: "http://localhost:6789" };
const completion = {
  id: "completion-test", object: "chat.completion", created: 1, model: "mistral-test",
  choices: [{ index: 0, message: { role: "assistant", content: "synthetic output" }, finish_reason: "stop" }],
  usage: { prompt_tokens: 3, completion_tokens: 5, total_tokens: 8 },
};

function installIdentityHook(instrumentation: MistralInstrumentation) {
  let calls = 0;
  const results: unknown[] = [];
  Object.defineProperty(instrumentation, integrationCall, {
    value: <T>(fn: () => T): T => {
      calls++;
      const result = fn();
      results.push(result);
      return result;
    },
  });
  return { get calls() { return calls; }, results };
}

function expectNoSharedHook() {
  for (const target of [
    MistralInstrumentation.prototype, MistralInstrumentation,
    instrumentationModule, mistralModule, globalThis,
  ]) {
    expect(Object.getOwnPropertyDescriptor(target, integrationCall)).toBeUndefined();
    expect(Reflect.has(target, integrationCall)).toBe(false);
  }
}

describe("Mistral integration call hook", () => {
  let instrumentation: MistralInstrumentation;
  let exporter: InMemorySpanExporter;
  let provider: BasicTracerProvider;

  beforeEach(() => {
    exporter = new InMemorySpanExporter();
    provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
    instrumentation = new MistralInstrumentation();
    instrumentation.setTracerProvider(provider);
    expect(Reflect.has(instrumentation, integrationCall)).toBe(false);
    expectNoSharedHook();
  });

  afterEach(async () => {
    jest.restoreAllMocks();
    instrumentation.disable();
    await provider.shutdown();
  });

  function spanData() {
    const spans = exporter.getFinishedSpans();
    expect(spans).toHaveLength(1);
    const [span] = spans;
    return { name: span.name, kind: span.kind, attributes: span.attributes, status: span.status };
  }

  it.each(["synchronous", "promise"])("preserves %s completion results and spans with and without an instance hook", async mode => {
    const promise = Promise.resolve(completion);
    const original = jest.fn((_input: typeof request, _options: typeof options) =>
      mode === "synchronous" ? completion : promise);
    const client = { chat: { complete: original } };
    expect(instrumentation.instrumentClient(client)).toBe(client);

    async function call() {
      const returned = client.chat.complete(request, options);
      expect(original).toHaveBeenCalledTimes(1);
      expect(original.mock.contexts[0]).toBe(client.chat);
      expect(original.mock.calls[0]).toHaveLength(2);
      expect(original.mock.calls[0][0]).toBe(request);
      expect(original.mock.calls[0][1]).toBe(options);
      if (mode === "synchronous") expect(returned).toBe(completion);
      else {
        // Mistral already observes promises with a chained promise, without a hook.
        expect(returned).toBeInstanceOf(Promise);
        expect(returned).not.toBe(promise);
      }
      expect(await returned).toBe(completion);
      const data = spanData();
      expect(data).toMatchObject({
        name: "Mistral chat.complete",
        kind: SpanKind.INTERNAL,
        status: { code: SpanStatusCode.OK },
        attributes: {
          [S.FI_SPAN_KIND]: FISpanKind.LLM,
          [S.LLM_PROVIDER]: LLMProvider.MISTRALAI,
          [S.LLM_MODEL_NAME]: request.model,
          [S.INPUT_VALUE]: JSON.stringify(request),
          [S.LLM_INPUT_MESSAGES]: JSON.stringify(request.messages),
          [S.OUTPUT_VALUE]: JSON.stringify(completion),
          [S.LLM_OUTPUT_MESSAGES]: JSON.stringify([completion.choices[0].message]),
          [S.GEN_AI_RESPONSE_FINISH_REASONS]: JSON.stringify(["stop"]),
          [S.LLM_TOKEN_COUNT_PROMPT]: 3,
          [S.LLM_TOKEN_COUNT_COMPLETION]: 5,
          [S.LLM_TOKEN_COUNT_TOTAL]: 8,
        },
      });
      return { returned, data };
    }

    const standalone = await call();
    expect(Reflect.has(instrumentation, integrationCall)).toBe(false);
    exporter.reset();
    original.mockClear();
    const hook = installIdentityHook(instrumentation);
    const hooked = await call();
    expect(hook.calls).toBe(1);
    expect(hook.results).toHaveLength(1);
    expect(hook.results[0]).toBe(hooked.returned);
    expect(hooked.data).toEqual(standalone.data);
    expectNoSharedHook();
  });

  it("preserves lazy stream results and spans with and without an instance hook", async () => {
    const event = {
      id: "stream-test", object: "chat.completion.chunk", model: "mistral-test",
      choices: [{ index: 0, delta: { role: "assistant", content: "hello" }, finish_reason: "stop" }],
      usage: { prompt_tokens: 3, completion_tokens: 5, total_tokens: 8 },
    };
    const next = jest.fn<() => Promise<IteratorResult<typeof event>>>();
    const source = {
      [Symbol.asyncIterator]: jest.fn(() => ({ next })),
      getSource() { return this; },
    };
    const original = jest.fn((_input: typeof request, _options: typeof options) => source);
    const client = { chat: { stream: original } };
    instrumentation.instrumentClient(client);

    async function call() {
      next.mockReset()
        .mockResolvedValueOnce({ done: false, value: event })
        .mockResolvedValueOnce({ done: true, value: undefined });
      source[Symbol.asyncIterator].mockClear();
      const returned = client.chat.stream(request, options);
      // Mistral already returns an observing proxy rather than the source stream.
      expect(returned).not.toBe(source);
      expect(returned.getSource()).toBe(source);
      expect(original).toHaveBeenCalledTimes(1);
      expect(original.mock.contexts[0]).toBe(client.chat);
      expect(original.mock.calls[0]).toHaveLength(2);
      expect(original.mock.calls[0][0]).toBe(request);
      expect(original.mock.calls[0][1]).toBe(options);
      expect(source[Symbol.asyncIterator]).not.toHaveBeenCalled();
      expect(next).not.toHaveBeenCalled();
      expect(exporter.getFinishedSpans()).toHaveLength(0);
      const received: typeof event[] = [];
      for await (const value of returned) {
        expect(exporter.getFinishedSpans()).toHaveLength(0);
        received.push(value);
      }
      expect(received).toHaveLength(1);
      expect(received[0]).toBe(event);
      expect(source[Symbol.asyncIterator]).toHaveBeenCalledTimes(1);
      expect(source[Symbol.asyncIterator].mock.contexts[0]).toBe(source);
      expect(next).toHaveBeenCalledTimes(2);
      const data = spanData();
      expect(data).toMatchObject({
        name: "Mistral chat.stream",
        kind: SpanKind.INTERNAL,
        status: { code: SpanStatusCode.OK },
        attributes: {
          [S.FI_SPAN_KIND]: FISpanKind.LLM,
          [S.LLM_PROVIDER]: LLMProvider.MISTRALAI,
          [S.LLM_MODEL_NAME]: request.model,
          [S.INPUT_VALUE]: JSON.stringify(request),
          [S.LLM_INPUT_MESSAGES]: JSON.stringify(request.messages),
          [S.OUTPUT_VALUE]: JSON.stringify([event]),
          [S.GEN_AI_RESPONSE_FINISH_REASONS]: JSON.stringify(["stop"]),
          [S.LLM_TOKEN_COUNT_PROMPT]: 3,
          [S.LLM_TOKEN_COUNT_COMPLETION]: 5,
          [S.LLM_TOKEN_COUNT_TOTAL]: 8,
        },
      });
      expect(JSON.parse(data.attributes[S.LLM_OUTPUT_MESSAGES] as string)[0].content).toBe("hello");
      return { returned, data };
    }

    const standalone = await call();
    expect(Reflect.has(instrumentation, integrationCall)).toBe(false);
    exporter.reset();
    original.mockClear();
    const hook = installIdentityHook(instrumentation);
    const hooked = await call();
    expect(hook.calls).toBe(1);
    expect(hook.results).toHaveLength(1);
    expect(hook.results[0]).toBe(hooked.returned);
    expect(hooked.data).toEqual(standalone.data);
    expectNoSharedHook();
  });

  it.each(["disabled", "suppressed"])("does not call the hook for a %s call", async mode => {
    const hook = installIdentityHook(instrumentation);
    const promise = Promise.resolve(completion);
    const original = jest.fn((_input: typeof request, _options: typeof options) => promise);
    const client = { chat: { complete: original } };
    instrumentation.instrumentClient(client);
    const wrapped = client.chat.complete;
    expect(wrapped).not.toBe(original);
    if (mode === "disabled") {
      instrumentation.disable();
      expect(client.chat.complete).toBe(original);
    } else {
      jest.spyOn(context, "active").mockReturnValue(suppressTracing(ROOT_CONTEXT));
    }
    // Exercise the guard even if the application saved the wrapper before disable.
    const returned = wrapped.call(client.chat, request, options);
    expect(returned).toBe(promise);
    expect(await returned).toBe(completion);
    expect(original).toHaveBeenCalledTimes(1);
    expect(original.mock.contexts[0]).toBe(client.chat);
    expect(original.mock.calls[0]).toHaveLength(2);
    expect(original.mock.calls[0][0]).toBe(request);
    expect(original.mock.calls[0][1]).toBe(options);
    expect(hook.calls).toBe(0);
    expect(hook.results).toHaveLength(0);
    expect(exporter.getFinishedSpans()).toHaveLength(0);
  });

  it("keeps the hook on its own instance, not prototypes, module exports or globals", () => {
    const hook = installIdentityHook(instrumentation);
    expect(Object.getOwnPropertyDescriptor(instrumentation, integrationCall)).toEqual({
      value: expect.any(Function), enumerable: false, writable: false, configurable: false,
    });
    const other = new MistralInstrumentation();
    try {
      other.setTracerProvider(provider);
      expect(Reflect.has(other, integrationCall)).toBe(false);
      const original = jest.fn((_input: typeof request, _options: typeof options) => completion);
      const client = { chat: { complete: original } };
      other.instrumentClient(client);
      expect(client.chat.complete(request, options)).toBe(completion);
      expect(original).toHaveBeenCalledTimes(1);
      expect(spanData().name).toBe("Mistral chat.complete");
      expect(hook.calls).toBe(0);
      expectNoSharedHook();
    } finally {
      other.disable();
    }
  });
});
