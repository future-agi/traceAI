import { context, createContextKey, ROOT_CONTEXT } from "@opentelemetry/api";
import { BasicTracerProvider, InMemorySpanExporter, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { FITracer } from "../trace/trace-config/FITracer";
import type { FISpan } from "../trace/trace-config/FISpan";

// An application with no OpenTelemetry context manager: nothing in this file
// registers one, so the API's context.with does not carry context.
const PROBE = createContextKey("fi-core.test.no-context-manager");
const tick = () => new Promise<void>((resolve) => setTimeout(resolve, 1));

test("no context manager is registered in this file", () => {
  const seen = context.with(ROOT_CONTEXT.setValue(PROBE, true), () => context.active().getValue(PROBE));
  expect(seen).toBeUndefined();
});

describe.each(["standalone", "auto"] as const)("startActiveSpan without a context manager (%s)", (mode) => {
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

  it("(a) nests a startActiveSpan started synchronously inside the callback", () => {
    const { tracer, parentOf, idOf } = fixture();
    const result = tracer.startActiveSpan("outer", (outer) => {
      tracer.startActiveSpan("inner", (inner) => inner.end());
      tracer.startSpan("child").end();
      outer.end();
      return 42;
    });
    expect(result).toBe(42);
    expect(parentOf("inner")).toBe(idOf("outer"));
    expect(parentOf("child")).toBe(idOf("outer"));
  });

  it("(a) nests a startActiveSpan started after an await, at every depth", async () => {
    const { tracer, parentOf, idOf } = fixture();
    await tracer.startActiveSpan("outer", async (outer) => {
      await Promise.resolve();
      await tick();
      await tracer.startActiveSpan("inner", async (inner) => {
        await tick();
        tracer.startSpan("grandchild").end();
        inner.end();
      });
      tracer.startSpan("child-after-inner").end();
      outer.end();
    });
    expect(parentOf("inner")).toBe(idOf("outer"));
    expect(parentOf("grandchild")).toBe(idOf("inner"));
    expect(parentOf("child-after-inner")).toBe(idOf("outer"));
  });

  it("(b) the span is not active in the caller's flow once the callback's promise resolves", async () => {
    const { tracer, parentOf, idOf } = fixture();
    await tracer.startActiveSpan("outer", async (outer) => {
      await tick();
      tracer.startSpan("inside").end();
      outer.end();
    });
    tracer.startSpan("after").end();
    await tick();
    tracer.startSpan("after-await").end();
    // Control: the span was active inside its callback.
    expect(parentOf("inside")).toBe(idOf("outer"));
    expect(parentOf("after")).toBeUndefined();
    expect(parentOf("after-await")).toBeUndefined();
  });

  it("(b) a span still open after its callback returns is not active in the caller's flow", () => {
    const { tracer, parentOf, idOf } = fixture();
    const open = tracer.startActiveSpan("open", (span) => {
      tracer.startSpan("inside").end();
      return span;
    });
    tracer.startSpan("beside").end();
    open.end();
    expect(parentOf("inside")).toBe(idOf("open"));
    expect(parentOf("beside")).toBeUndefined();
  });

  it("(c) a sibling flow started in the same tick does not see the span as active", async () => {
    const { tracer, parentOf, idOf } = fixture();
    const sibling = async () => {
      tracer.startSpan("sibling-sync").end();
      await Promise.resolve();
      tracer.startSpan("sibling-after-await").end();
      await tick();
      tracer.startSpan("sibling-after-timer").end();
    };
    await Promise.all([
      tracer.startActiveSpan("outer", async (outer) => {
        await Promise.resolve();
        tracer.startSpan("inside").end();
        await tick();
        await tick();
        outer.end();
      }),
      sibling(),
    ]);
    expect(parentOf("inside")).toBe(idOf("outer"));
    expect(parentOf("sibling-sync")).toBeUndefined();
    expect(parentOf("sibling-after-await")).toBeUndefined();
    expect(parentOf("sibling-after-timer")).toBeUndefined();
  });

  it("(d) a stream-shaped callback: work started inside it nests, an iterator body read after return does not", async () => {
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
    // Pinned: the iterator body runs in the reader's flow, after the callback
    // returned, so the stream span is not active there. Same as with a context manager.
    expect(parentOf("in-iterator")).toBeUndefined();
  });

  it("an explicit context or root option inside a callback still wins", () => {
    const { tracer, parentOf } = fixture();
    tracer.startActiveSpan("outer", (outer) => {
      tracer.startSpan("explicit-root", undefined, ROOT_CONTEXT).end();
      tracer.startActiveSpan("explicit-root-active", {}, ROOT_CONTEXT, (span) => span.end());
      tracer.startSpan("root-option", { root: true }).end();
      outer.end();
    });
    expect(parentOf("explicit-root")).toBeUndefined();
    expect(parentOf("explicit-root-active")).toBeUndefined();
    expect(parentOf("root-option")).toBeUndefined();
  });

  it("startSpan does not make its span active", () => {
    const { tracer, parentOf } = fixture();
    const first = tracer.startSpan("first");
    tracer.startSpan("second").end();
    first.end();
    expect(parentOf("second")).toBeUndefined();
  });
});
