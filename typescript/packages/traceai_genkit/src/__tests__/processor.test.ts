import { context, diag, ROOT_CONTEXT, trace } from "@opentelemetry/api";
import * as os from "os";
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

const HIDE_ENV = [
  "FI_HIDE_INPUTS",
  "FI_HIDE_OUTPUTS",
  "FI_HIDE_INPUT_MESSAGES",
  "FI_HIDE_OUTPUT_MESSAGES",
  "FI_HIDE_INPUT_TEXT",
  "FI_HIDE_OUTPUT_TEXT",
];
const savedEnv: Record<string, string | undefined> = {};
beforeEach(() => {
  for (const key of HIDE_ENV) {
    savedEnv[key] = process.env[key];
    delete process.env[key];
  }
});
afterEach(() => {
  for (const key of HIDE_ENV) {
    if (savedEnv[key] === undefined) delete process.env[key];
    else process.env[key] = savedEnv[key];
  }
});

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

  it("exports content by default and hides it with traceConfig (option) or FI_HIDE_* (env)", async () => {
    const span = v1Span({ name: "fixture/mock", attributes: modelAttributes(), spanId: "5555555555555555" });
    const on = fiLikeProvider();
    new FIGenkitSpanProcessor({ tracerProvider: on.provider }).onEnd(span);
    expect(on.exporter.getFinishedSpans()[0].attributes["input.value"]).toContain(PROMPT_MARKER);

    const byOption = fiLikeProvider();
    new FIGenkitSpanProcessor({ tracerProvider: byOption.provider, traceConfig: { hideInputs: true, hideOutputs: true } }).onEnd(span);
    expect(JSON.stringify(byOption.exporter.getFinishedSpans()[0].attributes)).not.toContain(PROMPT_MARKER);
    expect(byOption.exporter.getFinishedSpans()[0].attributes["input.value"]).toBe("__REDACTED__");

    process.env.FI_HIDE_INPUTS = "TRUE";
    process.env.FI_HIDE_OUTPUTS = "true";
    const byEnv = fiLikeProvider();
    new FIGenkitSpanProcessor({ tracerProvider: byEnv.provider }).onEnd(span);
    expect(JSON.stringify(byEnv.exporter.getFinishedSpans()[0].attributes)).not.toContain(PROMPT_MARKER);

    // option > env, as in fi-core
    const optionWins = fiLikeProvider();
    new FIGenkitSpanProcessor({ tracerProvider: optionWins.provider, traceConfig: { hideInputs: false, hideOutputs: false } }).onEnd(span);
    expect(optionWins.exporter.getFinishedSpans()[0].attributes["input.value"]).toContain(PROMPT_MARKER);

    // only "true" hides (fi-core's env parsing)
    process.env.FI_HIDE_INPUTS = "1";
    process.env.FI_HIDE_OUTPUTS = "yes";
    const notTrue = fiLikeProvider();
    new FIGenkitSpanProcessor({ tracerProvider: notTrue.provider }).onEnd(span);
    expect(notTrue.exporter.getFinishedSpans()[0].attributes["input.value"]).toContain(PROMPT_MARKER);
  });

  it("treats the removed captureContent: false as hide inputs and outputs, with a warning", async () => {
    const warn = jest.spyOn(diag, "warn").mockImplementation(() => undefined);
    try {
      const span = v1Span({ name: "fixture/mock", attributes: modelAttributes(), spanId: "5555555555555556" });
      const off = fiLikeProvider();
      new FIGenkitSpanProcessor({ tracerProvider: off.provider, captureContent: false }).onEnd(span);
      expect(JSON.stringify(off.exporter.getFinishedSpans()[0].attributes)).not.toContain(PROMPT_MARKER);
      const on = fiLikeProvider();
      new FIGenkitSpanProcessor({ tracerProvider: on.provider, captureContent: true }).onEnd(span);
      expect(on.exporter.getFinishedSpans()[0].attributes["input.value"]).toContain(PROMPT_MARKER);
      expect(warn.mock.calls.filter(([m]) => String(m).includes("captureContent was removed"))).toHaveLength(2);
    } finally {
      warn.mockRestore();
    }
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

describe("FIGenkitSpanProcessor: error text, stack traces and local identity", () => {
  // @genkit-ai/core 1.42.0 src/schema.ts:78 (ValidationError) and src/error.ts:78 (GenkitError prefix).
  // core/src/tracing/instrumentation.ts:156-162 copies e.message to status.message and records the exception.
  const DATA_MARKER = "SECRET_STRUCTURED_OUTPUT_MARKER";
  const user = os.userInfo().username;
  const host = os.hostname();
  const head = "INVALID_ARGUMENT: Schema validation failed. Parse Errors:\n\n- /answer: must be string\n\n";
  const message =
    head +
    `Provided data:\n\n${JSON.stringify({ answer: 42, note: DATA_MARKER }, null, 2)}\n\n` +
    `owner jane.doe@example.com on ${host} as ${user}, config ${process.cwd()}/genkit.config.json, also /Users/someone/x.txt`;
  const stacktrace = `GenkitError: ${message}\n    at parseSchema (${process.cwd()}/node_modules/@genkit-ai/core/lib/schema.js:71:11)`;
  const errorSpan = (spanId: string) =>
    v1Span({
      name: "generate",
      attributes: { ...generateAttributes(), "genkit:state": "error" },
      spanId,
      status: { code: 2, message },
      events: [
        { name: "custom", time: [1, 0], attributes: { note: "not an exception", where: `${os.homedir()}/notes` } },
        {
          name: "exception",
          time: [1, 5],
          attributes: { "exception.type": "GenkitError", "exception.message": message, "exception.stacktrace": stacktrace },
        },
      ],
    });
  const forbidden = ["jane.doe@example.com", user, host, process.cwd(), os.homedir(), "/Users/someone", "schema.js:71"];

  it("default: exports the error message without paths, emails, user or host names, and never the stack trace", async () => {
    const { exporter, provider } = fiLikeProvider();
    const span = errorSpan("9999999999999991");
    const processor = new FIGenkitSpanProcessor({ tracerProvider: provider });
    processor.onEnd(span);
    await processor.forceFlush();
    const [exported] = exporter.getFinishedSpans();
    const blob = JSON.stringify({ status: exported.status, events: exported.events, attributes: exported.attributes });
    for (const value of forbidden) expect(blob).not.toContain(value);
    // Content is on by default: the data Genkit quotes stays (it is the app's own content).
    expect(exported.status.message).toContain(DATA_MARKER);
    expect(exported.status.message).toContain("<email>");
    expect(exported.status.message).toContain("[cwd]/genkit.config.json");
    expect(exported.events.map((e) => e.name)).toEqual(["custom", "exception"]);
    expect(exported.events[0].attributes?.where).toBe("~/notes");
    const exception = exported.events[1];
    expect(exception.time).toEqual([1, 5]);
    expect(exception.attributes?.["exception.type"]).toBe("GenkitError");
    expect(exception.attributes?.["exception.message"]).toBe(exported.status.message);
    expect(exception.attributes).not.toHaveProperty("exception.stacktrace");
    // Genkit's own span (read by the Dev UI exporter) keeps the full message and stack.
    expect(span.status.message).toBe(message);
    expect(span.events[1].attributes?.["exception.stacktrace"]).toBe(stacktrace);
  });

  it("hidden (FI_HIDE_INPUTS / FI_HIDE_OUTPUTS): replaces the message with the error type and __REDACTED__", async () => {
    for (const traceConfig of [{ hideInputs: true }, { hideOutputs: true }]) {
      const { exporter, provider } = fiLikeProvider();
      const processor = new FIGenkitSpanProcessor({ tracerProvider: provider, traceConfig });
      processor.onEnd(errorSpan("9999999999999992"));
      await processor.forceFlush();
      const [exported] = exporter.getFinishedSpans();
      const blob = JSON.stringify({ status: exported.status, events: exported.events, attributes: exported.attributes });
      expect(blob).not.toContain(DATA_MARKER);
      for (const value of forbidden) expect(blob).not.toContain(value);
      expect(exported.status).toEqual({ code: 2, message: "GenkitError: __REDACTED__" });
      expect(exported.events[1].attributes).toEqual({ "exception.type": "GenkitError", "exception.message": "__REDACTED__" });
    }
  });

  it("hidden: a status with no exception event becomes __REDACTED__", async () => {
    const { exporter, provider } = fiLikeProvider();
    const processor = new FIGenkitSpanProcessor({ tracerProvider: provider, traceConfig: { hideInputs: true, hideOutputs: true } });
    processor.onEnd(v1Span({ name: "lookup", attributes: toolAttributes(), spanId: "9999999999999993", status: { code: 2, message: `bad ${DATA_MARKER}` } }));
    await processor.forceFlush();
    expect(exporter.getFinishedSpans()[0].status).toEqual({ code: 2, message: "__REDACTED__" });
  });

  it("leaves a plain error message unchanged by default and an OK status untouched", async () => {
    const { exporter, provider } = fiLikeProvider();
    const processor = new FIGenkitSpanProcessor({ tracerProvider: provider });
    const events = [{ name: "exception", time: [1, 0] as [number, number], attributes: { "exception.type": "Error", "exception.message": "boom", "exception.stacktrace": "Error: boom\n    at x (y.js:1:1)" } }];
    processor.onEnd(v1Span({ name: "lookup", attributes: toolAttributes(), spanId: "9999999999999994", status: { code: 2, message: "boom" }, events }));
    processor.onEnd(v1Span({ name: "qaFlow", attributes: flowAttributes(), spanId: "9999999999999995" }));
    await processor.forceFlush();
    const spans = exportedByName(exporter);
    expect(spans.lookup.status).toEqual({ code: 2, message: "boom" });
    expect(spans.lookup.events[0].attributes).toEqual({ "exception.type": "Error", "exception.message": "boom" });
    expect(spans.qaFlow.status).toEqual({ code: 0 });
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
