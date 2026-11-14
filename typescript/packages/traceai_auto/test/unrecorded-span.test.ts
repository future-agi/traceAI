import { ROOT_CONTEXT, trace, type Span } from "@opentelemetry/api";
import { BasicTracerProvider, ParentBasedSampler, SamplingDecision, SimpleSpanProcessor, TraceIdRatioBasedSampler, type Sampler } from "@opentelemetry/sdk-trace-base";
import { markAgentSpan } from "../src/agent-context";
import { endCoverage, patchFetch, shouldSkip, trackCoverage, type FetchHook } from "../src/wire/fetch";
import { removePromiseObservation } from "../src/wire/promise-node";

// An application whose sampler did not sample a trace gives each of its spans in that
// trace a span that never records: the trace id is kept, the span id is new, and there
// is no parent id. When the provider request of an AI call runs inside such a span, the
// wire layer can connect the request to the AI span only by trace id.

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
    request, requestContext,
    /** A span on the agent's provider, directly under the application's request span. */
    agentSpan(kind: string, made = true): Span {
      const span = agent.startSpan(kind.toLowerCase(), { attributes: { "gen_ai.span.kind": kind } }, requestContext);
      if (made) markAgentSpan(span);
      return span;
    },
    /** A span of the application's, started with `parent` active. */
    appSpan(name: string, parent: Span): Span { return app.startSpan(name, {}, trace.setSpan(requestContext, parent)); },
    /** Whether the wire layer leaves a request alone when `span` is active. */
    yields(span: Span): boolean { return shouldSkip(trace.setSpan(requestContext, span)); },
  };
}

test("off the default route, a span that was not recorded is not matched to a model span by its trace id", () => {
  const app = unsampledTrace(), model = app.agentSpan("LLM"), inside = app.appSpan("app-provider-request", model);
  expect(inside.isRecording()).toBe(false);
  expect(app.yields(model)).toBe(true);
  expect(app.yields(inside)).toBe(false);
  model.end();
});

describe("on the default route, where only the agent's spans reach Future AGI", () => {
  let hook: FetchHook;
  beforeAll(() => { hook = patchFetch(4096, () => {}, [], undefined, true); });
  afterAll(() => { hook.remove(); removePromiseObservation(); });

  test("a request inside application spans the sampler did not record yields to the one live model span in the trace", () => {
    const app = unsampledTrace(), model = app.agentSpan("LLM"), inside = app.appSpan("app-provider-request", model);
    // Not recorded: the model span's trace id, a span id of its own, the sampled flag off.
    expect(inside.isRecording()).toBe(false);
    expect(inside.spanContext().traceId).toBe(model.spanContext().traceId);
    expect(inside.spanContext().spanId).not.toBe(model.spanContext().spanId);
    expect(inside.spanContext().traceFlags & 1).toBe(0);
    expect(app.yields(inside)).toBe(true);
    // A second such span inside the first is matched the same way.
    const attempt = app.appSpan("app-provider-attempt", inside);
    expect(attempt.isRecording()).toBe(false);
    expect(app.yields(attempt)).toBe(true);
    // Once the model span has ended, a later request in the trace is captured again.
    model.end();
    expect(app.yields(inside)).toBe(false);
  });

  test("with two live model spans in the trace, the request is not matched to either", () => {
    const app = unsampledTrace(), first = app.agentSpan("LLM"), second = app.agentSpan("EMBEDDING");
    const inside = app.appSpan("app-provider-request", first);
    expect(app.yields(inside)).toBe(false);
    // With one of them ended, exactly one is left.
    second.end();
    expect(app.yields(inside)).toBe(true);
    first.end();
  });

  test("a trace with no live model span of the agent's is not matched", () => {
    const app = unsampledTrace();
    // The application's request span itself: nothing of the agent's is live in the trace.
    expect(app.request.isRecording()).toBe(false);
    expect(app.yields(app.request)).toBe(false);
    // A span of the agent's that is not a model span.
    const chain = app.agentSpan("CHAIN");
    expect(app.yields(app.appSpan("app-step", chain))).toBe(false);
    // A model span that the agent did not make never reaches Future AGI.
    const foreign = app.agentSpan("LLM", false);
    expect(app.yields(app.appSpan("app-provider-request", foreign))).toBe(false);
    chain.end(); foreign.end();
  });

  test("only a local span that never recorded and has the sampled flag off is matched by trace id", () => {
    const app = unsampledTrace(), model = app.agentSpan("LLM"), { traceId } = model.spanContext();
    // The same trace read from incoming headers, as a remote span context.
    expect(app.yields(trace.wrapSpanContext({ traceId, spanId: "1".repeat(16), traceFlags: 0, isRemote: true }))).toBe(false);
    // A span context with the sampled flag on.
    expect(app.yields(trace.wrapSpanContext({ traceId, spanId: "2".repeat(16), traceFlags: 1 }))).toBe(false);
    // A span that recorded and has ended also reports that it is not recording.
    const finished = new BasicTracerProvider({ sampler: recordOnly }).getTracer("app").startSpan("app-finished", {}, app.requestContext);
    finished.end();
    expect(finished.isRecording()).toBe(false);
    expect(finished.spanContext().traceId).toBe(traceId);
    expect(app.yields(finished)).toBe(false);
    // The control: a span in the same trace that never recorded.
    expect(app.yields(trace.wrapSpanContext({ traceId, spanId: "3".repeat(16), traceFlags: 0 }))).toBe(true);
    model.end();
  });
});
