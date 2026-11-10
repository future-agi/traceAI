import { ROOT_CONTEXT, type Attributes, type Context } from "@opentelemetry/api";
import { resourceFromAttributes } from "@opentelemetry/resources";
import { BasicTracerProvider, InMemorySpanExporter, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { privacyFilter } from "../export-privacy";
import { ProjectType } from "../fi_types";
import { register } from "../otel";
import { getRegisteredSecrets } from "../secrets";
import { setMetadata, setSession, setUser } from "../trace/contextAttributes";
import { REDACTED_VALUE } from "../trace/trace-config/constants";
import { FITracer } from "../trace/trace-config/FITracer";

// Session, user and session name are how spans are grouped into Sessions and
// Users. They must reach the collector exactly as set, unless a value is a
// credential the exporter already knows.

// What the FI exporter hands to its transport, after the export boundary.
const delivered: any[][] = [];
jest.mock("@opentelemetry/exporter-trace-otlp-http", () => ({ OTLPTraceExporter: class {
  export(spans: any[], callback: Function) { delivered.push(spans); callback({ code: 0 }); }
  async shutdown() {} async forceFlush() {}
} }));

const saved = { ...process.env };
beforeEach(() => {
  delivered.length = 0;
  // No privacy flag may come from the environment.
  for (const key of Object.keys(process.env)) if (/^(FI_|OTEL_)/.test(key)) delete process.env[key];
});
afterEach(() => { process.env = { ...saved }; jest.restoreAllMocks(); });

const SESSION = "support-chat-session-0193";
const USER = "customer-account-5521";
const SESSION_NAME = "weekday-support-run";
const METADATA = { session_id: SESSION, user_id: USER, channel: "web" };
const routing = { endpoint: "http://127.0.0.1:1", headers: {}, projectName: "identity-project", projectType: ProjectType.OBSERVE, setGlobalTracerProvider: false };

function identity(base: Context = ROOT_CONTEXT): Context {
  return setMetadata(setUser(setSession(base, { sessionId: SESSION }), { userId: USER }), METADATA);
}

function snapshot(attributes: Attributes): any {
  return { _spanContext: { traceId: "1".repeat(32), spanId: "2".repeat(16), traceFlags: 1 },
    name: "application", kind: 2, startTime: [1, 0], endTime: [2, 0], _duration: [1, 0], attributes,
    status: { code: 1 }, resource: { attributes: {} }, instrumentationScope: { name: "application" }, events: [], links: [] };
}

test.each(["startSpan", "startActiveSpan", "setAttributes"])("with no privacy flags, session and user identity export as set (%s)", async route => {
  jest.spyOn(globalThis, "fetch").mockResolvedValue(new Response("{}"));
  const provider = register({ ...routing, sessionName: SESSION_NAME });
  try {
    const tracer = new FITracer({ tracer: provider.getTracer("identity") });
    if (route === "startSpan") tracer.startSpan("identity", {}, identity()).end();
    else if (route === "startActiveSpan") tracer.startActiveSpan("identity", {}, identity(), span => span.end());
    else {
      // An instrumentation that copies the identity onto the span itself.
      const span = tracer.startSpan("identity", {}, ROOT_CONTEXT);
      span.setAttributes({ "session.id": SESSION, "user.id": USER, metadata: JSON.stringify(METADATA) });
      span.end();
    }
    await provider.forceFlush();
    const spans = delivered.flat();
    expect(spans).toHaveLength(1);
    const [span] = spans;
    expect(span.attributes["session.id"]).toBe(SESSION);
    expect(span.attributes["user.id"]).toBe(USER);
    expect(JSON.parse(String(span.attributes.metadata))).toEqual(METADATA);
    expect(span.resource.attributes.session_name).toBe(SESSION_NAME);
    // Exporting an identity never turns it into a known credential.
    expect(getRegisteredSecrets()).not.toContain(SESSION);
  } finally { await provider.shutdown(); }
});

test("with inputs and outputs hidden, identity still exports while message content is masked", async () => {
  jest.spyOn(globalThis, "fetch").mockResolvedValue(new Response("{}"));
  const traceConfig = { hideInputs: true, hideOutputs: true };
  const provider = register({ ...routing, sessionName: SESSION_NAME, traceConfig });
  try {
    const tracer = new FITracer({ tracer: provider.getTracer("identity"), traceConfig });
    const prompt = "private prompt about an order", answer = "private answer about a refund";
    tracer.startActiveSpan("identity", { attributes: {
      "input.value": prompt, "output.value": answer,
      "gen_ai.input.messages": JSON.stringify([{ role: "user", content: prompt }]),
      "gen_ai.output.messages": JSON.stringify([{ role: "assistant", content: answer }]),
    } }, identity(), span => span.end());
    await provider.forceFlush();
    const spans = delivered.flat();
    expect(spans).toHaveLength(1);
    const [span] = spans;
    expect(span.attributes["session.id"]).toBe(SESSION);
    expect(span.attributes["user.id"]).toBe(USER);
    expect(JSON.parse(String(span.attributes.metadata))).toEqual(METADATA);
    expect(span.resource.attributes.session_name).toBe(SESSION_NAME);
    expect(span.attributes["input.value"]).toBe(REDACTED_VALUE);
    expect(span.attributes["output.value"]).toBe(REDACTED_VALUE);
    const exported = JSON.stringify(span.attributes);
    expect(exported).not.toContain("private prompt");
    expect(exported).not.toContain("private answer");
  } finally { await provider.shutdown(); }
});

test.each(["session.id", "user.id", "metadata.session_id", "session_name"])("a %s equal to a configured credential is still scrubbed", async field => {
  const credential = `configured-credential-for-${field}`;
  const exporter = new InMemorySpanExporter();
  const provider = new BasicTracerProvider({
    resource: resourceFromAttributes({ session_name: field === "session_name" ? credential : SESSION_NAME }),
    spanProcessors: [new SimpleSpanProcessor(exporter)],
  });
  try {
    const sessionId = field === "session.id" ? credential : SESSION;
    const userId = field === "user.id" ? credential : USER;
    const metadata = { ...METADATA, session_id: field === "metadata.session_id" ? credential : SESSION };
    const tracer = new FITracer({ tracer: provider.getTracer("identity") });
    tracer.startActiveSpan("identity", {}, setMetadata(setUser(setSession(ROOT_CONTEXT, { sessionId }), { userId }), metadata), span => span.end());
    const clean = privacyFilter({}, [credential]).span(exporter.getFinishedSpans()[0]);
    expect(JSON.stringify({ attributes: clean.attributes, resource: clean.resource.attributes })).not.toContain(credential);
    if (field === "session.id") expect(clean.attributes["session.id"]).toBe("<redacted>");
    if (field === "user.id") expect(clean.attributes["user.id"]).toBe("<redacted>");
    if (field === "metadata.session_id") expect(JSON.parse(String(clean.attributes.metadata)).session_id).toBe("<redacted>");
    if (field === "session_name") expect(clean.resource.attributes.session_name).toBe("<redacted>");
  } finally { await provider.shutdown(); }
});

test("only the top-level session and user fields of metadata keep their values", () => {
  const metadata = JSON.stringify({ session_id: SESSION, user_id: USER, session_token: "metadata-session-token-value", nested: { session_id: "nested-session-value" } });
  const clean = privacyFilter().span(snapshot({ metadata, "session.id": SESSION, "session.cookie": "session-cookie-value" }));
  expect(JSON.parse(String(clean.attributes.metadata))).toEqual({ session_id: SESSION, user_id: USER, session_token: "<redacted>", nested: { session_id: "<redacted>" } });
  expect(clean.attributes["session.id"]).toBe(SESSION);
  expect(clean.attributes["session.cookie"]).toBe("<redacted>");
});

test("a session seen on a span with SDK data loss is not treated as a credential on later spans", () => {
  const lossySession = "lossy-span-session-7731", lossyUser = "lossy-span-user-7731";
  const lossy = snapshot({ "session.id": lossySession, "user.id": lossyUser, metadata: JSON.stringify({ session_id: lossySession }), note: "ordinary prose" });
  lossy.droppedAttributesCount = 1;
  privacyFilter().span(lossy);
  expect(getRegisteredSecrets()).not.toContain(lossySession);
  const later = privacyFilter().span(snapshot({ "session.id": lossySession, "user.id": lossyUser, note: `follow-up for ${lossySession}` }));
  expect(later.attributes["session.id"]).toBe(lossySession);
  expect(later.attributes["user.id"]).toBe(lossyUser);
  expect(later.attributes.note).toBe(`follow-up for ${lossySession}`);
});
