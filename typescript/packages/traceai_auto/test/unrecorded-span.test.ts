import { ROOT_CONTEXT, trace, type Span } from "@opentelemetry/api";
import { BasicTracerProvider, ParentBasedSampler, SamplingDecision, SimpleSpanProcessor, TraceIdRatioBasedSampler, type Sampler } from "@opentelemetry/sdk-trace-base";
import { markAgentSpan } from "../src/agent-context";
import { endCoverage, patchFetch, shouldSkip, trackCoverage, type FetchHook } from "../src/wire/fetch";
import { removePromiseObservation } from "../src/wire/promise-node";

// An application whose sampler did not sample a trace gives each of its spans in that
// trace a span that never records: the trace id is kept, the span id is new, and there
// is no parent id. Such a span looks the same whether the AI request under it is the model
// call's own or a separate request beside that call in the same trace. So the wire layer
// never matches it to a model span by trace id: the request is captured, never lost. When
// it was the model call's own request, Future AGI receives that call twice, a stated limit.

// Records every span with the sampled flag off, as the agent's sampler does for an AI
// span under a parent the application did not sample.
const recordOnly: Sampler = { shouldSample: () => ({ decision: SamplingDecision.RECORD }), toString: () => "RecordOnly" };

function unsampledTrace() {
  const processor = new SimpleSpanProcessor({ export(_s, cb) { cb({ code: 0 }); }, async shutdown() {} });
  processor.onStart = trackCoverage; processor.onEnd = endCoverage;
  const agent = new BasicTracerProvider({ sampler: recordOnly, spanProcessors: [processor] }).getTracer("ai");
  // ParentBased(TraceIdRatioBased(0)): no new trace is sampled, and child spans follow their parent's flag.
  const app = new BasicTracerProvider({ sampler: new ParentBasedSampler({ root: new TraceIdRatioBasedSampler(0) }) }).getTracer("app");
  const request = app.startSpan("app-request", {}, ROOT_CONTEXT), requestContext = trace.setSpan(ROOT_CONTEXT, request);
  return {
    request,
    /** A model span the agent made, directly under the application's request span. */
    modelSpan(): Span {
      const span = agent.startSpan("llm", { attributes: { "gen_ai.span.kind": "LLM" } }, requestContext);
      markAgentSpan(span);
      return span;
    },
    /** A span of the application's, started with `parent` active. */
    appSpan(name: string, parent: Span): Span { return app.startSpan(name, {}, trace.setSpan(requestContext, parent)); },
    /** Whether the wire layer leaves a request alone when `span` is active. */
    yields(span: Span): boolean { return shouldSkip(trace.setSpan(requestContext, span)); },
  };
}

describe("on the default route, where only the agent's spans reach Future AGI", () => {
  let hook: FetchHook;
  beforeAll(() => { hook = patchFetch(4096, () => {}, [], undefined, true); });
  afterAll(() => { hook.remove(); removePromiseObservation(); });

  test("a request under a span the sampler did not record is captured while a model span is live in its trace", () => {
    const app = unsampledTrace(), model = app.modelSpan(), { traceId } = model.spanContext();
    // The control, the same with or without a match by trace id: the model span's own request,
    // and a span that carries the model span's own span context (as the API's no-op tracer
    // gives one), both yield to the live model span.
    expect(app.yields(model)).toBe(true);
    expect(app.yields(trace.wrapSpanContext(model.spanContext()))).toBe(true);
    // The application's request span, beside the model span: not recorded, in the same trace.
    expect(app.request.isRecording()).toBe(false);
    expect(app.request.spanContext().traceId).toBe(traceId);
    expect(app.request.spanContext().traceFlags & 1).toBe(0);
    // A request under it is captured: it may be a separate AI request, which must not be lost.
    expect(app.yields(app.request)).toBe(false);
    // The same for any other span in the trace that never recorded and has an id of its own.
    expect(app.yields(trace.wrapSpanContext({ traceId, spanId: "3".repeat(16), traceFlags: 0 }))).toBe(false);
    // A span the application starts inside the model span looks no different, so a request
    // under it is captured too: the stated duplicate.
    const inside = app.appSpan("app-provider-request", model);
    expect(inside.isRecording()).toBe(false);
    expect(inside.spanContext().traceId).toBe(traceId);
    expect(inside.spanContext().spanId).not.toBe(model.spanContext().spanId);
    expect(app.yields(inside)).toBe(false);
    model.end();
  });
});
