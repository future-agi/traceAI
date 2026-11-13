import type { Attributes } from "@opentelemetry/api";
import { BasicTracerProvider } from "@opentelemetry/sdk-trace-base";
import { privacyFilter } from "../export-privacy";
import { getRegisteredSecrets } from "../secrets";
import { FITracer } from "../trace/trace-config/FITracer";

// Some integrations also record the session id under their own attribute name,
// with the same value as session.id. On spans made by an FI tracer, each of
// those keys reaches the collector as set, like session.id, unless its value is
// a credential the exporter already knows. Other session-named attributes keep
// the credential name rule, and so does a span from any other tracer.

// [integration key, a token- or cookie-named attribute next to it]
const KEYS: Array<[string, string]> = [
  ["pi.session.id", "pi.session.token"],
  ["claude_agent.session.id", "claude_agent.session.token"],
  ["claude_agent.session_id", "claude_agent.session_token"],
  ["claude_agent.resume_session_id", "claude_agent.resume_session_token"],
  ["claude_agent.session.previous_id", "claude_agent.session.previous_token"],
  ["claude_agent.session.fork_from", "claude_agent.session.fork_cookie"],
  ["genkit:metadata:agent:sessionId", "genkit:metadata:agent:sessionToken"],
];

const idFor = (key: string, suffix: string) => `${key.replace(/[^A-Za-z0-9]+/g, "-")}-${suffix}`;

// A detached copy of a fresh span made by an FI tracer, as an integration's
// FITracer span reaches the exporter. It carries that span's context.
const fiProvider = new BasicTracerProvider();
function snapshot(attributes: Attributes, fiTracer = true): any {
  let spanContext: object = { traceId: "1".repeat(32), spanId: "2".repeat(16), traceFlags: 1 };
  if (fiTracer) {
    const span = new FITracer({ tracer: fiProvider.getTracer("integration") }).startSpan("integration");
    span.end();
    spanContext = span.spanContext();
  }
  return { _spanContext: spanContext,
    name: "integration", kind: 1, startTime: [1, 0], endTime: [2, 0], _duration: [1, 0], attributes,
    status: { code: 1 }, resource: { attributes: {} }, instrumentationScope: { name: "integration" }, events: [], links: [] };
}

const saved = { ...process.env };
beforeEach(() => {
  // No privacy flag may come from the environment.
  for (const key of Object.keys(process.env)) if (/^(FI_|OTEL_)/.test(key)) delete process.env[key];
});
afterEach(() => { process.env = { ...saved }; });

test.each(KEYS)("%s exports as set next to session.id while %s stays masked", (key, sibling) => {
  const id = idFor(key, "0001");
  const token = idFor(sibling, "value-0001");
  const clean = privacyFilter().span(snapshot({ "session.id": id, [key]: id, [sibling]: token }));
  expect(clean.attributes["session.id"]).toBe(id);
  expect(clean.attributes[key]).toBe(id);
  expect(clean.attributes[sibling]).toBe("<redacted>");
  // The id never becomes a known credential; the token-named value does.
  expect(getRegisteredSecrets()).not.toContain(id);
  expect(getRegisteredSecrets()).toContain(token);
  // A later span with the same id, and text that mentions it, are not scrubbed.
  const later = privacyFilter().span(snapshot({ "session.id": id, note: `follow-up for ${id}` }));
  expect(later.attributes["session.id"]).toBe(id);
  expect(later.attributes.note).toBe(`follow-up for ${id}`);
});

test.each(KEYS)("%s equal to a configured credential is still scrubbed", key => {
  const credential = idFor(key, "configured-credential");
  const clean = privacyFilter({}, [credential]).span(snapshot({ "session.id": credential, [key]: credential }));
  expect(clean.attributes["session.id"]).toBe("<redacted>");
  expect(clean.attributes[key]).toBe("<redacted>");
  expect(JSON.stringify(clean.attributes)).not.toContain(credential);
});

test.each(KEYS)("%s seen on a span with SDK data loss is not treated as a credential on later spans", key => {
  const id = idFor(key, "lossy-0001");
  const lossy = snapshot({ "session.id": id, [key]: id, note: "ordinary prose" });
  lossy.droppedAttributesCount = 1;
  privacyFilter().span(lossy);
  expect(getRegisteredSecrets()).not.toContain(id);
  const later = privacyFilter().span(snapshot({ "session.id": id, [key]: id }));
  expect(later.attributes["session.id"]).toBe(id);
  expect(later.attributes[key]).toBe(id);
});

test("a session id under an attribute name that is not listed keeps the credential name rule", () => {
  const id = "other-integration-session-0001";
  const clean = privacyFilter().span(snapshot({ "other_integration.session.id": id }));
  expect(clean.attributes["other_integration.session.id"]).toBe("<redacted>");
});

test.each(KEYS)("%s on a span from another tracer keeps the credential name rule", key => {
  const id = idFor(key, "forwarded-0001");
  const clean = privacyFilter().span(snapshot({ "session.id": id, [key]: id }, false));
  expect(clean.attributes["session.id"]).toBe("<redacted>");
  expect(clean.attributes[key]).toBe("<redacted>");
  expect(JSON.stringify(clean.attributes)).not.toContain(id);
});
