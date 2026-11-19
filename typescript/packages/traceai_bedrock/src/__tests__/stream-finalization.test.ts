import { afterEach, describe, expect, it, jest } from "@jest/globals";
import { Span, SpanStatusCode } from "@opentelemetry/api";
import { BasicTracerProvider, InMemorySpanExporter, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { LLMSystem } from "@traceai/fi-semantic-conventions";

const OriginalRegistry = globalThis.FinalizationRegistry;
afterEach(() => { globalThis.FinalizationRegistry = OriginalRegistry; });

function setup() {
  let cleanup!: (held: unknown) => void;
  const registrations: { target: object; held: unknown; token?: object }[] = [];
  const unregister = jest.fn((_token: object) => true);
  class TestRegistry {
    constructor(callback: (held: unknown) => void) { cleanup = callback; }
    register(target: object, held: unknown, token?: object) { registrations.push({ target, held, token }); }
    unregister = unregister;
  }
  globalThis.FinalizationRegistry = TestRegistry as unknown as typeof FinalizationRegistry;
  let observe!: typeof import("../attributes/invoke-model-streaming-response-attributes").observeBedrockStream;
  jest.isolateModules(() => {
    observe = require("../attributes/invoke-model-streaming-response-attributes").observeBedrockStream;
  });
  const exporter = new InMemorySpanExporter();
  const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
  const span: Span = provider.getTracer("offline").startSpan("stream");
  const next = jest.fn(async () => ({ done: true as const, value: undefined }));
  const source = { next, [Symbol.asyncIterator]() { return this; } };
  const body = observe({ stream: source, span, modelType: LLMSystem.ANTHROPIC,
    onEnd: () => { span.setStatus({ code: SpanStatusCode.OK }); span.end(); },
    onError: () => { span.setStatus({ code: SpanStatusCode.ERROR }); span.end(); },
  });
  return { body, exporter, next, registrations, unregister, cleanup: (held: unknown) => cleanup(held) };
}

describe("Bedrock unconsumed stream finalization", () => {
  it("registers best-effort cleanup without pulling or creating timers", () => {
    const schedule = jest.spyOn(globalThis, "setTimeout");
    try {
      const run = setup();
      expect(run.next).not.toHaveBeenCalled();
      expect(schedule).not.toHaveBeenCalled();
      expect(run.exporter.getFinishedSpans()).toHaveLength(0);
      expect(run.registrations).toHaveLength(1);
      // Deterministic delivery of the GC notification, not a timing assertion.
      run.cleanup(run.registrations[0].held);
      const spans = run.exporter.getFinishedSpans();
      expect(spans).toHaveLength(1);
      expect(spans[0].status.code).toBe(SpanStatusCode.UNSET);
      expect(spans[0].attributes["fi.stream.consumed"]).toBe(false);
      expect(run.next).not.toHaveBeenCalled();
      run.cleanup(run.registrations[0].held);
      expect(run.exporter.getFinishedSpans()).toHaveLength(1);
    } finally { schedule.mockRestore(); }
  });

  it("disarms unconsumed cleanup when the caller pulls", async () => {
    const run = setup();
    expect(run.registrations).toHaveLength(1);
    await run.body[Symbol.asyncIterator]().next();
    expect(run.unregister).toHaveBeenCalled();
    // A queued notification must not turn a consumed stream into UNSET/unconsumed.
    run.cleanup(run.registrations[0].held);
    const spans = run.exporter.getFinishedSpans();
    expect(spans).toHaveLength(1);
    expect(spans[0].status.code).toBe(SpanStatusCode.OK);
    expect(spans[0].attributes["fi.stream.consumed"]).not.toBe(false);
  });
});
