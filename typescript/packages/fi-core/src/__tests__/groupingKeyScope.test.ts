import type { Attributes } from "@opentelemetry/api";
import { resourceFromAttributes } from "@opentelemetry/resources";
import { BasicTracerProvider, InMemorySpanExporter, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { privacyFilter } from "../export-privacy";
import * as publicApi from "../index";
import { getRegisteredSecrets } from "../secrets";
import { FITracer } from "../trace/trace-config/FITracer";

// Session, user and session-name grouping keys export as set only on spans made
// by an FI tracer. A span from any other tracer, such as an application span
// forwarded to Future AGI, keeps the credential name rule for the same keys.

const saved = { ...process.env };
beforeEach(() => {
  // No privacy flag may come from the environment.
  for (const key of Object.keys(process.env)) if (/^(FI_|OTEL_)/.test(key)) delete process.env[key];
});
afterEach(() => { process.env = { ...saved }; });

function identity(suffix: string): Attributes {
  return {
    "session.id": `scope-session-${suffix}`,
    "user.id": `scope-user-${suffix}`,
    "pi.session.id": `scope-session-${suffix}`,
    metadata: JSON.stringify({ session_id: `scope-session-${suffix}`, user_id: `scope-user-${suffix}`, channel: "web" }),
    "control.label": "scope-public-control",
  };
}

async function finished(fiTracer: boolean, suffix: string) {
  const exporter = new InMemorySpanExporter();
  const provider = new BasicTracerProvider({
    resource: resourceFromAttributes({ session_name: `scope-name-${suffix}` }),
    spanProcessors: [new SimpleSpanProcessor(exporter)],
  });
  try {
    const native = provider.getTracer("grouping-scope");
    const span = (fiTracer ? new FITracer({ tracer: native }) : native).startSpan("grouping");
    span.setAttributes(identity(suffix));
    span.end();
    return exporter.getFinishedSpans()[0];
  } finally { await provider.shutdown(); }
}

function snapshot(attributes: Attributes): any {
  return { _spanContext: { traceId: "1".repeat(32), spanId: "2".repeat(16), traceFlags: 1 },
    name: "application", kind: 2, startTime: [1, 0], endTime: [2, 0], _duration: [1, 0], attributes,
    status: { code: 1 }, resource: { attributes: {} }, instrumentationScope: { name: "application" }, events: [], links: [] };
}

test("a span made by an FI tracer exports its grouping keys as set, also on re-export", async () => {
  const span = await finished(true, "fi");
  const first = privacyFilter().span(span);
  for (const clean of [first, privacyFilter().span(first)]) {
    expect(clean.attributes["session.id"]).toBe("scope-session-fi");
    expect(clean.attributes["user.id"]).toBe("scope-user-fi");
    expect(clean.attributes["pi.session.id"]).toBe("scope-session-fi");
    expect(JSON.parse(String(clean.attributes.metadata))).toEqual({ session_id: "scope-session-fi", user_id: "scope-user-fi", channel: "web" });
    expect(clean.resource.attributes.session_name).toBe("scope-name-fi");
    expect(clean.attributes["control.label"]).toBe("scope-public-control");
  }
  expect(getRegisteredSecrets()).not.toContain("scope-session-fi");
});

test("a span from another tracer keeps the credential name rule for session keys", async () => {
  const span = await finished(false, "other");
  const before = JSON.stringify(span.attributes);
  const first = privacyFilter().span(span);
  for (const clean of [first, privacyFilter().span(first)]) {
    expect(clean.attributes["session.id"]).toBe("<redacted>");
    expect(clean.attributes["pi.session.id"]).toBe("<redacted>");
    const metadata = JSON.parse(String(clean.attributes.metadata));
    expect(metadata.session_id).toBe("<redacted>");
    expect(metadata.channel).toBe("web");
    expect(clean.resource.attributes.session_name).toBe("<redacted>");
    expect(clean.attributes["control.label"]).toBe("scope-public-control");
    expect(JSON.stringify({ attributes: clean.attributes, resource: clean.resource.attributes })).not.toContain("scope-session-other");
  }
  // The application's own span is unchanged.
  expect(JSON.stringify(span.attributes)).toBe(before);
});

test("a copied span that carries no FI span context keeps the credential name rule", () => {
  const clean = privacyFilter().span(snapshot({ "session.id": "scope-copied-session", "control.label": "scope-public-control" }));
  expect(clean.attributes["session.id"]).toBe("<redacted>");
  expect(clean.attributes["control.label"]).toBe("scope-public-control");
});

test("a session id seen first on a span from another tracer is not registered as a secret, so an FI span with the same id keeps it", async () => {
  const foreign = privacyFilter().span(await finished(false, "shared"));
  expect(foreign.attributes["session.id"]).toBe("<redacted>");
  expect(JSON.parse(String(foreign.attributes.metadata)).session_id).toBe("<redacted>");
  for (const value of ["scope-session-shared", "scope-user-shared"]) expect(getRegisteredSecrets()).not.toContain(value);
  const clean = privacyFilter().span(await finished(true, "shared"));
  expect(clean.attributes["session.id"]).toBe("scope-session-shared");
  expect(clean.attributes["user.id"]).toBe("scope-user-shared");
  expect(clean.attributes["pi.session.id"]).toBe("scope-session-shared");
  expect(JSON.parse(String(clean.attributes.metadata))).toEqual({ session_id: "scope-session-shared", user_id: "scope-user-shared", channel: "web" });
});

test("a span an integration converts and marks with the public rememberFITracerSpan exports its grouping keys as set", () => {
  // Integrations such as Genkit export copies of their framework's spans, keyed by the native span context.
  const span = snapshot({ "session.id": "scope-converted-session", "user.id": "scope-converted-user", "control.label": "scope-public-control" });
  const mark = (publicApi as unknown as Record<string, unknown>).rememberFITracerSpan;
  expect(typeof mark).toBe("function");
  (mark as (context: object) => void)(span._spanContext);
  const first = privacyFilter().span(span);
  for (const clean of [first, privacyFilter().span(first)]) {
    expect(clean.attributes["session.id"]).toBe("scope-converted-session");
    expect(clean.attributes["user.id"]).toBe("scope-converted-user");
    expect(clean.attributes["control.label"]).toBe("scope-public-control");
  }
  expect(getRegisteredSecrets()).not.toContain("scope-converted-session");
});
