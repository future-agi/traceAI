import { context, ROOT_CONTEXT, trace } from "@opentelemetry/api";
import {
  BasicTracerProvider,
  InMemorySpanExporter,
  SimpleSpanProcessor,
  type ReadableSpan,
} from "@opentelemetry/sdk-trace-base";
import { resourceFromAttributes } from "@opentelemetry/resources";
import { ProjectType, register, setSession, setUser } from "@traceai/fi-core";
import { FIGenkitSpanProcessor } from "../index";
import {
  PROMPT_MARKER,
  TRACE_ID,
  flowAttributes,
  generateAttributes,
  modelAttributes,
  toolAttributes,
  v1Span,
} from "./fixtures";

function fiLikeProvider() {
  const exporter = new InMemorySpanExporter();
  const provider = new BasicTracerProvider({
    resource: resourceFromAttributes({ project_name: "unit-project", project_type: "observe" }),
    spanProcessors: [new SimpleSpanProcessor(exporter)],
  });
  return { exporter, provider };
}

function exportedByName(exporter: InMemorySpanExporter): Record<string, ReadableSpan> {
  return Object.fromEntries(exporter.getFinishedSpans().map((s) => [s.name, s]));
}

describe("FIGenkitSpanProcessor: forwarding to the Future AGI provider", () => {
  it("re-shapes sdk-trace-base 1.x spans for the 2.x exporter: parent, scope, FI resource", async () => {
    const { exporter, provider } = fiLikeProvider();
    const processor = new FIGenkitSpanProcessor({ tracerProvider: provider });
    const flow = v1Span({ name: "qaFlow", attributes: flowAttributes(), spanId: "1111111111111111" });
    const model = v1Span({ name: "fixture/mock", attributes: modelAttributes(), spanId: "2222222222222222", parentSpanId: "1111111111111111" });
    processor.onEnd(model);
    processor.onEnd(flow);
    await processor.forceFlush();

    const spans = exportedByName(exporter);
    expect(Object.keys(spans).sort()).toEqual(["fixture/mock", "qaFlow"]);
    const exportedModel = spans["fixture/mock"];
    expect(exportedModel.parentSpanContext?.spanId).toBe("1111111111111111");
    expect(exportedModel.parentSpanContext?.traceId).toBe(TRACE_ID);
    expect(exportedModel.spanContext().spanId).toBe("2222222222222222");
    expect(spans.qaFlow.parentSpanContext).toBeUndefined();
    expect(exportedModel.instrumentationScope).toEqual({ name: "genkit-tracer", version: "v1" });
    expect(exportedModel.resource.attributes).toMatchObject({ project_name: "unit-project", project_type: "observe" });
    expect(exportedModel.attributes["fi.span.kind"]).toBe("LLM");
    expect(exportedModel.attributes["gen_ai.usage.input_tokens"]).toBe(11);
    expect(exportedModel.startTime).toEqual(model.startTime);
    expect(exportedModel.endTime).toEqual(model.endTime);
  });

  it("passes OTel status and exception events through", async () => {
    const { exporter, provider } = fiLikeProvider();
    const processor = new FIGenkitSpanProcessor({ tracerProvider: provider });
    processor.onEnd(
      v1Span({
        name: "lookup",
        attributes: { ...toolAttributes(), "genkit:state": "error" },
        spanId: "3333333333333333",
        status: { code: 2, message: "tool exploded" },
        events: [{ name: "exception", time: [1, 0], attributes: { "exception.type": "Error", "exception.message": "tool exploded" } }],
      }),
    );
    await processor.forceFlush();
    const [span] = exporter.getFinishedSpans();
    expect(span.status).toEqual({ code: 2, message: "tool exploded" });
    expect(span.events.map((e) => e.name)).toEqual(["exception"]);
    expect(span.attributes["fi.span.kind"]).toBe("TOOL");
  });

  it("never modifies Genkit's span (Genkit's own exporters read the same object)", async () => {
    const { provider } = fiLikeProvider();
    const processor = new FIGenkitSpanProcessor({ tracerProvider: provider });
    const span = v1Span({ name: "generate", attributes: generateAttributes(), spanId: "4444444444444444" });
    const before = JSON.stringify(span.attributes);
    processor.onEnd(span);
    await processor.forceFlush();
    expect(JSON.stringify(span.attributes)).toBe(before);
    expect(span.attributes["genkit:input"]).toContain(PROMPT_MARKER);
    expect(span.resource).toEqual({ attributes: { "service.name": "genkit-app" } });
  });

  it("drops content by default and exports it with captureContent", async () => {
    const off = fiLikeProvider();
    const on = fiLikeProvider();
    const span = v1Span({ name: "fixture/mock", attributes: modelAttributes(), spanId: "5555555555555555" });
    new FIGenkitSpanProcessor({ tracerProvider: off.provider }).onEnd(span);
    new FIGenkitSpanProcessor({ tracerProvider: on.provider, captureContent: true }).onEnd(span);
    expect(JSON.stringify(off.exporter.getFinishedSpans()[0].attributes)).not.toContain(PROMPT_MARKER);
    expect(on.exporter.getFinishedSpans()[0].attributes["input.value"]).toContain(PROMPT_MARKER);
  });

  it("applies session/user set with the fi-core context helpers at span start", async () => {
    const { exporter, provider } = fiLikeProvider();
    const processor = new FIGenkitSpanProcessor({ tracerProvider: provider });
    const span = v1Span({ name: "qaFlow", attributes: flowAttributes(), spanId: "6666666666666666" });
    const ctx = setUser(setSession(ROOT_CONTEXT, { sessionId: "sess-42" }), { userId: "user-7" });
    processor.onStart(span, ctx);
    processor.onEnd(span);
    const [exported] = exporter.getFinishedSpans();
    expect(exported.attributes["session.id"]).toBe("sess-42");
    expect(exported.attributes["user.id"]).toBe("user-7");
  });
});

describe("FIGenkitSpanProcessor: failure isolation", () => {
  it("onEnd never throws when the downstream processor throws", () => {
    const { provider } = fiLikeProvider();
    const processor = new FIGenkitSpanProcessor({ tracerProvider: provider });
    (provider as any)._activeSpanProcessor.onEnd = () => {
      throw new Error("exporter blew up");
    };
    expect(() => processor.onEnd(v1Span({ name: "x", attributes: flowAttributes(), spanId: "7777777777777777" }))).not.toThrow();
  });

  it("onEnd never throws on a malformed span", () => {
    const { provider } = fiLikeProvider();
    const processor = new FIGenkitSpanProcessor({ tracerProvider: provider });
    expect(() => processor.onEnd({} as any)).not.toThrow();
    expect(() => processor.onStart({}, undefined as any)).not.toThrow();
  });

  it("forceFlush and shutdown resolve when the provider rejects or throws", async () => {
    const provider = fiLikeProvider().provider;
    const processor = new FIGenkitSpanProcessor({ tracerProvider: provider });
    provider.forceFlush = () => Promise.reject(new Error("collector down"));
    provider.shutdown = () => {
      throw new Error("sync shutdown failure");
    };
    await expect(processor.forceFlush()).resolves.toBeUndefined();
    await expect(processor.shutdown()).resolves.toBeUndefined();
    await expect(processor.shutdown()).resolves.toBeUndefined();
  });

  it("forceFlush returns after flushTimeoutMillis when the exporter hangs", async () => {
    const provider = fiLikeProvider().provider;
    provider.forceFlush = () => new Promise<void>(() => undefined);
    const processor = new FIGenkitSpanProcessor({ tracerProvider: provider, flushTimeoutMillis: 50 });
    const started = Date.now();
    await expect(processor.forceFlush()).resolves.toBeUndefined();
    expect(Date.now() - started).toBeLessThan(2000);
  });

  it("drops spans after shutdown instead of throwing", async () => {
    const { exporter, provider } = fiLikeProvider();
    const processor = new FIGenkitSpanProcessor({ tracerProvider: provider });
    await processor.shutdown();
    expect(() => processor.onEnd(v1Span({ name: "late", attributes: flowAttributes(), spanId: "8888888888888888" }))).not.toThrow();
    expect(exporter.getFinishedSpans()).toEqual([]);
  });

  it("shutdown flushes before shutting the provider down", async () => {
    const order: string[] = [];
    const provider = fiLikeProvider().provider;
    const flush = provider.forceFlush.bind(provider);
    const stop = provider.shutdown.bind(provider);
    provider.forceFlush = async () => {
      order.push("flush");
      await flush();
    };
    provider.shutdown = async () => {
      order.push("shutdown");
      await stop();
    };
    await new FIGenkitSpanProcessor({ tracerProvider: provider }).shutdown();
    expect(order).toEqual(["flush", "shutdown"]);
  });
});

describe("FIGenkitSpanProcessor: construction", () => {
  it("rejects something that is not a tracer provider", () => {
    expect(() => new FIGenkitSpanProcessor({} as any)).toThrow(TypeError);
    expect(() => new FIGenkitSpanProcessor({ tracerProvider: { forceFlush: async () => {}, shutdown: async () => {} } })).toThrow(
      /register\(\) from @traceai\/fi-core/,
    );
  });

  it("reads the processor and resource of a real fi-core register() provider", async () => {
    const provider = register({
      projectName: "unit-register",
      projectType: ProjectType.OBSERVE,
      setGlobalTracerProvider: false,
      endpoint: "http://127.0.0.1:1",
      headers: { "x-api-key": "PLACEHOLDER", "x-secret-key": "PLACEHOLDER" },
    });
    const processor = new FIGenkitSpanProcessor({ tracerProvider: provider });
    expect((processor as any).resource.attributes).toMatchObject({ project_name: "unit-register", project_type: "observe" });
    expect(typeof (processor as any).target.onEnd).toBe("function");
    await processor.shutdown();
  });

  it("warns when the Future AGI provider was registered as the global provider", () => {
    const { provider } = fiLikeProvider();
    const emit = jest.spyOn(process, "emitWarning").mockImplementation(() => undefined);
    try {
      trace.setGlobalTracerProvider(provider);
      new FIGenkitSpanProcessor({ tracerProvider: provider });
      expect(emit).toHaveBeenCalledWith(expect.stringContaining("setGlobalTracerProvider: false"), expect.objectContaining({ code: "TRACEAI_GENKIT_GLOBAL_PROVIDER" }));
      emit.mockClear();
      new FIGenkitSpanProcessor({ tracerProvider: fiLikeProvider().provider });
      expect(emit).not.toHaveBeenCalled();
    } finally {
      trace.disable();
      context.disable();
      emit.mockRestore();
    }
  });
});
