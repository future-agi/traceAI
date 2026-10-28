import { describe, it, expect, beforeEach } from "@jest/globals";
import { SpanStatusCode } from "@opentelemetry/api";
import {
  BasicTracerProvider,
  InMemorySpanExporter,
  ReadableSpan,
  SimpleSpanProcessor,
} from "@opentelemetry/sdk-trace-base";
import { BaseTracer } from "@langchain/core/tracers/base";
import { awaitAllCallbacks } from "@langchain/core/callbacks/promises";
import { SimpleChatModel } from "@langchain/core/language_models/chat_models";
import { RunnableLambda } from "@langchain/core/runnables";
import { FITracer, TraceConfigOptions } from "@traceai/fi-core";
import { LangChainTracer } from "../tracer";

const PROMPT = "my secret prompt: the launch code is 0000";

/** Mimics an OpenAI SDK error: a subclass whose `name` stays "Error", with a provider `code` */
class NotFoundError extends Error {
  code = "model_not_found";
}

class FailingChatModel extends SimpleChatModel {
  _llmType() {
    return "failing";
  }
  async _call(): Promise<string> {
    throw new NotFoundError(`404 model not found for input "${PROMPT}"`);
  }
}

describe("LangChainTracer error recording", () => {
  const exporter = new InMemorySpanExporter();
  const provider = new BasicTracerProvider({
    spanProcessors: [new SimpleSpanProcessor(exporter)],
  });
  beforeEach(() => {
    exporter.reset();
  });

  function langChainTracer(traceConfig: TraceConfigOptions = {}) {
    return new LangChainTracer(
      new FITracer({ tracer: provider.getTracer("test"), traceConfig }),
    );
  }

  async function failingLLMSpan(traceConfig: TraceConfigOptions = {}) {
    const tracer = langChainTracer(traceConfig);
    await expect(
      new FailingChatModel({}).invoke(PROMPT, { callbacks: [tracer] }),
    ).rejects.toThrow("404 model not found");
    await awaitAllCallbacks();
    const spans = exporter.getFinishedSpans();
    expect(spans).toHaveLength(1);
    return spans[0];
  }

  function exceptionEvent(span: ReadableSpan) {
    const events = span.events.filter((event) => event.name === "exception");
    expect(events).toHaveLength(1);
    return events[0];
  }

  it("uses the error message, not run.error's traceback, as the status description", async () => {
    const span = await failingLLMSpan();
    expect(span.status.code).toBe(SpanStatusCode.ERROR);
    expect(span.status.message).toBe(`404 model not found for input "${PROMPT}"`);
    expect(span.status.message).not.toContain(" at ");
    expect(span.status.message).not.toContain(__filename);
    expect(span.attributes["error.type"]).toBe("model_not_found");

    // The stack trace stays only on the exception event
    const event = exceptionEvent(span);
    expect(event.attributes?.["exception.type"]).toBe("model_not_found");
    expect(event.attributes?.["exception.message"]).toBe(span.status.message);
    expect(event.attributes?.["exception.stacktrace"]).toContain(" at ");
  });

  it("hides the description, message and stack trace when inputs are hidden", async () => {
    const span = await failingLLMSpan({ hideInputs: true });
    expect(span.status).toEqual({ code: SpanStatusCode.ERROR });
    expect(span.attributes["error.type"]).toBe("model_not_found");
    expect(exceptionEvent(span).attributes).toEqual({
      "exception.type": "model_not_found",
    });
    expect(
      JSON.stringify({ status: span.status, attributes: span.attributes, events: span.events }),
    ).not.toContain(PROMPT);
  });

  it("an explicit hideErrorDetails: false keeps the message with inputs hidden", async () => {
    const span = await failingLLMSpan({ hideInputs: true, hideErrorDetails: false });
    expect(span.status.message).toBe(`404 model not found for input "${PROMPT}"`);
    expect(exceptionEvent(span).attributes?.["exception.stacktrace"]).toBeDefined();
  });

  it("records chain errors the same way", async () => {
    const tracer = langChainTracer();
    const chain = RunnableLambda.from(async (_input: string) => {
      throw new TypeError("chain step failed");
    }).withConfig({ runName: "failing_step" });
    await expect(chain.invoke("x", { callbacks: [tracer] })).rejects.toThrow(
      "chain step failed",
    );
    await awaitAllCallbacks();
    const span = exporter.getFinishedSpans().find((s) => s.name === "failing_step");
    expect(span?.status).toEqual({
      code: SpanStatusCode.ERROR,
      message: "chain step failed",
    });
    expect(span?.attributes["error.type"]).toBe("TypeError");
  });

  it("falls back to splitting run.error when the error object was not seen", async () => {
    const tracer = langChainTracer();
    const runId = "00000000-0000-4000-8000-000000000001";
    await tracer.handleChainStart(
      { lc: 1, type: "not_implemented", id: ["failing"] },
      { input: "x" },
      runId,
    );
    const error = new RangeError("out of range");
    // Bypass the override, as an older BaseTracer path would: only run.error is available
    await BaseTracer.prototype.handleChainError.call(tracer, error, runId);
    const [span] = exporter.getFinishedSpans();
    expect(span.status).toEqual({ code: SpanStatusCode.ERROR, message: "out of range" });
    expect(span.attributes["error.type"]).toBe("RangeError");
    const event = exceptionEvent(span);
    expect(event.attributes?.["exception.message"]).toBe("out of range");
    expect(event.attributes?.["exception.stacktrace"]).toContain(" at ");
  });

  it("keeps blank lines that are part of the message in the fallback", async () => {
    const tracer = langChainTracer();
    const runId = "00000000-0000-4000-8000-000000000002";
    await tracer.handleChainStart(
      { lc: 1, type: "not_implemented", id: ["failing"] },
      { input: "x" },
      runId,
    );
    const error = new Error("first paragraph\n\nsecond paragraph");
    await BaseTracer.prototype.handleChainError.call(tracer, error, runId);
    const [span] = exporter.getFinishedSpans();
    expect(span.status).toEqual({
      code: SpanStatusCode.ERROR,
      message: "first paragraph\n\nsecond paragraph",
    });
    expect(exceptionEvent(span).attributes?.["exception.stacktrace"]).toContain(" at ");
  });

    it("forgets the stashed error after the run ends", async () => {
    const tracer = langChainTracer();
    await failingLLMSpan();
    await expect(
      new FailingChatModel({}).invoke("again", { callbacks: [tracer] }),
    ).rejects.toThrow();
    await awaitAllCallbacks();
    expect((tracer as unknown as { runErrors: object }).runErrors).toEqual({});
  });
});
