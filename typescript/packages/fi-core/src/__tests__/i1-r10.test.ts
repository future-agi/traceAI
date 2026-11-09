import type { Attributes } from "@opentelemetry/api";
import { privacyFilter, rememberMediaAdmission, rememberProjectedAttributeDrops } from "../export-privacy";
import { indexPrivacyHints, mediaReplacement, scrubValue } from "../privacy-runtime";
import { projectAttributes } from "../attribute-projection";

// Literal controls. Runtime NOT_RUN. No existing test or application is edited.
const savedEnv = { ...process.env }, registryKey = Symbol.for("traceai.secrets");
let savedRegistry: PropertyDescriptor | undefined;
beforeEach(() => {
  for (const key of Object.keys(process.env)) if (/^(FI_|OTEL_)/.test(key)) delete process.env[key];
  savedRegistry = Object.getOwnPropertyDescriptor(globalThis, registryKey);
  Reflect.deleteProperty(globalThis, registryKey);
});
afterEach(() => {
  jest.restoreAllMocks(); process.env = { ...savedEnv };
  Reflect.deleteProperty(globalThis, registryKey);
  if (savedRegistry) Object.defineProperty(globalThis, registryKey, savedRegistry);
});
const HOST = "traceai.capture.host_data_unknown", COUNT = "traceai.capture.dropped_content_count";
const UNKNOWN = "<unknown: host-truncated>";
function fixture(attributes: Attributes = {}): any {
  const ctx = { traceId: "3".repeat(32), spanId: "4".repeat(16), traceFlags: 1 };
  return { _spanContext: ctx, spanContext: () => ctx, name: "r10 name", attributes,
    kind: 2, startTime: [1, 0], endTime: [2, 0], _duration: [1, 0], ended: true,
    droppedAttributesCount: 0, droppedEventsCount: 0, droppedLinksCount: 0,
    status: { code: 2 }, resource: { attributes: {} }, instrumentationScope: { name: "r10 scope" }, events: [], links: [] };
}
const g4 = ["gen_ai.audio.bytes", "gen_ai.usage.input_tokens", "gen_ai.usage.output_tokens", "gen_ai.usage.total_tokens",
  "gen_ai.usage.cache_read_tokens", "gen_ai.usage.input_tokens.audio", "gen_ai.usage.output_tokens.audio", "gen_ai.usage.output_tokens.reasoning"];

// A: FI-authored scalar-role widening is a REGRESSION, not host loss.
test.each([0, 9, "0", "9"])("R10 A pre-r7 G4 count %s keeps its type", value => {
  for (const key of g4) {
    expect(mediaReplacement(value, key)).toBeUndefined();
    expect(scrubValue(value, [], key)).toBe(value);
    expect(projectAttributes({ [key]: value }, 16).attributes).toEqual({ [key]: value });
    expect(privacyFilter().admissionAttributes({ [key]: value })).toEqual({ [key]: value });
    expect(privacyFilter().span(fixture({ [key]: value })).attributes).toEqual({ [key]: value });
  }
});
// A confinement: core/detail string roles retain r9 behavior, including literal undefined.
// These are FI-policy REGRESSION controls, not host-side loss witnesses.
test.each(["undefined", "01", "16777216", "9007199254740991", "ordinary detail prose", true, -1, 0.5])(
  "R10 A non-byte G4 input %s follows the unchanged r9 role", value => {
    for (const key of g4.slice(1)) {
      expect(mediaReplacement(value, key)).toBeUndefined();
      expect(scrubValue(value, [], key)).toBe(value);
      expect(projectAttributes({ [key]: value }, 16).attributes).toEqual({ [key]: value });
      expect(privacyFilter().admissionAttributes({ [key]: value })).toEqual({ [key]: value });
      expect(privacyFilter().span(fixture({ [key]: value })).attributes).toEqual({ [key]: value });
    }
  });
test.each([
  ["1234", "<omitted: 4 base64 chars>"], ["16777216", "<omitted: 8 base64 chars>"],
  ["9007199254740991", "<omitted: 16 base64 chars>"], ["AQIDBAUGBwgJCgsM", "<omitted: 16 base64 chars>"],
  ["private transcript!", "<omitted: media>"], ["01", "<omitted: 2 base64 chars>"],
  ["9\n", "<omitted: 2 base64 chars>"],
])("R10 A payload precedence for byte counter %s", (value, expected) => {
  for (const key of ["gen_ai.audio.bytes", "elevenlabs.audio.bytes"]) {
    expect(mediaReplacement(value, key)).toBe(expected);
    expect(scrubValue(value, [], key)).toBe(expected);
    expect(projectAttributes({ [key]: value }, 16).attributes).toEqual({ [key]: expected });
    expect(privacyFilter().admissionAttributes({ [key]: value })).toEqual({ [key]: expected });
    expect(privacyFilter().span(fixture({ [key]: value })).attributes).toEqual({ [key]: expected });
  }
});
test.each([
  ["0", "<omitted: 1 base64 chars>"], ["9", "<omitted: 1 base64 chars>"],
  ["10", "<omitted: 2 base64 chars>"], ["123", "<omitted: 3 base64 chars>"],
])("R10 A decimal string %s keeps only the gen_ai byte role", (value, elevenLabs) => {
  for (const [key, expected] of [["gen_ai.audio.bytes", value], ["elevenlabs.audio.bytes", elevenLabs]]) {
    expect(mediaReplacement(value, key)).toBe(expected === value ? undefined : expected);
    expect(scrubValue(value, [], key)).toBe(expected);
    expect(projectAttributes({ [key]: value }, 16).attributes).toEqual({ [key]: expected });
    expect(privacyFilter().admissionAttributes({ [key]: value })).toEqual({ [key]: expected });
    expect(privacyFilter().span(fixture({ [key]: value })).attributes).toEqual({ [key]: expected });
  }
});
test("R10 A numeric byte validation and independent precedence stay unchanged", () => {
  for (const key of ["gen_ai.audio.bytes", "elevenlabs.audio.bytes"]) {
    for (const value of [0, 9, 9007199254740991]) expect(scrubValue(value, [], key)).toBe(value);
    for (const value of [true, false, -1, 0.5, NaN, Infinity, 9007199254740992, [9]]) expect(scrubValue(value, [], key)).toBe("<omitted: media>");
    expect(scrubValue("16777216", ["16777216"], key)).toBe("<redacted>");
    expect(scrubValue(16777216, ["16777216"], key)).toBe("<redacted>");
    expect(scrubValue("9", [], `secret.${key}`)).toBe("<redacted>");
    expect(mediaReplacement("9", key, indexPrivacyHints([["type", "input_audio"]]))).toBe("<omitted: 1 base64 chars>");
    expect(scrubValue({ audio: { data: { [key]: "9" } } })).toEqual({ audio: { data: { [key]: "<omitted: 1 base64 chars>" } } });
  }
});

// B r2: class (b), fixture-host resource/scope values at/over the host limit.
test.each(["resource", "rawResource", "scope", "legacyScope", "extraResource"])("R10 B long %s alone at limit 16 triggers full Q8b", surface => {
  const raw = fixture({ note: "short prose", "session.id": "session prose", "gen_ai.usage.total_tokens": 7 }), long = "a complete resource value";
  raw._attributeValueLengthLimit = 16;
  raw.resource.attributes.note = "resource prose";
  raw.instrumentationScope.attributes = { note: "scope prose" };
  raw.status.message = "status prose";
  raw.events = [{ name: "event", time: [1, 0], attributes: { note: "event prose" } }];
  raw.links = [{ context: raw.spanContext(), attributes: { note: "link prose" } }];
  let extra: Attributes = {};
  if (surface === "resource") raw.resource.attributes.note = long;
  if (surface === "rawResource") raw.resource = { _rawAttributes: [["note", long]] };
  if (surface === "scope") raw.instrumentationScope.attributes = { note: long };
  if (surface === "legacyScope") { raw.instrumentationLibrary = { name: "legacy", attributes: { note: long } }; delete raw.instrumentationScope; }
  if (surface === "extraResource") extra = { note: long };
  const before = JSON.stringify(raw), filter = privacyFilter(), first = filter.span(raw, [], extra);
  for (const clean of [first, filter.span(first)]) {
    expect(clean.attributes).toEqual({ note: UNKNOWN, "session.id": "<redacted>", "gen_ai.usage.total_tokens": 7, [HOST]: true });
    expect(clean.name).toBe(UNKNOWN); expect(clean.status).toEqual({ code: 2, message: UNKNOWN });
    expect(clean.resource.attributes).toEqual({ note: UNKNOWN });
    expect(clean.instrumentationScope).toEqual({ name: UNKNOWN, version: UNKNOWN, schemaUrl: UNKNOWN, attributes: { note: UNKNOWN } });
    expect(clean.events).toHaveLength(1); expect(clean.events[0].name).toBe(UNKNOWN);
    expect(clean.events[0].attributes).toEqual({ note: UNKNOWN });
    expect(clean.links).toHaveLength(1); expect(clean.links[0].attributes).toEqual({ note: UNKNOWN });
    expect(clean.spanContext()).toEqual(raw.spanContext()); expect(clean.droppedAttributesCount).toBe(0);
  }
  expect(JSON.stringify(raw)).toBe(before);
});

// B positive: class (b), exact-length value on a host-limited attribute surface.
test.each(["span", "event", "link"])("R10 B %s value at host limit 16 applies full Q8b", surface => {
  const raw = fixture({ note: "short prose", "session.id": "session prose", "gen_ai.usage.total_tokens": 7 });
  raw._attributeValueLengthLimit = 16;
  raw.resource.attributes.note = "resource prose";
  raw.instrumentationScope.attributes = { note: "scope prose" };
  raw.status.message = "status prose";
  raw.events = [{ name: "event", time: [1, 0], attributes: { note: "event prose" } }];
  raw.links = [{ context: raw.spanContext(), attributes: { note: "link prose" } }];
  const target = surface === "span" ? raw : surface === "event" ? raw.events[0] : raw.links[0];
  target.attributes.sentinel = "abcdefghijklmnop";
  const before = JSON.stringify(raw), clean = privacyFilter().span(raw);
  expect(clean.attributes.note).toBe(UNKNOWN); expect(clean.attributes[HOST]).toBe(true);
  expect(clean.attributes["session.id"]).toBe("<redacted>"); expect(clean.attributes["gen_ai.usage.total_tokens"]).toBe(7);
  expect(clean.name).toBe(UNKNOWN); expect(clean.status.message).toBe(UNKNOWN);
  expect(clean.resource.attributes.note).toBe(UNKNOWN); expect((clean.instrumentationScope as any).attributes.note).toBe(UNKNOWN);
  expect(clean.events[0].name).toBe(UNKNOWN); expect(clean.events[0].attributes!.note).toBe(UNKNOWN);
  expect(clean.links[0].attributes!.note).toBe(UNKNOWN); expect(JSON.stringify(raw)).toBe(before);
});

// B positive: class (a) host counts, or explicit host markers, still count off-span.
test.each(["resource", "scope"].flatMap(surface => ["count", "suffix", "flag"].map(evidence => ({ surface, evidence }))))(
  "R10 B $surface explicit $evidence still withholds every content surface", ({ surface, evidence }) => {
    const raw = fixture({ note: "short prose" }); raw._attributeValueLengthLimit = 16;
    const target = surface === "resource" ? raw.resource : raw.instrumentationScope;
    target.attributes = { note: "ordinary prose" };
    if (evidence === "count") target.droppedAttributesCount = 1;
    if (evidence === "suffix") target.attributes.note = "tiny [truncated]";
    if (evidence === "flag") target.attributes[HOST] = true;
    expect(privacyFilter().span(raw).attributes).toEqual({ note: UNKNOWN, [HOST]: true });
  });

// B r2: class (b) by construction, NOT a claim about SDK default resource bytes.
// The six inherited finite-limit failures remain WAITING_ON_DIAGNOSTIC.
test.each([16, 24])("R10 B explicit host resource sentinel at %s overrides complete FI text", limit => {
  const raw = fixture({ "output.value": "ordinary answer", "output.mime_type": "text/plain", "session.id": "session prose", "gen_ai.usage.input_tokens": 7, [COUNT]: 123 });
  raw._attributeValueLengthLimit = limit;
  raw.resource.attributes["service.name"] = "r".repeat(limit);
  rememberMediaAdmission(raw, raw.attributes);
  raw.droppedAttributesCount = 123; rememberProjectedAttributeDrops(raw, 123);
  const before = JSON.stringify(raw), filter = privacyFilter(), first = filter.span(raw);
  for (const clean of [first, filter.span(first)]) {
    expect(clean.attributes).toEqual({ "output.value": UNKNOWN, "output.mime_type": UNKNOWN, "session.id": "<redacted>", "gen_ai.usage.input_tokens": 7, [COUNT]: 123, [HOST]: true });
    expect(clean.resource.attributes).toEqual({ "service.name": UNKNOWN }); expect(clean.name).toBe(UNKNOWN);
    expect(clean.instrumentationScope).toEqual({ name: UNKNOWN, version: UNKNOWN, schemaUrl: UNKNOWN, attributes: {} });
    expect(clean.status).toEqual({ code: 2 }); expect(clean.events).toEqual([]); expect(clean.links).toEqual([]);
    expect(clean.droppedAttributesCount).toBe(123); expect(clean.spanContext()).toEqual(raw.spanContext());
  }
  expect(JSON.stringify(raw)).toBe(before);
});

test("R10 B FI-created omission at limit 16 retains exact-map provenance", () => {
  // FI-generated marker length is REGRESSION evidence; a later native value at 16 is class (b).
  const raw = fixture({ "output.value": "ordinary answer" }); raw._attributeValueLengthLimit = 16;
  raw.droppedAttributesCount = 1; rememberProjectedAttributeDrops(raw, 1);
  const filter = privacyFilter(), first = filter.span(raw);
  expect(first.attributes).toEqual({ "output.value": "<omitted: media>" });
  expect(filter.span(first).attributes).toEqual(first.attributes);
  first.attributes["output.value"] = "abcdefghijklmnop";
  expect(filter.span(first).attributes).toEqual({ "output.value": UNKNOWN, [HOST]: true });
  const admitted = fixture({ "output.value": "ordinary answer", "output.mime_type": "text/plain" });
  rememberMediaAdmission(admitted, admitted.attributes); admitted.droppedAttributesCount = 1;
  // Class (a) real host drop cannot be defeated by complete MIME evidence.
  expect(filter.span(admitted).attributes).toEqual({ "output.value": UNKNOWN, "output.mime_type": UNKNOWN, [HOST]: true });
});

// C: class (b) host limit 8; content is withheld before marker/secret discovery.
test("R10 C known-loss payloads are never marker or secret probes", () => {
  const chunk = "q".repeat(4096), raw = fixture({ ...Object.fromEntries(Array.from({ length: 512 }, (_, i) => [`note${i}`, chunk])), "session.id": chunk });
  raw._attributeValueLengthLimit = 8;
  raw.status.message = chunk; raw.resource.attributes.note = chunk;
  raw.instrumentationScope.attributes = { note: chunk };
  raw.events = [{ name: "event", time: [1, 0], attributes: { "exception.message": chunk, "exception.stacktrace": chunk } }];
  raw.links = [{ context: raw.spanContext(), attributes: { note: chunk } }];
  const filter = privacyFilter({}, Array.from({ length: 1024 }, (_, i) => `s${i.toString(16).padStart(7, "0")}-synthetic-r10-secret`));
  const before = JSON.stringify(raw), includes = String.prototype.includes, test = RegExp.prototype.test;
  let probes = 0;
  const stringSpy = jest.spyOn(String.prototype, "includes").mockImplementation(function (this: string, search: string, position?: number) {
    if (String(this) === chunk) probes++;
    return includes.call(this, search, position);
  });
  const regexSpy = jest.spyOn(RegExp.prototype, "test").mockImplementation(function (this: RegExp, value: string) {
    if (value === chunk) probes++;
    return test.call(this, value);
  });
  let clean: any;
  try { clean = filter.span(raw); } finally { stringSpy.mockRestore(); regexSpy.mockRestore(); }
  expect(probes).toBe(0);
  expect(clean.attributes.note0).toBe(UNKNOWN); expect(clean.attributes.note511).toBe(UNKNOWN);
  expect(clean.attributes["session.id"]).toBe("<redacted>"); expect(clean.attributes[HOST]).toBe(true);
  expect(clean.status.message).toBe(UNKNOWN); expect(clean.resource.attributes.note).toBe(UNKNOWN);
  expect(clean.instrumentationScope.attributes.note).toBe(UNKNOWN);
  expect(clean.events[0].attributes).toEqual({ "exception.message": UNKNOWN, "exception.stacktrace": UNKNOWN });
  expect(clean.links[0].attributes.note).toBe(UNKNOWN); expect(JSON.stringify(raw)).toBe(before);
});

test("R10 C loss suffix and skipped-source reservations still share one allowance", () => {
  // Explicit host suffix; FI budget refusal must not invent a host drop count.
  const chunk = "ordinary! ".repeat(10000), raw = fixture({ note: chunk });
  raw.events = Array.from({ length: 32 }, () => ({ name: "event", attributes: { note: chunk } }));
  raw.links = [{ context: raw.spanContext(), attributes: { note: "tiny [truncated]" } }];
  const filter = privacyFilter({}, ["absent-r10-synthetic-secret"]), test = RegExp.prototype.test, includes = String.prototype.includes;
  let chars = 0;
  const charge = (value: string) => { if (value === chunk) { chars += value.length; if (chars > 4194304) throw new Error("R10 probe overrun"); } };
  const regexSpy = jest.spyOn(RegExp.prototype, "test").mockImplementation(function (this: RegExp, value: string) { charge(value); return test.call(this, value); });
  const stringSpy = jest.spyOn(String.prototype, "includes").mockImplementation(function (this: string, search: string, position?: number) { charge(String(this)); return includes.call(this, search, position); });
  let clean: any;
  try { clean = filter.span(raw); } finally { regexSpy.mockRestore(); stringSpy.mockRestore(); }
  expect(chars).toBeGreaterThan(0); expect(chars).toBeLessThanOrEqual(4194304);
  expect(clean.attributes).toEqual({ [HOST]: true }); expect(clean.events).toEqual([]); expect(clean.links).toEqual([]);
  expect(clean.droppedAttributesCount).toBe(0); expect(raw.attributes.note).toBe(chunk);
});

test("R10 C no-loss discovery and typed-secret precedence remain active", () => {
  // No host loss: credential discovery must still sanitize a later echo.
  const secret = "synthetic-r10-discovery-password", raw = fixture({ password: secret, echo: secret, note: "ordinary prose" });
  expect(privacyFilter().span(raw).attributes).toEqual({ password: "<redacted>", echo: "<redacted>", note: "ordinary prose" });
  // Class (a) host count: NON_CONTENT is not an exact registered-secret exemption.
  const lossy = fixture({ note: "ordinary prose", "session.id": "session prose", "gen_ai.usage.input_tokens": 87004321 });
  lossy.droppedAttributesCount = 1;
  expect(privacyFilter({}, ["87004321"]).span(lossy).attributes).toEqual({ note: UNKNOWN, "session.id": "<redacted>", "gen_ai.usage.input_tokens": "<redacted>", [HOST]: true });
});

test("R10 C later tracestate loss is known before payload discovery", () => {
  // Explicit host marker, not a resource/scope length inference.
  const raw = fixture({ password: "synthetic-r10-not-discovered", echo: "short prose" });
  raw._spanContext.traceState = { _rawTraceState: "vendor=tiny [truncated]" };
  const clean = privacyFilter().span(raw);
  expect(clean.attributes).toEqual({ password: UNKNOWN, echo: UNKNOWN, [HOST]: true });
  expect(clean.spanContext().traceState).toBeUndefined();
});

// D/E are HARNESS_DEFECT model controls, not claims that the app PTC was applied.
const scopeOf = (span: any) => span.instrumentationScope ?? span.instrumentationLibrary ?? {};
const nativeStamp = (span: any) => JSON.stringify({ context: span.spanContext(), name: span.name, attributes: span.attributes,
  status: span.status, events: span.events, links: span.links, resource: span.resource.attributes, scope: scopeOf(span),
  startTime: span.startTime, endTime: span.endTime, droppedAttributesCount: span.droppedAttributesCount,
  droppedEventsCount: span.droppedEventsCount, droppedLinksCount: span.droppedLinksCount });
test("R10 D plain native projection excludes processor cycles and detects semantic writes", () => {
  const raw = fixture({ note: "native prose" }), processor: any = {};
  processor._shutdownOnce = { _that: processor }; raw._spanProcessor = processor;
  expect(() => JSON.stringify(raw)).toThrow();
  const before = nativeStamp(raw), clean = privacyFilter().span(raw);
  expect(clean.attributes.note).toBe("native prose"); expect(nativeStamp(raw)).toBe(before);
  raw.attributes.note = "changed prose";
  expect(nativeStamp(raw)).not.toBe(before);
});
test("R10 E legacy scope observation preserves native witness and export metadata", () => {
  const raw = fixture({ note: "native prose" }); delete raw.instrumentationScope;
  raw.instrumentationLibrary = { name: "genkit-tracer", version: "1.2.3", attributes: { note: "scope prose" } };
  expect(() => raw.instrumentationScope.attributes).toThrow();
  const before = nativeStamp(raw), witnesses: any[] = [];
  const witness = (span: any) => ({ name: span.name, scope: scopeOf(span).attributes, traceId: span.spanContext().traceId });
  witnesses.push(...[raw].map(witness));
  expect(witnesses).toEqual([{ name: "r10 name", scope: { note: "scope prose" }, traceId: "3".repeat(32) }]);
  const clean = privacyFilter().span(raw);
  expect(clean.instrumentationScope.name).toBe("genkit-tracer");
  expect((clean.instrumentationScope as any).attributes).toEqual({ note: "scope prose" });
  expect(nativeStamp(raw)).toBe(before);
});
