import { describe, it, expect, beforeAll, afterAll, beforeEach } from "@jest/globals";
import { Attributes, context, SpanStatusCode, trace } from "@opentelemetry/api";
import { AsyncLocalStorageContextManager } from "@opentelemetry/context-async-hooks";
import {
  BasicTracerProvider,
  InMemorySpanExporter,
  ReadableSpan,
  Sampler,
  SamplingDecision,
  SamplingResult,
  SimpleSpanProcessor,
} from "@opentelemetry/sdk-trace-base";
import { SemanticConventions } from "@traceai/fi-semantic-conventions";
import { FITracer } from "../trace/trace-config/FITracer";
import { REDACTED_VALUE } from "../trace/trace-config/constants";
import { setSession } from "../trace/contextAttributes";
import { TraceConfigOptions } from "../trace/trace-config/types";

const PROMPT = "my secret prompt: the launch code is 0000";

/**
 * The attributes an instrumentor such as @traceai/openai passes to startSpan: the prompt is in
 * input.value, the input messages and the raw request body.
 */
function llmStartAttributes(): Attributes {
  const body = { model: "gpt-x", messages: [{ role: "user", content: PROMPT }] };
  return {
    [SemanticConventions.FI_SPAN_KIND]: "LLM",
    [SemanticConventions.LLM_MODEL_NAME]: "gpt-x",
    [SemanticConventions.INPUT_VALUE]: JSON.stringify(body),
    [SemanticConventions.INPUT_MIME_TYPE]: "application/json",
    [SemanticConventions.LLM_INPUT_MESSAGES]: JSON.stringify(body.messages),
    [SemanticConventions.RAW_INPUT]: JSON.stringify(body),
    [SemanticConventions.PROMPT_TEMPLATE_VARIABLES]: JSON.stringify({ question: PROMPT }),
    "error.message": PROMPT,
    "exception.stacktrace": `Error: ${PROMPT}\n    at run (/private/tmp/app.js:1:1)`,
    "autogen.error.message": PROMPT,
  };
}

/** Everything the exporter is handed for the span, as text */
function exported(span: ReadableSpan): string {
  return JSON.stringify({
    name: span.name,
    attributes: span.attributes,
    events: span.events,
    status: span.status,
    links: span.links,
  });
}

/** Records the attributes each sampling decision saw */
class RecordingSampler implements Sampler {
  seen: Attributes[] = [];
  shouldSample(
    _context: unknown,
    _traceId: string,
    _spanName: string,
    _spanKind: unknown,
    attributes: Attributes,
  ): SamplingResult {
    this.seen.push({ ...attributes });
    return { decision: SamplingDecision.RECORD_AND_SAMPLED };
  }
  toString() {
    return "RecordingSampler";
  }
}

describe("FITracer creation-time attributes", () => {
  const exporter = new InMemorySpanExporter();
  const sampler = new RecordingSampler();
  const provider = new BasicTracerProvider({
    sampler,
    spanProcessors: [new SimpleSpanProcessor(exporter)],
  });
  const contextManager = new AsyncLocalStorageContextManager();

  beforeAll(() => {
    context.setGlobalContextManager(contextManager.enable());
  });
  afterAll(() => {
    context.disable();
  });
  beforeEach(() => {
    exporter.reset();
    sampler.seen = [];
  });

  function tracerWith(traceConfig: TraceConfigOptions) {
    return new FITracer({ tracer: provider.getTracer("test"), traceConfig });
  }

  function onlySpan(): ReadableSpan {
    const spans = exporter.getFinishedSpans();
    expect(spans).toHaveLength(1);
    return spans[0];
  }

  /** The common instrumentor shape: content at creation, then a failure quoting the prompt */
  function failingLLMCall(tracer: FITracer) {
    const span = tracer.startSpan("llm call", { attributes: llmStartAttributes() });
    const error = Object.assign(new Error(`404 model not found for input "${PROMPT}"`), {
      code: "model_not_found",
    });
    span.recordException(error);
    span.setStatus({ code: SpanStatusCode.ERROR, message: error.message });
    span.end();
  }

  it("the reviewer's repro: startSpan attributes are masked with hideInputs", () => {
    tracerWith({ hideInputs: true })
      .startSpan("x", { attributes: { "input.value": PROMPT, "error.message": PROMPT } })
      .end();
    const { attributes } = onlySpan();
    expect(attributes).toEqual({ "input.value": REDACTED_VALUE });
  });

  describe.each([
    ["hideInputs", { hideInputs: true }],
    ["hideInputs, env-style explicit hideErrorDetails", { hideInputs: true, hideErrorDetails: true }],
  ])("hidden (%s)", (_label, traceConfig: TraceConfigOptions) => {
    it("startSpan: the prompt is in no exported attribute, event or status", () => {
      failingLLMCall(tracerWith(traceConfig));
      const span = onlySpan();
      expect(exported(span)).not.toContain(PROMPT);
      expect(exported(span)).not.toContain("/private/tmp");
      expect(span.attributes[SemanticConventions.INPUT_VALUE]).toBe(REDACTED_VALUE);
      expect(span.attributes[SemanticConventions.RAW_INPUT]).toBe(REDACTED_VALUE);
      expect(span.attributes[SemanticConventions.PROMPT_TEMPLATE_VARIABLES]).toBe(
        REDACTED_VALUE,
      );
      expect(span.attributes[SemanticConventions.LLM_INPUT_MESSAGES]).toBeUndefined();
      expect(span.attributes[SemanticConventions.INPUT_MIME_TYPE]).toBeUndefined();
      // Attributes that are not content are kept
      expect(span.attributes[SemanticConventions.LLM_MODEL_NAME]).toBe("gpt-x");
      expect(span.attributes[SemanticConventions.FI_SPAN_KIND]).toBe("LLM");
      expect(span.attributes["error.type"]).toBe("model_not_found");
      expect(span.status).toEqual({ code: SpanStatusCode.ERROR });
    });

    it("startActiveSpan: the prompt is in no exported attribute, event or status", () => {
      const tracer = tracerWith(traceConfig);
      context.with(setSession(context.active(), { sessionId: "session-1" }), () => {
        tracer.startActiveSpan("llm call", { attributes: llmStartAttributes() }, (span) => {
          trace.getActiveSpan()?.recordException(new Error(PROMPT));
          trace.getActiveSpan()?.setStatus({ code: SpanStatusCode.ERROR, message: PROMPT });
          span.end();
        });
      });
      const span = onlySpan();
      expect(exported(span)).not.toContain(PROMPT);
      expect(span.attributes[SemanticConventions.INPUT_VALUE]).toBe(REDACTED_VALUE);
      // Context attributes still arrive
      expect(span.attributes[SemanticConventions.SESSION_ID]).toBe("session-1");
    });

    it("the sampler sees the masked attributes, not the prompt", () => {
      const tracer = tracerWith(traceConfig);
      tracer.startSpan("a", { attributes: llmStartAttributes() }).end();
      tracer.startActiveSpan("b", { attributes: llmStartAttributes() }, (span) => span.end());
      expect(sampler.seen).toHaveLength(2);
      for (const attributes of sampler.seen) {
        expect(JSON.stringify(attributes)).not.toContain(PROMPT);
        expect(attributes[SemanticConventions.INPUT_VALUE]).toBe(REDACTED_VALUE);
        // Attributes a sampler can route on still reach it
        expect(attributes[SemanticConventions.LLM_MODEL_NAME]).toBe("gpt-x");
        expect(attributes[SemanticConventions.FI_SPAN_KIND]).toBe("LLM");
      }
    });
  });

  it("hideErrorDetails alone drops creation-time error text and keeps content", () => {
    tracerWith({ hideErrorDetails: true })
      .startSpan("x", { attributes: llmStartAttributes() })
      .end();
    const { attributes } = onlySpan();
    expect(attributes["error.message"]).toBeUndefined();
    expect(attributes["exception.stacktrace"]).toBeUndefined();
    expect(attributes["autogen.error.message"]).toBeUndefined();
    expect(attributes[SemanticConventions.INPUT_VALUE]).toContain(PROMPT);
  });

  it("hideOutputs masks raw.output like output.value", () => {
    const span = tracerWith({ hideOutputs: true }).startSpan("x", {
      attributes: {
        [SemanticConventions.OUTPUT_VALUE]: PROMPT,
        [SemanticConventions.RAW_OUTPUT]: PROMPT,
        [SemanticConventions.RAW_INPUT]: "kept",
      },
    });
    span.setAttributes({ [SemanticConventions.RAW_OUTPUT]: PROMPT });
    span.end();
    const { attributes } = onlySpan();
    expect(attributes[SemanticConventions.OUTPUT_VALUE]).toBe(REDACTED_VALUE);
    expect(attributes[SemanticConventions.RAW_OUTPUT]).toBe(REDACTED_VALUE);
    expect(attributes[SemanticConventions.RAW_INPUT]).toBe("kept");
  });

  it("PII redaction applies to creation-time attributes", () => {
    tracerWith({ piiRedaction: true })
      .startSpan("x", { attributes: { "input.value": "mail john@example.com" } })
      .end();
    expect(onlySpan().attributes["input.value"]).toBe("mail <EMAIL_ADDRESS>");
  });

  it("with no flags, creation-time attributes are unchanged", () => {
    tracerWith({}).startSpan("x", { attributes: llmStartAttributes() }).end();
    expect(onlySpan().attributes).toEqual(llmStartAttributes());
  });

  it("an error.type passed at creation is kept", () => {
    const span = tracerWith({ hideInputs: true }).startSpan("x", {
      attributes: { "error.type": "RateLimitError" },
    });
    span.recordException(new TypeError("boom"));
    span.setStatus({ code: SpanStatusCode.ERROR });
    span.end();
    expect(onlySpan().attributes["error.type"]).toBe("RateLimitError");
  });

  it("other span options still reach the SDK", () => {
    const parent = tracerWith({}).startSpan("parent");
    const parentContext = trace.setSpan(context.active(), parent);
    const child = tracerWith({ hideInputs: true }).startSpan(
      "child",
      { kind: 2, startTime: [1700000000, 0], attributes: { "input.value": PROMPT } },
      parentContext,
    );
    child.end();
    parent.end();
    const finished = exporter.getFinishedSpans().find((s) => s.name === "child");
    expect(finished?.kind).toBe(2);
    expect(finished?.startTime).toEqual([1700000000, 0]);
    expect(finished?.parentSpanContext?.spanId).toBe(parent.spanContext().spanId);
    expect(finished?.attributes["input.value"]).toBe(REDACTED_VALUE);
  });
});
