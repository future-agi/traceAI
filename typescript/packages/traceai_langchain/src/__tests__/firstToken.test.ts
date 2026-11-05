import { BasicTracerProvider, InMemorySpanExporter, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { FITracer } from "@traceai/fi-core";
import { LangChainTracer } from "../tracer";
import { wrapModelStream } from "../streamLifecycle";

const key = "gen_ai.response.time_to_first_token";
const runId = "00000000-0000-4000-8000-000000000009";
const serialized = { lc: 1, type: "not_implemented" as const, id: ["synthetic"] };

describe("first-token timing", () => {
  afterEach(() => jest.useRealTimers());

  it.each(["llm", "chain"])("does not treat a %s end callback after return as successful output", async (kind) => {
    const exporter = new InMemorySpanExporter();
    const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
    const tracer = new LangChainTracer(new FITracer({ tracer: provider.getTracer("test") }), true);
    try {
      const stream = wrapModelStream(async function* () {
        if (kind === "llm") await tracer.handleLLMStart(serialized, ["query"], runId);
        else await tracer.handleChainStart(serialized, { input: "query" }, runId);
        try { yield 1; }
        finally {
          if (kind === "llm") await tracer.handleLLMEnd({ generations: [[{ text: "partial-answer" }]],
            llmOutput: { tokenUsage: { promptTokens: 5, completionTokens: 2, totalTokens: 7 } },
          }, runId);
          else await tracer.handleChainEnd({ output: "partial-answer" }, runId);
        }
      }, runId);
      await stream.next();
      await stream.return(undefined);
      expect(exporter.getFinishedSpans()).toHaveLength(1);
      const [span] = exporter.getFinishedSpans();
      expect(span.status.code).toBe(2);
      expect(span.attributes["langchain.stream.cancelled"]).toBe(true);
      expect(span.attributes["gen_ai.usage.total_tokens"]).toBeUndefined();
      expect(JSON.stringify(span.attributes)).not.toContain("partial-answer");
    } finally { await provider.shutdown(); }
  });

  it.each(["success", "error", "cancellation"])("records once and clears timing state on %s", async (action) => {
    jest.useFakeTimers();
    const exporter = new InMemorySpanExporter();
    const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
    const tracer = new LangChainTracer(new FITracer({ tracer: provider.getTracer("test"), traceConfig: {
      hideInputs: true, hideOutputs: true, hideErrorDetails: true,
    } }), true);
    try {
      const stream = wrapModelStream(async function* () {
        await tracer.handleLLMStart(serialized, ["synthetic-private-input"], runId);
        jest.advanceTimersByTime(125);
        await tracer.handleLLMNewToken("synthetic-private-token", { prompt: 0, completion: 0 }, runId);
        yield 1;
        jest.advanceTimersByTime(375);
        await tracer.handleLLMNewToken("synthetic-private-token", { prompt: 0, completion: 0 }, runId);
        if (action === "error") await tracer.handleLLMError(new Error("synthetic-private-error"), runId);
        else await tracer.handleLLMEnd({ generations: [[{ text: "synthetic-private-output" }]] }, runId);
      }, runId);
      await stream.next();
      if (action === "cancellation") await stream.return(undefined);
      else await stream.next();
      const [span] = exporter.getFinishedSpans();
      expect(span.attributes[key]).toBe(0.125);
      expect(JSON.stringify({ attrs: span.attributes, status: span.status, events: span.events })).not.toContain("synthetic-private");
      expect((tracer as unknown as { runs: object }).runs).toEqual({});
      // Reusing an upstream run ID must not inherit the previous run's timing.
      await tracer.handleLLMStart(serialized, ["again"], runId);
      await tracer.handleLLMEnd({ generations: [[{ text: "no streaming tokens" }]] }, runId);
      expect(exporter.getFinishedSpans()[1].attributes[key]).toBeUndefined();
    } finally { await provider.shutdown(); }
  });
});
