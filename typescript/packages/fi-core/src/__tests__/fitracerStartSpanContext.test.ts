import { Attributes, context, ROOT_CONTEXT } from "@opentelemetry/api";
import { AsyncLocalStorageContextManager } from "@opentelemetry/context-async-hooks";
import { BasicTracerProvider, InMemorySpanExporter, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { FITracer } from "../trace/trace-config/FITracer";
import { setAttributes, setSession, setUser } from "../trace/contextAttributes";
import { privacyFilter } from "../export-privacy";
import { REDACTED_VALUE } from "../trace/trace-config/constants";

const env = { ...process.env };
beforeEach(() => {
  for (const key of Object.keys(process.env)) if (/^(FI_|OTEL_)/.test(key)) delete process.env[key];
  context.setGlobalContextManager(new AsyncLocalStorageContextManager().enable());
});
afterEach(() => { context.disable(); process.env = { ...env }; });

test.each([false, true])("startSpan merges %s explicit context before span attributes, like startActiveSpan", async explicitContext => {
  const exporter = new InMemorySpanExporter();
  const seen: Attributes[] = [];
  const provider = new BasicTracerProvider({
    sampler: { shouldSample: (_parent, _id, _name, _kind, attrs) => { seen.push(attrs); return { decision: 2 }; }, toString: () => "record" },
    spanProcessors: [new SimpleSpanProcessor(exporter)],
  });
  try {
    const tracer = new FITracer({ tracer: provider.getTracer("context-test") });
    const ctx = setAttributes(setUser(setSession(ROOT_CONTEXT, { sessionId: "conversation-123" }), { userId: "user-123" }), { inherited: "context", override: "context" });
    const opts = { attributes: { override: "span", extra: false } };
    context.with(ctx, () => {
      tracer.startSpan("ordinary", opts, explicitContext ? ctx : undefined).end();
      tracer.startActiveSpan("active", opts, ctx, span => span.end());
    });
    expect(seen[0]).toEqual(seen[1]);
    expect(exporter.getFinishedSpans()[0].attributes).toEqual({ "session.id": "conversation-123", "user.id": "user-123", inherited: "context", override: "span", extra: false });
    const exported = privacyFilter().span(exporter.getFinishedSpans()[0]);
    expect(exported.attributes["session.id"]).toBe("conversation-123");
    expect(opts).toEqual({ attributes: { override: "span", extra: false } });
  } finally { await provider.shutdown(); }
});

test("explicit ROOT_CONTEXT does not inherit active context", async () => {
  const exporter = new InMemorySpanExporter();
  const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
  try {
    const tracer = new FITracer({ tracer: provider.getTracer("context-test") });
    context.with(setUser(ROOT_CONTEXT, { userId: "outer" }), () => tracer.startSpan("isolated", {}, ROOT_CONTEXT).end());
    expect(exporter.getFinishedSpans()[0].attributes["user.id"]).toBeUndefined();
  } finally { await provider.shutdown(); }
});

test("inherited content crosses the same creation-time privacy boundary", async () => {
  const exporter = new InMemorySpanExporter();
  const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
  try {
    const tracer = new FITracer({ tracer: provider.getTracer("context-test"), traceConfig: { hideInputs: true } });
    tracer.startSpan("private", {}, setAttributes(ROOT_CONTEXT, { "input.value": "private text", "gen_ai.agent.name": "support-agent" })).end();
    expect(exporter.getFinishedSpans()[0].attributes).toEqual({ "input.value": REDACTED_VALUE, "gen_ai.agent.name": "support-agent" });
  } finally { await provider.shutdown(); }
});
