import { createContextKey, ROOT_CONTEXT, trace } from "@opentelemetry/api";
import { BasicTracerProvider, InMemorySpanExporter, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { activeFIContext, FITracer, runInFIContext, setSession } from "../index";

// No OpenTelemetry context manager is registered in this file.
const KEY = createContextKey("fi-core.test.value");

function fixture() {
  const exporter = new InMemorySpanExporter();
  const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
  const tracer = new FITracer({ tracer: provider.getTracer("active-context"), traceConfig: { piiRedaction: false } });
  const span = (name: string) => {
    const found = exporter.getFinishedSpans().find((s) => s.name === name);
    if (!found) throw new Error(`no finished span named ${name}`);
    return found;
  };
  const parentOf = (name: string) => span(name).parentSpanContext?.spanId;
  return { tracer, span, parentOf };
}

describe("runInFIContext and activeFIContext without a context manager", () => {
  it("activeFIContext is the root context outside any callback", () => {
    expect(activeFIContext()).toBe(ROOT_CONTEXT);
  });

  it("activeFIContext gives the enclosing startActiveSpan's span; the API's active span stays unset", async () => {
    const { tracer } = fixture();
    await tracer.startActiveSpan("outer", async (outer) => {
      await Promise.resolve();
      expect(trace.getSpan(activeFIContext())).toBe(outer.getContextSpan());
      // Nothing is registered with the OpenTelemetry API.
      expect(trace.getActiveSpan()).toBeUndefined();
      outer.end();
    });
    expect(activeFIContext()).toBe(ROOT_CONTEXT);
  });

  it("runInFIContext runs fn in the context, returns its value and restores the caller's context", () => {
    const { tracer, parentOf } = fixture();
    const parent = tracer.startSpan("parent");
    const ctx = trace.setSpan(ROOT_CONTEXT, parent.getContextSpan()).setValue(KEY, "kept");
    const result = runInFIContext(ctx, () => {
      expect(activeFIContext().getValue(KEY)).toBe("kept");
      tracer.startSpan("child").end();
      return 7;
    });
    parent.end();
    expect(result).toBe(7);
    expect(parentOf("child")).toBe(parent.spanContext().spanId);
    expect(activeFIContext()).toBe(ROOT_CONTEXT);
  });

  it("a session set through runInFIContext reaches spans started inside it, nested ones too", () => {
    const { tracer, span } = fixture();
    runInFIContext(setSession(activeFIContext(), { sessionId: "session-demo" }), () => {
      tracer.startActiveSpan("with-session", (outer) => {
        tracer.startSpan("nested").end();
        outer.end();
      });
    });
    expect(span("with-session").attributes["session.id"]).toBe("session-demo");
    expect(span("nested").attributes["session.id"]).toBe("session-demo");
  });

  it("a callback that throws runs once, and the caller's context is restored", () => {
    let calls = 0;
    expect(() => runInFIContext(ROOT_CONTEXT.setValue(KEY, "inside"), () => {
      calls++;
      throw new Error("callback failed");
    })).toThrow("callback failed");
    expect(calls).toBe(1);
    expect(activeFIContext()).toBe(ROOT_CONTEXT);
  });

  it("every loaded copy of fi-core shares one store", () => {
    const loaded: { copy?: { activeFIContext: typeof activeFIContext; runInFIContext: typeof runInFIContext } } = {};
    jest.isolateModules(() => {
      loaded.copy = require("../trace/trace-config/activeContext");
    });
    const copy = loaded.copy!;
    expect(copy.runInFIContext).not.toBe(runInFIContext);
    const ctx = ROOT_CONTEXT.setValue(KEY, "shared");
    runInFIContext(ctx, () => expect(copy.activeFIContext().getValue(KEY)).toBe("shared"));
    copy.runInFIContext(ctx, () => expect(activeFIContext().getValue(KEY)).toBe("shared"));
  });
});
