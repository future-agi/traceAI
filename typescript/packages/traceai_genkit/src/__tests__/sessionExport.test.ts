import { ROOT_CONTEXT } from "@opentelemetry/api";
import { BasicTracerProvider, InMemorySpanExporter, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { resourceFromAttributes } from "@opentelemetry/resources";
import { getRegisteredSecrets, privacyFilter, setSession, setUser } from "@traceai/fi-core";
import { enable, type GenkitAutoSpanProcessor } from "../auto";
import { FIGenkitSpanProcessor } from "../index";
import { flowAttributes, v1Span } from "./fixtures";

// Genkit spans reach Future AGI as converted copies. The Future AGI export
// boundary treats those copies like FI tracer spans: the session and user ids
// set with the fi-core context helpers export as set, for Sessions grouping.

const saved = { ...process.env };
beforeEach(() => {
  // No privacy flag may come from the environment.
  for (const key of Object.keys(process.env)) if (/^(FI_|OTEL_)/.test(key)) delete process.env[key];
});
afterEach(() => { process.env = { ...saved }; });

it("the processor's converted spans keep session and user ids at the Future AGI export boundary", async () => {
  const exporter = new InMemorySpanExporter();
  const provider = new BasicTracerProvider({
    resource: resourceFromAttributes({ project_name: "unit-project", project_type: "observe" }),
    spanProcessors: [new SimpleSpanProcessor(exporter)],
  });
  try {
    const processor = new FIGenkitSpanProcessor({ tracerProvider: provider });
    const span = v1Span({ name: "qaFlow", attributes: flowAttributes(), spanId: "7777777777777777" });
    processor.onStart(span, setUser(setSession(ROOT_CONTEXT, { sessionId: "genkit-session-0042" }), { userId: "genkit-user-0007" }));
    processor.onEnd(span);
    await processor.forceFlush();
    const clean = privacyFilter().span(exporter.getFinishedSpans()[0]);
    expect(clean.attributes["session.id"]).toBe("genkit-session-0042");
    expect(clean.attributes["user.id"]).toBe("genkit-user-0007");
    expect(getRegisteredSecrets()).not.toContain("genkit-session-0042");
  } finally { await provider.shutdown(); }
});

it("the processor marks only Genkit spans: an application span's session id keeps the credential name rule", async () => {
  // Genkit's manual setup attaches the processor to the provider the application uses, so
  // the application's own spans reach it too. Only Genkit spans are first-party copies.
  const exporter = new InMemorySpanExporter();
  const provider = new BasicTracerProvider({
    resource: resourceFromAttributes({ project_name: "unit-project", project_type: "observe" }),
    spanProcessors: [new SimpleSpanProcessor(exporter)],
  });
  try {
    const processor = new FIGenkitSpanProcessor({ tracerProvider: provider });
    const span = { ...v1Span({ name: "application work", attributes: {}, spanId: "8888888888888888" }), instrumentationLibrary: { name: "my-application", version: "1" } };
    processor.onStart(span, setSession(ROOT_CONTEXT, { sessionId: "application-session-0099" }));
    processor.onEnd(span);
    await processor.forceFlush();
    const finished = exporter.getFinishedSpans();
    expect(finished).toHaveLength(1);
    const clean = privacyFilter().span(finished[0]);
    expect(clean.attributes["session.id"]).not.toBe("application-session-0099");
  } finally { await provider.shutdown(); }
});

it("the zero-code mapping's converted spans keep session and user ids at the Future AGI export boundary", () => {
  const ended: object[] = [];
  const target: GenkitAutoSpanProcessor = { onStart() { /* The export branch's own start hook. */ }, onEnd(span) { ended.push(span); } };
  expect(enable({ spanProcessor: target }).status).toBe("instrumented");
  const span = v1Span({ name: "qaFlow", attributes: flowAttributes(), spanId: "8888888888888888" });
  target.onStart(span, setUser(setSession(ROOT_CONTEXT, { sessionId: "genkit-auto-session-0042" }), { userId: "genkit-auto-user-0007" }));
  target.onEnd(span);
  expect(ended).toHaveLength(1);
  const clean = privacyFilter().span(ended[0] as Parameters<ReturnType<typeof privacyFilter>["span"]>[0]);
  expect(clean.attributes["session.id"]).toBe("genkit-auto-session-0042");
  expect(clean.attributes["user.id"]).toBe("genkit-auto-user-0007");
});
