import { context, ROOT_CONTEXT, trace } from "@opentelemetry/api";
import { AsyncLocalStorageContextManager } from "@opentelemetry/context-async-hooks";
import { BasicTracerProvider, InMemorySpanExporter, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { FITracer } from "../trace/trace-config/FITracer";
import type { FISpan } from "../trace/trace-config/FISpan";

// The application registered an OpenTelemetry context manager. These tests pin
// the behaviour that already holds with one, so it stays unchanged.
const contextManager = new AsyncLocalStorageContextManager();
beforeAll(() => {
  context.setGlobalContextManager(contextManager.enable());
});
afterAll(() => {
  context.disable();
  contextManager.disable();
});
const tick = () => new Promise<void>((resolve) => setTimeout(resolve, 1));

describe.each(["standalone", "auto"] as const)("startActiveSpan with a context manager (%s)", (mode) => {
  function fixture() {
    const exporter = new InMemorySpanExporter();
    const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
    const tracer = new FITracer({
      tracer: (mode === "auto" ? FITracer.autoProvider(provider) : provider).getTracer("active-span"),
    });
    const span = (name: string) => {
      const found = exporter.getFinishedSpans().find((s) => s.name === name);
      if (!found) throw new Error(`no finished span named ${name}`);
      return found;
    };
    const parentOf = (name: string) => span(name).parentSpanContext?.spanId;
    const idOf = (name: string) => span(name).spanContext().spanId;
    return { tracer, parentOf, idOf };
  }

  it("nests synchronously and after an await, and the span is active through the API", async () => {
    const { tracer, parentOf, idOf } = fixture();
    await tracer.startActiveSpan("outer", async (outer) => {
      expect(trace.getActiveSpan()).toBe(outer.getContextSpan());
      tracer.startActiveSpan("inner-sync", (inner) => inner.end());
      await tick();
      await tracer.startActiveSpan("inner-async", async (inner) => {
        await Promise.resolve();
        tracer.startSpan("grandchild").end();
        inner.end();
      });
      outer.end();
    });
    expect(parentOf("inner-sync")).toBe(idOf("outer"));
    expect(parentOf("inner-async")).toBe(idOf("outer"));
    expect(parentOf("grandchild")).toBe(idOf("inner-async"));
  });

  it("does not leak to the caller after the callback or to a sibling started in the same tick", async () => {
    const { tracer, parentOf, idOf } = fixture();
    const sibling = async () => {
      tracer.startSpan("sibling-sync").end();
      await Promise.resolve();
      tracer.startSpan("sibling-after-await").end();
    };
    await Promise.all([
      tracer.startActiveSpan("outer", async (outer) => {
        await tick();
        tracer.startSpan("inside").end();
        outer.end();
      }),
      sibling(),
    ]);
    tracer.startSpan("after").end();
    expect(parentOf("inside")).toBe(idOf("outer"));
    expect(parentOf("sibling-sync")).toBeUndefined();
    expect(parentOf("sibling-after-await")).toBeUndefined();
    expect(parentOf("after")).toBeUndefined();
  });

  it("context.with(ROOT_CONTEXT) inside a callback still detaches a span from it", () => {
    const { tracer, parentOf } = fixture();
    tracer.startActiveSpan("outer", (outer) => {
      context.with(ROOT_CONTEXT, () => {
        tracer.startSpan("detached").end();
        tracer.startActiveSpan("detached-active", (span) => span.end());
      });
      outer.end();
    });
    expect(parentOf("detached")).toBeUndefined();
    expect(parentOf("detached-active")).toBeUndefined();
  });

  it("a stream-shaped callback behaves as without a context manager", async () => {
    const { tracer, parentOf, idOf } = fixture();
    async function* chunks(span: FISpan) {
      yield "a";
      tracer.startSpan("in-iterator").end();
      yield "b";
      span.end();
    }
    let started: Promise<void> = Promise.resolve();
    const stream = tracer.startActiveSpan("stream", (span) => {
      tracer.startSpan("in-callback").end();
      started = tick().then(() => tracer.startSpan("started-in-callback").end());
      return chunks(span);
    });
    const seen: string[] = [];
    for await (const chunk of stream) seen.push(chunk);
    await started;
    expect(seen).toEqual(["a", "b"]);
    expect(parentOf("in-callback")).toBe(idOf("stream"));
    expect(parentOf("started-in-callback")).toBe(idOf("stream"));
    expect(parentOf("in-iterator")).toBeUndefined();
  });
});
