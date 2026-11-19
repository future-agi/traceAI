import type { Attributes } from "@opentelemetry/api";
import { BasicTracerProvider, InMemorySpanExporter, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { resourceFromAttributes } from "@opentelemetry/resources";
import { privacyFilter, rememberSpanValueLimit } from "../export-privacy";
import { indexPrivacyHints, mediaReplacement, scrubValue } from "../privacy-runtime";
import { getRegisteredSecrets, registerSecret } from "../secrets";
import { FITracer } from "../trace/trace-config/FITracer";

// Literal regression controls; writer runtime NOT_RUN. Existing tests are immutable.
const savedEnv = { ...process.env };
const registryKey = Symbol.for("traceai.secrets");
let savedRegistry: PropertyDescriptor | undefined;
beforeEach(() => {
  for (const key of Object.keys(process.env)) if (/^(FI_|OTEL_)/.test(key)) delete process.env[key];
  savedRegistry = Object.getOwnPropertyDescriptor(globalThis, registryKey);
  Reflect.deleteProperty(globalThis, registryKey);
});
afterEach(() => {
  jest.restoreAllMocks();
  process.env = { ...savedEnv };
  Reflect.deleteProperty(globalThis, registryKey);
  if (savedRegistry) Object.defineProperty(globalThis, registryKey, savedRegistry);
});
const COUNT = "traceai.capture.dropped_content_count";
const HOST = "traceai.capture.host_data_unknown";
const UNKNOWN = "<unknown: host-truncated>";
const SECRET = "synthetic-r8-count-credential";
function fixture(attributes: Record<string, unknown> = {}): any {
  const ctx = { traceId: "7".repeat(32), spanId: "8".repeat(16), traceFlags: 1 };
  return { _spanContext: ctx, spanContext: () => ctx, name: "r8 ordinary name", attributes,
    kind: 2, startTime: [1, 0], endTime: [2, 0], _duration: [1, 0], ended: true,
    droppedAttributesCount: 0, droppedEventsCount: 0, droppedLinksCount: 0,
    status: { code: 2, message: "r8 ordinary status" }, resource: { attributes: {} },
    instrumentationScope: { name: "r8 ordinary scope" }, events: [], links: [] };
}

// P1: the ElevenLabs lens witnesses, plus the separately audited matching key.
test.each([
  { label: "non-magic base64", input: "AQIDBAUGBwgJCgsM", expected: "<omitted: 16 base64 chars>" },
  { label: "private prose", input: "caller private transcript!", expected: "<omitted: media>" },
  { label: "numeric string", input: "16777216", expected: "<omitted: 8 base64 chars>" },
  { label: "true", input: true, expected: "<omitted: media>" },
  { label: "false", input: false, expected: "<omitted: media>" },
  { label: "negative", input: -1, expected: "<omitted: media>" },
  { label: "fraction", input: 0.5, expected: "<omitted: media>" },
  { label: "unsafe", input: 9007199254740992, expected: "<omitted: media>" },
  { label: "NaN", input: NaN, expected: "<omitted: media>" },
  { label: "positive infinity", input: Infinity, expected: "<omitted: media>" },
  { label: "negative infinity", input: -Infinity, expected: "<omitted: media>" },
  { label: "bytes array", input: [1, 2, 3], expected: "<omitted: media>" },
  { label: "object type token", input: { private: "caller private transcript!" }, expected: "object" },
  { label: "PNG", input: "iVBORw0KGgoAAAANSUhEUg==", expected: "<omitted: 24 base64 chars>" },
  { label: "data URI", input: "data:audio/pcm;base64,AQID", expected: "<omitted: 4 base64 chars>" },
])("R8 P1 $label never receives the byte-count exemption", ({ input, expected }) => {
  for (const key of ["elevenlabs.audio.bytes", "gen_ai.audio.bytes"]) {
    const attrs = { [key]: input }, filter = privacyFilter();
    // PENDING Z-OVER: F2 cap-0 whole-scope replacement precedes I1 media markers.
    if (input === "iVBORw0KGgoAAAANSUhEUg==") {
      expect(filter.admissionAttributes(attrs)).toEqual({ [key]: "[image-redacted]" });
      expect(filter.span(fixture(attrs)).attributes).toEqual({ [key]: "[image-redacted]" });
      const positive = privacyFilter({ base64ImageMaxLength: 32000, hideInputImages: true });
      expect(positive.admissionAttributes(attrs)).toEqual({ [key]: expected });
      expect(positive.span(fixture(attrs)).attributes).toEqual({ [key]: expected });
    } else {
      expect(filter.admissionAttributes(attrs)).toEqual({ [key]: expected });
      expect(filter.span(fixture(attrs)).attributes).toEqual({ [key]: expected });
    }
    expect(attrs[key]).toBe(input);
  }
});

test.each([0, 9, 9007199254740991])("R8 P1 safe byte count %s stays numeric", count => {
  const attrs = { "elevenlabs.audio.bytes": count, "gen_ai.audio.bytes": count };
  expect(privacyFilter().admissionAttributes(attrs)).toEqual(attrs);
  expect(privacyFilter().span(fixture(attrs)).attributes).toEqual(attrs);
  expect(scrubValue({ elevenlabs: { audio: { bytes: count } } })).toEqual({ elevenlabs: { audio: { bytes: count } } });
});

test("R8 P1 typed counts do not override credentials, hints, payload ancestry or exact paths", () => {
  const filter = privacyFilter({}, [SECRET, "16777216"]);
  expect(filter.admissionAttributes({ "elevenlabs.audio.bytes": SECRET })).toEqual({ "elevenlabs.audio.bytes": "<redacted>" });
  expect(filter.span(fixture({ "elevenlabs.audio.bytes": 16777216 })).attributes).toEqual({ "elevenlabs.audio.bytes": "<redacted>" });
  expect(filter.admissionAttributes({ "elevenlabs.audio.bytes": 16777216 })).toEqual({ "elevenlabs.audio.bytes": "<redacted>" });
  expect(filter.span(fixture({ "secret.elevenlabs.audio.bytes": 9 })).attributes).toEqual({ "secret.elevenlabs.audio.bytes": "<redacted>" });
  for (const hint of [{ "elevenlabs.audio.mime_type": "audio/mpeg" }, { "elevenlabs.audio.type": "input_audio" }]) {
    const attrs = { ...hint, "elevenlabs.audio.bytes": 9 };
    expect(filter.admissionAttributes(attrs)["elevenlabs.audio.bytes"]).toBe("<omitted: media>");
    expect(filter.span(fixture(attrs)).attributes["elevenlabs.audio.bytes"]).toBe("<omitted: media>");
  }
  expect(mediaReplacement(9, "elevenlabs.audio.bytes", indexPrivacyHints([["type", "input_audio"]]))).toBe("<omitted: media>");
  expect(scrubValue(9, [], "elevenlabs.audio.bytes", 0, true)).toBe("<omitted: media>");
  expect(scrubValue({ audio: { data: { "elevenlabs.audio.bytes": 9 } } })).toEqual({ audio: { data: { "elevenlabs.audio.bytes": "<omitted: media>" } } });
  expect(filter.admissionAttributes({ "x.elevenlabs.audio.bytes": 9 })).toEqual({ "x.elevenlabs.audio.bytes": "<omitted: media>" });
  expect(filter.admissionAttributes({ "elevenlabs.audio.bytes": undefined })).toEqual({});
});

// P2: count provenance belongs to the auto lineage, not to telemetry.
test.each(["start", "write"])("R8 P2 %s: native 999 and forged detached 888 cannot inflate one FI redaction", async schedule => {
  const filter = privacyFilter({}, [SECRET]), memory = new InMemorySpanExporter();
  const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(memory)] });
  try {
    const tracer = new FITracer({ tracer: FITracer.autoProvider(provider).getTracer("r8-count") });
    const span = tracer.startSpan("r8 count", { attributes: schedule === "start" ? { first: SECRET } : {} });
    if (schedule === "write") span.setAttribute("first", SECRET);
    const nativeHandle = span.getContextSpan();
    nativeHandle.setAttribute(COUNT, 999);
    span.end();
    const native = memory.getFinishedSpans()[0], before = JSON.stringify(native.attributes);
    const clean = filter.span(native);
    expect(clean.attributes).toEqual({ first: "<redacted>", [COUNT]: 1 });
    expect(native.attributes[COUNT]).toBe(999);
    expect(native.attributes.first).toBe("<redacted>");
    clean.attributes[COUNT] = 888;
    const later = filter.span(clean);
    expect(later.attributes).toEqual({ first: "<redacted>", [COUNT]: 1 });
    expect(clean.attributes[COUNT]).toBe(888);
    expect(filter.span(later).attributes[COUNT]).toBe(1);
    expect(JSON.stringify(native.attributes)).toBe(before);
    expect(JSON.stringify(later)).not.toContain(SECRET);
  } finally { await provider.shutdown(); }
});

test.each([false, true])("R8 P2 auto=%s: zero-difference provenance does not change standalone incoming counts", async auto => {
  const memory = new InMemorySpanExporter(), provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(memory)] });
  try {
    const tracer = new FITracer({ tracer: (auto ? FITracer.autoProvider(provider) : provider).getTracer("r8-zero") });
    const span = tracer.startSpan("r8 zero", { attributes: { note: "r8 visible", [COUNT]: 999 } });
    span.end();
    const native = memory.getFinishedSpans()[0];
    const clean = privacyFilter().span(native);
    expect(clean.attributes).toEqual(auto ? { note: "r8 visible" } : { note: "r8 visible", [COUNT]: 999 });
    expect(native.attributes[COUNT]).toBe(999);
    clean.attributes[COUNT] = 888;
    expect(privacyFilter().span(clean).attributes[COUNT]).toBe(auto ? undefined : 888);
    expect(clean.attributes[COUNT]).toBe(888);
  } finally { await provider.shutdown(); }
});

test("R8 P2 repeated writes count twice, not once per key or again per export", async () => {
  registerSecret(SECRET);
  const memory = new InMemorySpanExporter(), provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(memory)] });
  try {
    const span = new FITracer({ tracer: FITracer.autoProvider(provider).getTracer("r8-repeat") }).startSpan("r8 repeat");
    span.setAttribute("first", SECRET); span.setAttribute("first", SECRET); span.end();
    const clean = privacyFilter().span(memory.getFinishedSpans()[0]);
    expect(clean.attributes).toEqual({ first: "<redacted>", [COUNT]: 2 });
    expect(privacyFilter().span(clean).attributes).toEqual({ first: "<redacted>", [COUNT]: 2 });
    expect(memory.getFinishedSpans()[0].attributes[COUNT]).toBeUndefined();
  } finally { await provider.shutdown(); }
});

// Regression: keep legacy combined drop telemetry, but never call its FI part host loss.
const timing = { "gen_ai.response.time_to_first_token": 0.25, "gen_ai.server.request.duration": 1.75 };
const crowded: Attributes = { "gen_ai.span.kind": "LLM", "gen_ai.usage.input_tokens": 7,
  "output.value": "ordinary answer", "output.mime_type": "text/plain",
  ...Object.fromEntries(Array.from({ length: 150 }, (_, i) => [`gen_ai.input.messages.${i}.content`, "ordinary words"])) };
test.each([{ limit: 16, drops: 141 }, { limit: 32, drops: 125 }, { limit: 128, drops: 29 }])(
  "R8 projection at $limit: initial and late timing remain numeric with $drops FI-only drops", async ({ limit, drops }) => {
    const memory = new InMemorySpanExporter();
    const provider = new BasicTracerProvider({ spanLimits: { attributeCountLimit: limit }, spanProcessors: [
      { onStart() {}, onEnd(span) { rememberSpanValueLimit(span); }, async forceFlush() {}, async shutdown() {} },
      new SimpleSpanProcessor(memory),
    ] });
    try {
      const tracer = new FITracer({ tracer: provider.getTracer("r8-projection") });
      tracer.startSpan("r8 initial", { attributes: { ...crowded, ...timing } }).end();
      const late = tracer.startSpan("r8 late", { attributes: crowded });
      late.setAttributes(timing); late.end();
      expect(memory.getFinishedSpans()).toHaveLength(2);
      for (const native of memory.getFinishedSpans()) {
        expect(native.droppedAttributesCount).toBe(drops);
        expect(native.attributes[COUNT]).toBe(drops);
        const before = JSON.stringify(native.attributes), clean = privacyFilter().span(native);
        for (const view of [clean, privacyFilter().span(clean)]) {
          expect(view.attributes).toMatchObject({ ...timing, "gen_ai.usage.input_tokens": 7, "output.value": "ordinary answer", [COUNT]: drops });
          expect(view.attributes[HOST]).toBeUndefined();
          expect(view.droppedAttributesCount).toBe(drops);
          expect(view.name).toBe(native.name);
        }
        expect(JSON.stringify(native.attributes)).toBe(before);
      }
    } finally { await provider.shutdown(); }
  });

test.each([16, 24])("R8 projection: complete text at value limit %s survives, changed content loses its evidence", async limit => {
  const memory = new InMemorySpanExporter();
  const provider = new BasicTracerProvider({ resource: resourceFromAttributes({ "service.name": "svc" }), spanLimits: { attributeCountLimit: 32, attributeValueLengthLimit: limit }, spanProcessors: [new SimpleSpanProcessor(memory)] });
  try {
    const span = new FITracer({ tracer: provider.getTracer("r8-text") }).startSpan("r8 text", { attributes: crowded });
    span.end();
    const native = memory.getFinishedSpans()[0];
    expect(native.droppedAttributesCount).toBe(123);
    const filter = privacyFilter(), clean = filter.span(native);
    expect(clean.attributes["output.value"]).toBe("ordinary answer");
    expect(clean.attributes[HOST]).toBeUndefined();
    native.attributes["output.value"] = "AQID";
    const changed = filter.span(native);
    expect(changed.attributes["output.value"]).toBe("<omitted: 4 base64 chars>");
    expect(changed.attributes[HOST]).toBeUndefined();
    expect(native.attributes["output.value"]).toBe("AQID");
  } finally { await provider.shutdown(); }
});

test("R8 projection: an additional real SDK drop still applies the full sticky Q8b blanket", async () => {
  let handle: any;
  const memory = new InMemorySpanExporter();
  const provider = new BasicTracerProvider({ spanLimits: { attributeCountLimit: 16 }, spanProcessors: [
    { onStart(span) { handle = span; }, onEnd() {}, async forceFlush() {}, async shutdown() {} },
    new SimpleSpanProcessor(memory),
  ] });
  try {
    const span = new FITracer({ tracer: provider.getTracer("r8-host-drop") }).startSpan("r8 host name", { attributes: { ...crowded, ...timing } });
    const beforeDrop = handle.droppedAttributesCount;
    handle.setAttribute("foreign.excess", "r8 discarded by SDK");
    expect(handle.droppedAttributesCount).toBe(beforeDrop + 1);
    expect(handle.attributes["foreign.excess"]).toBeUndefined();
    handle.addEvent("r8 host event", { note: "r8 event prose", "session.id": "r8 session" });
    handle.addLink({ context: handle.spanContext(), attributes: { note: "r8 link prose" } });
    handle.setStatus({ code: 2, message: "r8 host status" });
    span.end();
    const native = memory.getFinishedSpans()[0], before = JSON.stringify({ attributes: native.attributes, events: native.events, links: native.links, status: native.status });
    expect(native.droppedAttributesCount).toBe(142);
    const clean = privacyFilter().span(native);
    for (const view of [clean, privacyFilter().span(clean)]) {
      expect(view.attributes[HOST]).toBe(true);
      expect(view.attributes["gen_ai.usage.input_tokens"]).toBe(7);
      for (const [key, value] of Object.entries(view.attributes)) {
        if (key === HOST) expect(value).toBe(true);
        else if (key === COUNT) expect(value).toBe(141);
        else if (key === "gen_ai.span.kind") expect(value).toBe("LLM");
        else if (key === "gen_ai.usage.input_tokens") expect(value).toBe(7);
        else expect(value).toBe(UNKNOWN);
      }
      expect(view.attributes["gen_ai.response.time_to_first_token"]).toBe(UNKNOWN);
      expect(view.attributes["output.value"]).toBe(UNKNOWN);
      expect(view.attributes[COUNT]).toBe(141);
      expect(view.droppedAttributesCount).toBe(142);
      expect(view.name).toBe(UNKNOWN);
      expect(view.status).toEqual({ code: 2, message: UNKNOWN });
      expect(view.instrumentationScope.name).toBe(UNKNOWN);
      for (const value of Object.values(view.resource.attributes)) expect(value).toBe(UNKNOWN);
      expect(view.events[0].name).toBe(UNKNOWN);
      expect(view.events[0].attributes).toEqual({ note: UNKNOWN, "session.id": "<redacted>" });
      expect(view.links[0].attributes).toEqual({ note: UNKNOWN });
      expect(view.attributes["foreign.excess"]).toBeUndefined();
    }
    expect(JSON.stringify({ attributes: native.attributes, events: native.events, links: native.links, status: native.status })).toBe(before);
  } finally { await provider.shutdown(); }
});

test("R8 projection: genuine SDK drops during standalone re-admission are not overwritten", async () => {
  let handle: any;
  const memory = new InMemorySpanExporter();
  const provider = new BasicTracerProvider({ spanLimits: { attributeCountLimit: 16 }, spanProcessors: [
    { onStart(span) { handle = span; }, onEnd() {}, async forceFlush() {}, async shutdown() {} },
    new SimpleSpanProcessor(memory),
  ] });
  try {
    const span = new FITracer({ tracer: provider.getTracer("r8-readmission") }).startSpan("r8 readmission", { attributes: { ...crowded, ...timing } });
    expect(handle.droppedAttributesCount).toBe(141);
    const setAttributes = handle.setAttributes.bind(handle);
    jest.spyOn(handle, "setAttributes").mockImplementation((attributes: unknown) => {
      setAttributes(attributes);
      // Exercise the real SDK setter after projection has filled its 16 slots.
      setAttributes({ "foreign.readmission.excess": "r8 actual SDK refusal" });
      return handle;
    });
    span.end();
    const native = memory.getFinishedSpans()[0];
    expect(native.droppedAttributesCount).toBe(142);
    expect(native.attributes["foreign.readmission.excess"]).toBeUndefined();
    const clean = privacyFilter().span(native);
    expect(clean.attributes[HOST]).toBe(true);
    expect(clean.attributes["gen_ai.response.time_to_first_token"]).toBe(UNKNOWN);
    expect(clean.attributes[COUNT]).toBe(141);
    expect(clean.droppedAttributesCount).toBe(142);
  } finally { await provider.shutdown(); }
});

test("R8 projection: telemetry alone cannot authenticate an FI component in a native drop count", () => {
  const raw = fixture({ [COUNT]: 999, note: "r8 must withhold", "session.id": "r8 private session" });
  raw.droppedAttributesCount = 1;
  const clean = privacyFilter().span(raw);
  expect(clean.attributes).toEqual({ [COUNT]: 999, note: UNKNOWN, "session.id": "<redacted>", [HOST]: true });
  expect(clean.name).toBe(UNKNOWN);
  expect(clean.status).toEqual({ code: 2, message: UNKNOWN });
  expect(raw.attributes.note).toBe("r8 must withhold");
});

// H1: the shared 4 MiB discovery ledger owns each ordinary input once.
test.each([65536, 131072])("R8 H1 two long exception fields fit one discovery debit and keep populated scrubbed output (repeats=%i)", repeats => {
  const prose = "Note: m\n\n".repeat(repeats);
  const original = `${prose}password=synthetic-r8-long-password\nend of message`;
  const attrs = { "exception.type": "Error", "exception.message": original, "exception.stacktrace": original };
  const clean = privacyFilter().admissionAttributes(attrs);
  expect(clean["exception.type"]).toBe("Error");
  // PENDING I1xF2-PRECEDENCE: F2 scan bound precedes I1 long-prose retention.
  if (repeats === 65536) {
    expect(clean["exception.message"]).toBe(`${prose}password="<redacted>"\nend of message`);
  } else {
    expect(clean["exception.message"]).toBe("[image-redacted]");
  }
  // PENDING I1xF2-PRECEDENCE: F2 scan bound precedes I1 long-prose retention.
  if (repeats === 65536) {
    expect(clean["exception.stacktrace"]).toBe(`${prose}password="<redacted>"\nend of message`);
  } else {
    expect(clean["exception.stacktrace"]).toBe("[image-redacted]");
  }
  expect(attrs["exception.message"]).toBe(original);
}, 30000);

test.each([65536, 131072])("R8 H1 recordException retains long prose and late password masking (repeats=%i)", async repeats => {
  const memory = new InMemorySpanExporter(), provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(memory)] });
  try {
    const prose = "Note: m\n\n".repeat(repeats);
    const span = new FITracer({ tracer: provider.getTracer("r8-h1") }).startSpan("r8 large");
    span.recordException(new Error(`${prose}password=synthetic-r8-h1-password\nend of message`));
    span.end();
    const event = memory.getFinishedSpans()[0].events[0];
    expect(event.attributes?.["exception.type"]).toBe("Error");
    // PENDING I1xF2-PRECEDENCE: F2 scan bound precedes I1 long-prose retention.
    if (repeats === 65536) {
      expect(event.attributes?.["exception.message"]).toBe(`${prose}password="<redacted>"\nend of message`);
    } else {
      expect(event.attributes?.["exception.message"]).toBe("[image-redacted]");
    }
    expect(JSON.stringify(event)).not.toContain("synthetic-r8-h1-password");
  } finally { await provider.shutdown(); }
}, 30000);

test.each([100000, 110000])("R8 H1 ordinary export queue stays populated and still discovers credential-valued attributes (repeats=%i)", repeats => {
  const prose = "ordinary! ".repeat(repeats), secret = "synthetic-r8-discovery-password";
  const raw = fixture({ note: prose, password: secret, echo: secret });
  expect(getRegisteredSecrets()).not.toContain(secret);
  const clean = privacyFilter().span(raw);
  // PENDING I1xF2-PRECEDENCE: F2 scan bound precedes I1 long-prose retention.
  if (repeats === 100000) {
  expect(clean.attributes).toEqual({ note: prose, password: "<redacted>", echo: "<redacted>" });
  } else {
    expect(clean.attributes).toEqual({ note: "[image-redacted]", password: "<redacted>", echo: "<redacted>" });
  }
  expect(getRegisteredSecrets()).toContain(secret);
  expect(clean.name).toBe("r8 ordinary name");
  expect(raw.attributes.echo).toBe(secret);
}, 30000);

test("R8 H1 one shared allowance still refuses over 4 MiB and keeps FI refusal distinct from host loss", () => {
  const chunk = "ordinary! ".repeat(210000);
  expect(() => privacyFilter().admissionAttributes({ first: chunk, second: chunk })).toThrow("Privacy secret discovery incomplete");
  const raw = fixture({ first: chunk, second: chunk }), clean = privacyFilter().span(raw);
  expect(clean.attributes).toEqual({});
  expect(clean.name).toBe(UNKNOWN);
  expect(clean.status).toEqual({ code: 2 });
  expect(clean.events).toEqual([]);
  expect(clean.links).toEqual([]);
  expect(clean.resource.attributes).toEqual({});
  expect(clean.instrumentationScope).toEqual({ name: UNKNOWN });
  expect(raw.attributes.first).toBe(chunk);
}, 30000);
