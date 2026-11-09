import type { Attributes } from "@opentelemetry/api";
import { BasicTracerProvider, InMemorySpanExporter, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { privacyFilter, rememberSpanValueLimit } from "../export-privacy";
import { getRegisteredSecrets, registerSecret } from "../secrets";
import { FITracer } from "../trace/trace-config/FITracer";

// I1 r7 literal controls. Synthetic values only. No tests, builds or
// typechecks were run by this writer; RED/GREEN reasoning is in 02-tests.json.
const savedEnv = { ...process.env };
const secretKey = Symbol.for("traceai.secrets");
let savedRegistry: PropertyDescriptor | undefined;
beforeEach(() => {
  for (const key of Object.keys(process.env)) if (/^(FI_|OTEL_)/.test(key)) delete process.env[key];
  savedRegistry = Object.getOwnPropertyDescriptor(globalThis, secretKey);
  Reflect.deleteProperty(globalThis, secretKey);
});
afterEach(() => {
  jest.restoreAllMocks();
  process.env = { ...savedEnv };
  Reflect.deleteProperty(globalThis, secretKey);
  if (savedRegistry) Object.defineProperty(globalThis, secretKey, savedRegistry);
});

const UNKNOWN = "<unknown: host-truncated>";
const HOST_FLAG = "traceai.capture.host_data_unknown";
const COUNT = "traceai.capture.dropped_content_count";
const SECRET = "synthetic-r7-count-credential";

function fixture(attributes: Record<string, unknown> = { note: "synthetic r7 prose" }): any {
  const ctx = { traceId: "5".repeat(32), spanId: "6".repeat(16), traceFlags: 1 };
  return { _spanContext: ctx, spanContext: () => ctx, name: "synthetic r7 name", attributes,
    kind: 2, startTime: [1, 2], endTime: [3, 4], _duration: [2, 2], ended: true,
    droppedAttributesCount: 0, droppedEventsCount: 0, droppedLinksCount: 0,
    status: { code: 2, message: "synthetic r7 status" },
    resource: { attributes: { note: "synthetic r7 resource" } },
    instrumentationScope: { name: "synthetic r7 scope", attributes: { note: "synthetic r7 scope note" } },
    events: [{ name: "synthetic r7 event", time: [1, 2], attributes: { note: "synthetic r7 event note" } }],
    links: [{ context: ctx, attributes: { note: "synthetic r7 link note" } }] };
}
// The r6 FI-refusal view: structure and status code 2 survive; content does
// not; the host flag appears only with independent host-loss evidence.
function expectMinimal(clean: any, hostLoss: boolean): void {
  expect(clean.attributes).toEqual(hostLoss ? { [HOST_FLAG]: true } : {});
  expect(clean.name).toBe(UNKNOWN);
  expect(clean.status).toEqual({ code: 2 });
  expect(clean.events).toEqual([]);
  expect(clean.links).toEqual([]);
  expect(clean.resource.attributes).toEqual({});
  expect(clean.instrumentationScope).toEqual({ name: UNKNOWN });
  expect(clean.spanContext()).toEqual({ traceId: "5".repeat(32), spanId: "6".repeat(16), traceFlags: 1 });
}
function trap(): { execute: jest.Mock; proxy: object } {
  const execute = jest.fn(() => { throw new Error("synthetic r7 user code"); });
  const proxy = new Proxy({}, { get: execute, ownKeys: execute, getOwnPropertyDescriptor: execute, has: execute, getPrototypeOf: execute });
  return { execute, proxy };
}

// ---- R6 regression: round4 S1 nested proxy value (fixed in source) ----

test("R7 ROUND4 a nested proxy attribute VALUE keeps its type token, siblings export, no trap runs", () => {
  const { execute, proxy } = trap();
  const raw = fixture({ note: "synthetic r7 visible prose", proxy, nested: [proxy] });
  // Never JSON.stringify(raw) here: serialization would run the traps itself.
  const clean = privacyFilter().span(raw);
  expect(clean.attributes).toEqual({ note: "synthetic r7 visible prose", proxy: "object", nested: ["object"] });
  expect(clean.name).toBe("synthetic r7 name");
  expect(execute).not.toHaveBeenCalled();
});

test("R7 ROUND4 the same proxy as an attribute MAP is still refused without running a trap", () => {
  const { execute, proxy } = trap();
  const raw = fixture(); raw.events[0].attributes = proxy;
  expectMinimal(privacyFilter().span(raw), false);
  expect(execute).not.toHaveBeenCalled();
});

// ---- R6-intent pins for the four expectation-conflict scenarios (03b rows) ----

test("R7 PIN i1-r5 A-001: refusal with observed host loss keeps status 2 and the host flag after discovering a later credential", () => {
  const raw = fixture(Object.fromEntries(Array.from({ length: 5000 }, (_, i) => [`k${i}`, "x"])));
  raw.droppedAttributesCount = 1;
  const secret = "r7-later-link-synthetic-amber-fig";
  raw.links[0].attributes.api_key = secret;
  expect(getRegisteredSecrets()).not.toContain(secret);
  const clean = privacyFilter().span(raw);
  expect(getRegisteredSecrets()).toContain(secret);
  expectMinimal(clean, true);
  expect(clean.droppedAttributesCount).toBe(1);
});

test("R7 PIN i1-r5 B-001: hidden-key refusal without host evidence is FI refusal, snapshotted once", () => {
  const attrs = {};
  for (let i = 0; i < 5000; i++) Object.defineProperty(attrs, `hidden${i}`, { value: "hidden prose", enumerable: false });
  const raw = fixture(attrs), ownKeys = Reflect.ownKeys;
  let snapshots = 0;
  const spy = jest.spyOn(Reflect, "ownKeys").mockImplementation(value => { if (value === attrs) snapshots++; return ownKeys(value); });
  let clean: any;
  try { clean = privacyFilter().span(raw); } finally { spy.mockRestore(); }
  expect(snapshots).toBe(1);
  expectMinimal(clean, false);
});

test("R7 PIN i1-r3 R3: shared discovery exhaustion is FI refusal and a later short copy stays minimal", () => {
  const chunk = "ordinary prose! ".repeat(4096), shared = { "gen_ai.operation.name": chunk };
  const raw = fixture({});
  raw.events = Array.from({ length: 128 }, () => ({ name: "event", attributes: { ...shared } }));
  raw.links = Array.from({ length: 128 }, () => ({ context: raw._spanContext, attributes: { ...shared } }));
  expectMinimal(privacyFilter().span(raw), false);
  const mapped = { ...raw, attributes: { note: "short copy", "session.id": "short" }, events: [], links: [] };
  expectMinimal(privacyFilter().span(mapped), false);
});

test.each([{ values: 5, minimal: true }, { values: 3, minimal: false }])(
  "R7 PIN i1-dest-r2 R2: $values shared 1.1M-char event/link values, later copy minimal=$minimal", ({ values, minimal }) => {
    const chunk = "ordinary! ".repeat(110000), native = fixture({});
    native.events = Array.from({ length: Math.min(values, 3) }, (_, i) => ({ name: `event ${i}`, time: [1, 2], attributes: { note: chunk } }));
    native.links = Array.from({ length: Math.max(values - 3, 0) }, () => ({ context: native._spanContext, attributes: { note: chunk } }));
    rememberSpanValueLimit(native);
    const copy = { ...native, attributes: { note: "short copy" }, events: [], links: [] };
    const clean = privacyFilter().span(copy);
    // 3 values fit one 4 MiB ledger; 5 do not. Only a SHARED budget separates them.
    if (minimal) expectMinimal(clean, false);
    else expect(clean.attributes).toEqual({ note: "short copy" });
    expect(native.events[0].attributes.note).toBe(chunk);
  });

// ---- COMPILE (i1-r4-privacy TS2339): runtime shape only ----

test("R7 COMPILE detached scope attributes exist at runtime; the type-level check is the i1-r4-privacy suite compile", () => {
  const clean = privacyFilter().span(fixture());
  const scope = clean.instrumentationScope as unknown as { name: string; attributes?: Attributes };
  expect(scope.name).toBe("synthetic r7 scope");
  expect(scope.attributes).toEqual({ note: "synthetic r7 scope note" });
});

// ---- AUTO count bridge ----

async function exported(auto: boolean, write: (tracer: FITracer) => void): Promise<{ native: any; clean: any; before: string }> {
  const memory = new InMemorySpanExporter();
  const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(memory)] });
  try {
    write(new FITracer({ tracer: (auto ? FITracer.autoProvider(provider) : provider).getTracer("r7-count") }));
    const native = memory.getFinishedSpans().at(-1)!, before = JSON.stringify(native.attributes);
    return { native, clean: privacyFilter().span(native), before };
  } finally { await provider.shutdown(); }
}

test.each(["start", "updates", "mixed"])("R7 AUTO three FI redactions (%s) export count 3; manual exports no count", async schedule => {
  registerSecret(SECRET);
  for (const auto of [false, true]) {
    const { native, clean, before } = await exported(auto, tracer => {
      const start: Attributes = schedule === "start" ? { first: SECRET, second: SECRET, third: SECRET }
        : schedule === "mixed" ? { first: SECRET, second: SECRET, kept: "synthetic r7 kept" } : {};
      const span = tracer.startSpan("synthetic r7 count", { attributes: start });
      if (schedule === "updates") { span.setAttribute("first", SECRET); span.setAttributes({ second: SECRET, third: SECRET }); }
      if (schedule === "mixed") span.setAttribute("third", SECRET);
      span.end();
    });
    expect(clean.attributes.first).toBe("<redacted>");
    expect(clean.attributes.second).toBe("<redacted>");
    expect(clean.attributes.third).toBe("<redacted>");
    expect(clean.attributes[COUNT]).toBe(auto ? 3 : undefined);
    expect(clean.attributes[HOST_FLAG]).toBeUndefined();
    // The count lives only on the detached view; native storage is untouched.
    expect(native.attributes[COUNT]).toBeUndefined();
    expect(JSON.stringify(native.attributes)).toBe(before);
  }
});

test("R7 AUTO repeated FI redactions of one key count per write; re-export does not double; clean writes add no count", async () => {
  registerSecret(SECRET);
  const repeated = await exported(true, tracer => {
    const span = tracer.startSpan("synthetic r7 repeated");
    span.setAttribute("first", SECRET); span.setAttribute("first", SECRET); span.end();
  });
  expect(repeated.clean.attributes).toEqual({ first: "<redacted>", [COUNT]: 2 });
  expect(privacyFilter().span(repeated.clean).attributes[COUNT]).toBe(2);
  const unchanged = await exported(true, tracer => {
    const span = tracer.startSpan("synthetic r7 unchanged", { attributes: { note: "synthetic r7 kept" } });
    span.setAttributes({ other: "synthetic r7 kept too", absent: undefined }); span.end();
  });
  expect(unchanged.clean.attributes).toEqual({ note: "synthetic r7 kept", other: "synthetic r7 kept too" });
});

// ---- ElevenLabs byte COUNT metric ----

test.each([9, 6, 3, 16777216])("R7 ELEVENLABS elevenlabs.audio.bytes=%i stays a number through admission, standalone FISpan and export", async bytes => {
  const filter = privacyFilter();
  const attrs = { "elevenlabs.audio.bytes": bytes, "elevenlabs.audio.chunks": 2, "elevenlabs.completion": "complete" };
  expect(filter.admissionAttributes(attrs)).toEqual(attrs);
  expect(filter.span(fixture(attrs)).attributes).toEqual(attrs);
  const memory = new InMemorySpanExporter();
  const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(memory)] });
  try {
    const span = new FITracer({ tracer: provider.getTracer("@traceai/elevenlabs") }).startSpan("synthetic r7 speech");
    span.setAttribute("elevenlabs.audio.chunks", 2);
    span.setAttribute("elevenlabs.audio.bytes", bytes);
    span.end();
    expect(memory.getFinishedSpans()[0].attributes["elevenlabs.audio.bytes"]).toBe(bytes);
    expect(memory.getFinishedSpans()[0].attributes["elevenlabs.audio.chunks"]).toBe(2);
  } finally { await provider.shutdown(); }
});

test("R7 ELEVENLABS real media payloads, other members, aliases and explicit media context stay omitted", () => {
  const filter = privacyFilter();
  expect(filter.admissionAttributes({ "elevenlabs.audio.bytes": [1, 2, 3] })).toEqual({ "elevenlabs.audio.bytes": "<omitted: media>" });
  expect(filter.admissionAttributes({ "elevenlabs.audio.data": 9 })).toEqual({ "elevenlabs.audio.data": "<omitted: media>" });
  expect(filter.admissionAttributes({ "x.elevenlabs.audio.bytes": 9 })).toEqual({ "x.elevenlabs.audio.bytes": "<omitted: media>" });
  // PENDING Z-OVER: F2 cap-0 whole-scope replacement precedes I1 media markers.
  expect(filter.admissionAttributes({ "elevenlabs.audio.bytes": "iVBORw0KGgoAAAANSUhEUg==" })).toEqual({ "elevenlabs.audio.bytes": "[image-redacted]" });
  const positive = privacyFilter({ base64ImageMaxLength: 32000, hideInputImages: true });
  expect(positive.admissionAttributes({ "elevenlabs.audio.bytes": "iVBORw0KGgoAAAANSUhEUg==" })).toEqual({ "elevenlabs.audio.bytes": "<omitted: 24 base64 chars>" });
  expect(filter.admissionAttributes({ "elevenlabs.audio.mime_type": "audio/mpeg", "elevenlabs.audio.bytes": 9 })["elevenlabs.audio.bytes"]).toBe("<omitted: media>");
});
