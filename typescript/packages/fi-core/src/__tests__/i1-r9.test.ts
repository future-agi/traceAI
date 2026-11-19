import type { Attributes } from "@opentelemetry/api";
import { BasicTracerProvider, InMemorySpanExporter, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { resourceFromAttributes } from "@opentelemetry/resources";
import { privacyFilter, rememberMediaAdmission, rememberProjectedAttributeDrops } from "../export-privacy";
import { indexPrivacyHints, mediaReplacement, scrubValue } from "../privacy-runtime";
import { projectAttributes } from "../attribute-projection";
import { getRegisteredSecrets } from "../secrets";
import { FITracer } from "../trace/trace-config/FITracer";

// Literal controls. Runtime NOT_RUN; all pre-existing test files remain immutable.
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
const COUNT = "traceai.capture.dropped_content_count", HOST = "traceai.capture.host_data_unknown";
const UNKNOWN = "<unknown: host-truncated>";
function fixture(attributes: Attributes = {}): any {
  const ctx = { traceId: "9".repeat(32), spanId: "1".repeat(16), traceFlags: 1 };
  return { _spanContext: ctx, spanContext: () => ctx, name: "r9 name", attributes,
    kind: 2, startTime: [1, 0], endTime: [2, 0], _duration: [1, 0], ended: true,
    droppedAttributesCount: 0, droppedEventsCount: 0, droppedLinksCount: 0,
    status: { code: 2 }, resource: { attributes: {} }, instrumentationScope: { name: "r9 scope" }, events: [], links: [] };
}
const crowded: Attributes = { "gen_ai.span.kind": "LLM", "gen_ai.usage.input_tokens": 7,
  "output.value": "ordinary answer", "output.mime_type": "text/plain",
  ...Object.fromEntries(Array.from({ length: 150 }, (_, i) => [`gen_ai.input.messages.${i}.content`, "ordinary words"])) };
const timing = { "gen_ai.response.time_to_first_token": 0.25, "gen_ai.server.request.duration": 1.75 };
test.each([{ limit: 16, drops: 141 }, { limit: 32, drops: 125 }, { limit: 128, drops: 29 }])(
  "R9 FI projection $limit retains initial and late text and timing through repeated export", async ({ limit, drops }) => {
    const memory = new InMemorySpanExporter(), provider = new BasicTracerProvider({
      spanLimits: { attributeCountLimit: limit }, spanProcessors: [new SimpleSpanProcessor(memory)],
    });
    try {
      const tracer = new FITracer({ tracer: provider.getTracer("r9 projection") });
      tracer.startSpan("initial", { attributes: { ...crowded, ...timing } }).end();
      const late = tracer.startSpan("late", { attributes: crowded }); late.setAttributes(timing); late.end();
      expect(memory.getFinishedSpans()).toHaveLength(2);
      for (const raw of memory.getFinishedSpans()) {
        const before = JSON.stringify(raw.attributes), filter = privacyFilter();
        expect(raw.droppedAttributesCount).toBe(drops);
        let clean = filter.span(raw);
        for (let i = 0; i < 3; i++) {
          expect(clean.attributes).toMatchObject({ ...timing, "output.value": "ordinary answer", "output.mime_type": "text/plain", [COUNT]: drops });
          expect(clean.attributes[HOST]).toBeUndefined(); expect(clean.droppedAttributesCount).toBe(drops);
          clean = filter.span(clean);
        }
        expect(JSON.stringify(raw.attributes)).toBe(before);
      }
    } finally { await provider.shutdown(); }
  });

test.each([16, 24])("R9 finite native limit %s preserves complete output but not a later replacement", async limit => {
  const memory = new InMemorySpanExporter(), provider = new BasicTracerProvider({
    resource: resourceFromAttributes({ "service.name": "svc" }), spanLimits: { attributeCountLimit: 32, attributeValueLengthLimit: limit }, spanProcessors: [new SimpleSpanProcessor(memory)],
  });
  try {
    new FITracer({ tracer: provider.getTracer("r9 text") }).startSpan("text", { attributes: crowded }).end();
    const raw = memory.getFinishedSpans()[0], filter = privacyFilter();
    expect(raw.droppedAttributesCount).toBe(123);
    const clean = filter.span(raw);
    expect(clean.attributes["output.value"]).toBe("ordinary answer"); expect(clean.attributes[HOST]).toBeUndefined();
    expect(filter.span(clean).attributes["output.value"]).toBe("ordinary answer");
    raw.attributes["output.value"] = "AQID";
    const changed = filter.span(raw);
    expect(changed.attributes["output.value"]).toBe("<omitted: 4 base64 chars>");
    expect(changed.attributes[HOST]).toBeUndefined(); expect(raw.attributes["output.value"]).toBe("AQID");
  } finally { await provider.shutdown(); }
});

test("R9 complete MIME proof crosses only FI-created unchanged maps", () => {
  const raw = fixture({ "output.value": "ordinary answer", "output.mime_type": "text/plain", [COUNT]: 1 });
  raw.droppedAttributesCount = 1; rememberProjectedAttributeDrops(raw, 1); rememberMediaAdmission(raw, raw.attributes);
  const before = JSON.stringify(raw.attributes), filter = privacyFilter(), first = filter.span(raw), second = filter.span(first);
  expect(first.attributes["output.value"]).toBe("ordinary answer");
  expect(second.attributes["output.value"]).toBe("ordinary answer"); expect(second.attributes[HOST]).toBeUndefined();
  first.attributes["output.value"] = "AQID";
  expect(filter.span(first).attributes["output.value"]).toBe("<omitted: 4 base64 chars>");
  expect(JSON.stringify(raw.attributes)).toBe(before);
  // Copying the map does not copy the private admission proof.
  const copied = { ...second, attributes: { ...second.attributes } };
  rememberProjectedAttributeDrops(copied, 1);
  expect(filter.span(copied).attributes["output.value"]).toBe("<omitted: media>");
});

test("R9 FI-generated marker at the retained limit is not native truncation", () => {
  const raw = fixture({ "output.value": "ordinary answer" });
  raw._attributeValueLengthLimit = 16; raw.droppedAttributesCount = 1; rememberProjectedAttributeDrops(raw, 1);
  const filter = privacyFilter(), clean = filter.span(raw);
  expect(clean.attributes).toEqual({ "output.value": "<omitted: media>" });
  expect(filter.span(clean).attributes).toEqual({ "output.value": "<omitted: media>" });
  expect(raw.attributes["output.value"]).toBe("ordinary answer");
  clean.attributes["output.value"] = "abcdefghijklmnop";
  const changed = filter.span(clean);
  expect(changed.attributes).toEqual({ "output.value": UNKNOWN, [HOST]: true });
  expect(changed.name).toBe(UNKNOWN);
});

test("R9 complete text evidence cannot override genuine host loss or explicit media", () => {
  const raw = fixture({ "output.value": "ordinary answer", "output.mime_type": "text/plain", "session.id": "r9 session", "gen_ai.usage.input_tokens": 7 });
  rememberMediaAdmission(raw, raw.attributes);
  raw.droppedAttributesCount = 2; rememberProjectedAttributeDrops(raw, 1);
  const filter = privacyFilter(), clean = filter.span(raw);
  expect(clean.attributes).toEqual({ "output.value": UNKNOWN, "output.mime_type": UNKNOWN, "session.id": "<redacted>", "gen_ai.usage.input_tokens": 7, [HOST]: true });
  expect(clean.name).toBe(UNKNOWN); expect(filter.span(clean).attributes).toEqual(clean.attributes);
  const media = fixture({ "output.value": "ordinary answer", "output.mime_type": "text/plain" });
  rememberMediaAdmission(media, media.attributes);
  media.attributes["output.mime_type"] = "audio/mpeg";
  expect(filter.span(media).attributes["output.value"]).toBe("<omitted: media>");
});

test.each([
  { count: 0, gen: 0, eleven: 0 },
  { count: 9, gen: 9, eleven: 9 },
  { count: "0", gen: "0", eleven: "<omitted: 1 base64 chars>" },
  { count: "9", gen: "9", eleven: "<omitted: 1 base64 chars>" },
  { count: "10", gen: "10", eleven: "<omitted: 2 base64 chars>" },
  { count: "123", gen: "123", eleven: "<omitted: 3 base64 chars>" },
  { count: "16777216", gen: "<omitted: 8 base64 chars>", eleven: "<omitted: 8 base64 chars>" },
  { count: "9007199254740991", gen: "<omitted: 16 base64 chars>", eleven: "<omitted: 16 base64 chars>" },
])(
  "R9 declared byte counter $count follows confined r2 roles", ({ count, gen, eleven }) => {
    // REGRESSION control: FI scalar roles, not evidence of host-side loss.
    for (const { key, expected } of [{ key: "gen_ai.audio.bytes", expected: gen }, { key: "elevenlabs.audio.bytes", expected: eleven }]) {
      expect(mediaReplacement(count, key)).toBe(expected === count ? undefined : expected); expect(scrubValue(count, [], key)).toBe(expected);
      expect(projectAttributes({ [key]: count }, 16).attributes[key]).toBe(expected);
      expect(privacyFilter().admissionAttributes({ [key]: count })).toEqual({ [key]: expected });
      expect(privacyFilter().span(fixture({ [key]: count })).attributes).toEqual({ [key]: expected });
    }
  });

test.each([
  ["AQIDBAUGBwgJCgsM", "<omitted: 16 base64 chars>"], ["caller private transcript!", "<omitted: media>"],
  ["01", "<omitted: 2 base64 chars>"], ["+9", "<omitted: 2 base64 chars>"], ["-1", "<omitted: 2 base64 chars>"],
  ["9.0", "<omitted: media>"], [" 9", "<omitted: media>"], ["9\n", "<omitted: 2 base64 chars>"],
  ["9007199254740992", "<omitted: 16 base64 chars>"], ["1e3", "<omitted: 3 base64 chars>"],
  ["iVBORw0KGgoAAAANSUhEUg==", "<omitted: 24 base64 chars>"], ["data:audio/pcm;base64,AQID", "<omitted: 4 base64 chars>"],
])("R9 non-counter string %s keeps role-free withholding", (value, expected) => {
  for (const key of ["gen_ai.audio.bytes", "elevenlabs.audio.bytes"]) {
    // PENDING Z-OVER: F2 cap-0 whole-scope replacement precedes I1 media markers.
    if (value === "iVBORw0KGgoAAAANSUhEUg==") {
      expect(privacyFilter().admissionAttributes({ [key]: value })).toEqual({ [key]: "[image-redacted]" });
      expect(privacyFilter().span(fixture({ [key]: value })).attributes).toEqual({ [key]: "[image-redacted]" });
      const positive = privacyFilter({ base64ImageMaxLength: 32000, hideInputImages: true });
      expect(positive.admissionAttributes({ [key]: value })).toEqual({ [key]: expected });
      expect(positive.span(fixture({ [key]: value })).attributes).toEqual({ [key]: expected });
    } else {
      expect(privacyFilter().admissionAttributes({ [key]: value })).toEqual({ [key]: expected });
      expect(privacyFilter().span(fixture({ [key]: value })).attributes).toEqual({ [key]: expected });
    }
  }
});

test("R9 canonical decimal role never outranks credentials, payload ancestry, media hints or Q8b", () => {
  for (const key of ["gen_ai.audio.bytes", "elevenlabs.audio.bytes"]) {
    expect(scrubValue("16777216", ["16777216"], key)).toBe("<redacted>");
    expect(scrubValue("9", [], `secret.${key}`)).toBe("<redacted>");
    expect(scrubValue({ audio: { data: { [key]: "9" } } })).toEqual({ audio: { data: { [key]: "<omitted: 1 base64 chars>" } } });
    expect(mediaReplacement("9", key, indexPrivacyHints([["type", "input_audio"]]))).toBe("<omitted: 1 base64 chars>");
    expect(scrubValue("9", [], `x.${key}`)).toBe("<omitted: 1 base64 chars>");
    expect(scrubValue([9], [], key)).toBe("<omitted: media>");
  }
  const raw = fixture({ "gen_ai.usage.input_tokens": "9" }); raw.droppedAttributesCount = 1;
  expect(privacyFilter().span(raw).attributes).toEqual({ "gen_ai.usage.input_tokens": UNKNOWN, [HOST]: true });
});

test("R9 suffix and discovery probes reserve one shared export allowance", () => {
  const chunk = "ordinary! ".repeat(10000), raw = fixture({ note: chunk });
  raw.events = Array.from({ length: 32 }, () => ({ name: "event", attributes: { note: chunk } }));
  raw.links = [{ context: raw.spanContext(), attributes: { note: "tiny [truncated]" } }];
  const before = JSON.stringify(raw), filter = privacyFilter({}, ["absent-r9-synthetic-secret"]);
  const test = RegExp.prototype.test, includes = String.prototype.includes;
  let chars = 0;
  const charge = (value: string): void => {
    if (value === chunk) { chars += value.length; if (chars > 4194304) throw new Error("R9 observed probe overrun"); }
  };
  const regexSpy = jest.spyOn(RegExp.prototype, "test").mockImplementation(function (this: RegExp, value: string) { charge(value); return test.call(this, value); });
  const prefixSpy = jest.spyOn(String.prototype, "includes").mockImplementation(function (this: string, search: string, position?: number) { charge(String(this)); return includes.call(this, search, position); });
  let clean: any;
  try { clean = filter.span(raw); } finally { regexSpy.mockRestore(); prefixSpy.mockRestore(); }
  expect(chars).toBeGreaterThan(0); expect(chars).toBeLessThanOrEqual(4194304);
  expect(clean.attributes).toEqual({ [HOST]: true }); expect(clean.events).toEqual([]); expect(clean.links).toEqual([]);
  expect(clean.status).toEqual({ code: 2 }); expect(clean.resource.attributes).toEqual({});
  expect(clean.instrumentationScope).toEqual({ name: UNKNOWN }); expect(JSON.stringify(raw)).toBe(before);
  expect(filter.span(clean).attributes).toEqual({ [HOST]: true });
});

test.each([100000, 110000])("R9 under-budget ordinary export stays populated and discovers a later credential (repeats=%i)", repeats => {
  const prose = "ordinary! ".repeat(repeats), secret = "synthetic-r9-discovery-password";
  const raw = fixture({ note: prose, password: secret, echo: secret });
  expect(getRegisteredSecrets()).not.toContain(secret);
  const clean = privacyFilter().span(raw);
  // PENDING I1xF2-PRECEDENCE: F2 scan bound precedes I1 long-prose retention.
  if (repeats === 100000) {
  expect(clean.attributes).toEqual({ note: prose, password: "<redacted>", echo: "<redacted>" });
  } else {
    expect(clean.attributes).toEqual({ note: "[image-redacted]", password: "<redacted>", echo: "<redacted>" });
  }
  expect(getRegisteredSecrets()).toContain(secret); expect(raw.attributes.echo).toBe(secret);
}, 30000);

test.each([65536, 131072])("R9 shared admission accounting still admits two long exception fields once (repeats=%i)", repeats => {
  const prose = "Note: m\n\n".repeat(repeats), value = `${prose}password=synthetic-r9-password\nend of message`;
  const clean = privacyFilter().admissionAttributes({ "exception.type": "Error", "exception.message": value, "exception.stacktrace": value });
  // PENDING I1xF2-PRECEDENCE: F2 scan bound precedes I1 long-prose retention.
  if (repeats === 65536) {
  expect(clean).toEqual({ "exception.type": "Error", "exception.message": `${prose}password="<redacted>"\nend of message`, "exception.stacktrace": `${prose}password="<redacted>"\nend of message` });
  } else {
    expect(clean).toEqual({ "exception.type": "Error", "exception.message": "[image-redacted]", "exception.stacktrace": "[image-redacted]" });
  }
}, 30000);
