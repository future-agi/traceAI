import { afterEach, beforeEach, describe, expect, test } from "@jest/globals";
import { type Attributes } from "@opentelemetry/api";
import { BasicTracerProvider, InMemorySpanExporter, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { privacyFilter } from "../export-privacy";
import { scrubValue } from "../privacy-runtime";
import { FITracer } from "../trace/trace-config/FITracer";
import { FISpan } from "../trace/trace-config/FISpan";
import { capBase64Images } from "../trace/trace-config/imageCap";
import { generateTraceConfig } from "../trace/trace-config/traceConfig";

// Authored before the production overlay. RED/GREEN execution belongs to E2B.
// Valid synthetic 58-byte, 1x1, 24-bit BMP: 14-byte header, 40-byte DIB and
// one padded red BGR pixel. No catalog magic matches; below the 256-char heuristic.
const BMP_1X1 = "Qk06AAAAAAAAADYAAAAoAAAAAQAAAAEAAAABABgAAAAAAAQAAAAAAAAAAAAAAAAAAAAAAAAAAAD/AA==";
const OMITTED = "<omitted: 80 base64 chars>";
const AUDIO = "AQIDBA==";
const saved = { ...process.env };
beforeEach(() => { for (const key of Object.keys(process.env)) if (/^(FI_|OTEL_)/.test(key)) delete process.env[key]; });
afterEach(() => { process.env = { ...saved }; });

function snapshot(attributes: Attributes): any {
  const ctx = { traceId: "1".repeat(32), spanId: "2".repeat(16), traceFlags: 1 };
  return { name: "typed image fixture", kind: 2, spanContext: () => ctx, attributes,
    startTime: [1, 0], endTime: [2, 0], duration: [1, 0], ended: true, status: { code: 1 },
    resource: { attributes: {} }, instrumentationScope: { name: "fixture" }, events: [], links: [] };
}

const ROUTES = ["admission", "detached-export", "tracer-create", "span-set", "span-set-many", "direct-span-set"] as const;
type Route = typeof ROUTES[number];
async function through(route: Route, attributes: Attributes, cap = 32000): Promise<Attributes> {
  const options = { base64ImageMaxLength: cap };
  const before = JSON.stringify(attributes);
  if (route === "admission") {
    const clean = privacyFilter(options).admissionAttributes(attributes);
    expect(JSON.stringify(attributes)).toBe(before);
    return clean;
  }
  if (route === "detached-export") {
    const raw = snapshot(attributes), rawBefore = JSON.stringify(raw);
    const clean = privacyFilter(options).span(raw);
    expect(JSON.stringify(raw)).toBe(rawBefore);
    expect(clean.name).toBe("typed image fixture");
    return clean.attributes;
  }
  // No privacy-filter processor: native capture proves standalone write-time policy.
  const exporter = new InMemorySpanExporter();
  const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
  try {
    const span = route === "direct-span-set"
      ? new FISpan({ span: provider.getTracer("fixture").startSpan("typed image fixture"), config: generateTraceConfig(options) })
      : new FITracer({ tracer: provider.getTracer("fixture"), traceConfig: options })
        .startSpan("typed image fixture", route === "tracer-create" ? { attributes } : undefined);
    if (route === "span-set" || route === "direct-span-set") {
      for (const [key, value] of Object.entries(attributes)) if (value !== undefined) span.setAttribute(key, value);
    } else if (route === "span-set-many") span.setAttributes(attributes);
    span.end();
    expect(exporter.getFinishedSpans()).toHaveLength(1);
    const native = exporter.getFinishedSpans()[0];
    expect(JSON.stringify(attributes)).toBe(before);
    return native.attributes;
  } finally { await provider.shutdown(); }
}

const DECLARATIONS: Array<[string, Record<string, string>]> = [
  ["type image", { type: "image" }],
  ["type image_url", { type: "image_url" }],
  ["type input_image", { type: "input_image" }],
  ["modality image without MIME", { type: "blob", modality: "image" }],
  ["media_type image", { media_type: "image/bmp" }],
  ["mime_type image", { mime_type: "image/bmp" }],
  ["mimeType image", { mimeType: "image/bmp" }],
  ["mediaType image", { mediaType: "image/bmp" }],
  ["content_type image", { content_type: "image/bmp" }],
  // Other image media types on every route.
  ["mime_type image/png", { mime_type: "image/png" }],
  ["mime_type image/jpeg", { mime_type: "image/jpeg" }],
  ["mime_type image/gif", { mime_type: "image/gif" }],
  ["mime_type image/webp", { mime_type: "image/webp" }],
  ["mime_type image/heic", { mime_type: "image/heic" }],
  ["mime_type image/svg+xml", { mime_type: "image/svg+xml" }],
  ["mime_type image/tiff", { mime_type: "image/tiff" }],
  ["mime_type image/avif", { mime_type: "image/avif" }],
];
const PAYLOAD_FIELDS = ["data", "bytes", "content", "image", "image_url", "url", "base64", "b64_json"];
function body(declaration: Record<string, string>, payload: string) {
  return { ...declaration, ...Object.fromEntries(PAYLOAD_FIELDS.map(key => [key, payload])), label: "ordinary sibling", width: 1 };
}

// Each declaration has its own no-hide test on every route: MIME must not hide a
// missing type/modality branch. The same fixture covers every supported payload key.
describe.each(ROUTES)("typed short image via %s", route => {
  test.each(DECLARATIONS)("positive cap: %s omits all payload fields", async (_label, declaration) => {
    const input = body(declaration, BMP_1X1), raw = JSON.stringify(input);
    const attrs = { "output.value": raw, ordinary: "ordinary attribute" };
    expect(BMP_1X1).toHaveLength(80);
    expect(capBase64Images(raw, 32000)).toBe(raw);
    const clean = await through(route, attrs);
    expect(clean).toEqual({ "output.value": JSON.stringify(body(declaration, OMITTED)), ordinary: "ordinary attribute" });
    expect(JSON.stringify(clean)).not.toContain(BMP_1X1);
    expect(JSON.stringify(clean)).not.toContain(BMP_1X1.slice(0, 24));
    expect(input).toEqual(body(declaration, BMP_1X1));
  });

  test.each([0, 1, 79, 80, 81, 32000])("cap %i retains marker precedence", async cap => {
    const raw = JSON.stringify({ type: "image", data: BMP_1X1, label: "ordinary sibling" });
    const marker = cap === 0 ? "[image-redacted]" : cap < 80 ? "__REDACTED__" : OMITTED;
    const clean = await through(route, { "output.value": raw }, cap);
    expect(clean["output.value"]).toBe(JSON.stringify({ type: "image", data: marker, label: "ordinary sibling" }));
    expect(JSON.stringify(clean)).not.toContain(BMP_1X1);
  });

  test("audio omission and audio URL/transcript metadata are unchanged", async () => {
    const input = { type: "input_audio", data: AUDIO, url: "https://example.invalid/audio.wav", transcript: { content: "ordinary words" } };
    const clean = await through(route, { "output.value": JSON.stringify(input) });
    expect(clean["output.value"]).toBe(JSON.stringify({ ...input, data: "<omitted: 8 base64 chars>" }));
  });

  test("non-image typed values and ordinary HTTP image URLs are kept", async () => {
    const input = [{ type: "text", data: AUDIO }, { type: "blob", modality: "text", data: AUDIO },
      { type: "image_url", image_url: { url: "https://example.invalid/image.bmp" } }];
    const attrs = { "output.value": JSON.stringify(input) };
    expect(await through(route, attrs)).toEqual(attrs);
  });
});

test.each(["source", "inlineData", "inline_data", "image_url", "image"])("image context reaches %s payload objects without mutating the source", wrapper => {
  const input = { type: "image", [wrapper]: { data: BMP_1X1, url: BMP_1X1, label: "ordinary sibling" } };
  const before = JSON.stringify(input);
  const expected = { type: "image", [wrapper]: { data: OMITTED, url: OMITTED, label: "ordinary sibling" } };
  expect(scrubValue(input)).toEqual(expected);
  expect(privacyFilter({ base64ImageMaxLength: 32000 }).admissionAttributes({ "output.value": before })["output.value"])
    .toBe(JSON.stringify(expected));
  expect(JSON.stringify(input)).toBe(before);
});

test.each(["admission", "detached-export"] as const)("%s indexes flattened modality/MIME hints without crossing array siblings", async route => {
  const attrs = { "parts.0.modality": "image", "parts.0.url": BMP_1X1,
    "parts.1.modality": "text", "parts.1.url": BMP_1X1,
    "parts.2.mime_type": "image/bmp", "parts.2.image_url": BMP_1X1 };
  expect(await through(route, attrs)).toEqual({ ...attrs, "parts.0.url": OMITTED, "parts.2.image_url": OMITTED });
});

test.each(["type", "modality", "mime_type"])("authored %s image hints survive separate auto writes", async key => {
  const exporter = new InMemorySpanExporter();
  const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
  try {
    const span = new FITracer({ tracer: FITracer.autoProvider(provider).getTracer("fixture"), traceConfig: { base64ImageMaxLength: 32000 } })
      .startSpan("typed image fixture");
    span.setAttribute(`part.${key}`, key === "mime_type" ? "image/bmp" : "image");
    span.setAttribute("part.image_url", BMP_1X1);
    span.end();
    expect(exporter.getFinishedSpans()).toHaveLength(1);
    expect(exporter.getFinishedSpans()[0].attributes["part.image_url"]).toBe(OMITTED);
  } finally { await provider.shutdown(); }
});

test("detached export protects events, status, links, resources and scope attributes", () => {
  const raw = JSON.stringify({ type: "image", data: BMP_1X1, label: "ordinary sibling" });
  const expected = JSON.stringify({ type: "image", data: OMITTED, label: "ordinary sibling" });
  const source = snapshot({ "output.value": raw });
  source.status = { code: 2, message: raw };
  source.events = [{ name: "image event", time: [1, 0], attributes: { body: raw } }];
  source.links = [{ context: source.spanContext(), attributes: { body: raw } }];
  source.resource.attributes = { body: raw };
  source.instrumentationScope.attributes = { body: raw };
  const before = JSON.stringify(source), clean = privacyFilter({ base64ImageMaxLength: 32000, hideErrorDetails: false }).span(source);
  expect(clean.attributes["output.value"]).toBe(expected);
  expect(clean.status.message).toBe(expected);
  expect(clean.events[0].attributes?.body).toBe(expected);
  expect(clean.links[0].attributes?.body).toBe(expected);
  expect(clean.resource.attributes.body).toBe(expected);
  expect((clean.instrumentationScope as { attributes?: Attributes }).attributes?.body).toBe(expected);
  expect(JSON.stringify(clean)).not.toContain(BMP_1X1);
  expect(JSON.stringify(source)).toBe(before);
});

test("credential names and exact secrets still precede media-only sentinels", () => {
  // Fixed value-free markers, not fabricated credentials or a name-only exemption.
  for (const marker of ["__REDACTED__", "[image-redacted]"]) {
    expect(scrubValue({ type: "image", data: marker, password: marker })).toEqual({ type: "image", data: marker, password: "<redacted>" });
    // wholeSecret has excluded only this value-free hide sentinel since 1.2.0.
    if (marker === "__REDACTED__") {
      expect(scrubValue({ type: "image", data: marker }, [marker])).toEqual({ type: "image", data: "__REDACTED__" });
    } else {
      expect(scrubValue({ type: "image", data: marker }, [marker])).toEqual({ type: "image", data: "<redacted>" });
    }
  }
  // The valid BMP is a local exact-secret fixture only; never register it globally.
  expect(scrubValue({ type: "image", data: BMP_1X1 }, [BMP_1X1])).toEqual({ type: "image", data: "<redacted>" });
});

// FICORE-121-R2: RED-first rows authored before the video runtime overlay.
// These four synthetic bytes intentionally have no media magic. The declaration,
// not a file header, must establish omission. No credential-shaped values added.
const MEDIA_BYTES = "AQIDBA==";
const MEDIA_OMITTED = "<omitted: 8 base64 chars>";
const MEDIA_DECLARATIONS: Array<[string, Record<string, string>]> = [
  ["type video", { type: "video" }],
  ["type input_video", { type: "input_video" }],
  ["modality video", { modality: "video" }],
  ["MIME video/mp4", { mime_type: "video/mp4" }],
  ["MIME video/webm", { mime_type: "video/webm" }],
  ["MIME application/pdf", { mime_type: "application/pdf" }],
];
const MEDIA_PAYLOAD_FIELDS = ["data", "bytes", "content", "value", "body", "base64", "b64_json", "file_data", "audio_base64"];
const MEDIA_CAPS = [0, 1, 7, 8, 9, 32000];

describe.each(ROUTES)("declared non-audio media via %s", route => {
  describe.each(MEDIA_DECLARATIONS)("%s", (_label, declaration) => {
    test.each(MEDIA_CAPS)("data omitted at cap %i without an image marker", async cap => {
      const input = { ...declaration, data: MEDIA_BYTES, label: "ordinary sibling" };
      const raw = JSON.stringify(input);
      // The image cap must not be the reason video/PDF bytes disappear.
      expect(capBase64Images(raw, cap)).toBe(raw);
      const clean = await through(route, { "output.value": raw, ordinary: "ordinary attribute" }, cap);
      expect(clean).toEqual({ "output.value": JSON.stringify({ ...input, data: MEDIA_OMITTED }), ordinary: "ordinary attribute" });
      expect(JSON.stringify(clean)).not.toContain(MEDIA_BYTES);
      expect(JSON.stringify(input)).toBe(raw);
    });

    test("all MIME payload fields and wrapped descendants omit below the cap", async () => {
      const payloads = Object.fromEntries(MEDIA_PAYLOAD_FIELDS.map(key => [key, MEDIA_BYTES]));
      const omitted = Object.fromEntries(MEDIA_PAYLOAD_FIELDS.map(key => [key, MEDIA_OMITTED]));
      const metadata = { label: "ordinary sibling", url: "https://example.invalid/media", transcript: { content: "ordinary words" } };
      const input = { ...declaration, ...payloads, source: { inlineData: { data: { chunk: [MEDIA_BYTES] } } }, ...metadata };
      const expected = { ...declaration, ...omitted, source: { inlineData: { data: { chunk: [MEDIA_OMITTED] } } }, ...metadata };
      const before = JSON.stringify(input);
      expect(scrubValue(input)).toEqual(expected);
      expect(await through(route, { "output.value": before })).toEqual({ "output.value": JSON.stringify(expected) });
      expect(JSON.stringify(input)).toBe(before);
    });

    test("flattened hints survive separate writes and stay within their indexed parent", async () => {
      const hint = Object.fromEntries(Object.entries(declaration).map(([key, value]) => [`parts.0.${key}`, value]));
      // Hint first: setter routes must retain tightening context for the next write.
      const attrs = { ...hint, "parts.0.data": MEDIA_BYTES, "parts.1.data": MEDIA_BYTES,
        "parts.0.transcript.content": "ordinary words", "parts.0.url": "https://example.invalid/media" };
      expect(await through(route, attrs)).toEqual({ ...attrs, "parts.0.data": MEDIA_OMITTED });
    });
  });

  test.each(MEDIA_CAPS)("audio control remains omitted with metadata at cap %i", async cap => {
    const input = { type: "input_audio", data: AUDIO, url: "https://example.invalid/audio.wav", transcript: { content: "ordinary words" } };
    expect(await through(route, { "output.value": JSON.stringify(input) }, cap))
      .toEqual({ "output.value": JSON.stringify({ ...input, data: MEDIA_OMITTED }) });
  });

  test("unrecognized declarations do not grant media context", async () => {
    const input = [{ type: "text", data: MEDIA_BYTES }, { type: "blob", modality: "text", data: MEDIA_BYTES }];
    const attrs = { "output.value": JSON.stringify(input) };
    expect(await through(route, attrs)).toEqual(attrs);
  });
});

test.each(MEDIA_DECLARATIONS)("%s protects all detached export surfaces", (_label, declaration) => {
  const raw = JSON.stringify({ ...declaration, data: MEDIA_BYTES, label: "ordinary sibling" });
  const expected = JSON.stringify({ ...declaration, data: MEDIA_OMITTED, label: "ordinary sibling" });
  const source = snapshot({ "output.value": raw });
  source.status = { code: 2, message: raw };
  source.events = [{ name: "media event", time: [1, 0], attributes: { body: raw } }];
  source.links = [{ context: source.spanContext(), attributes: { body: raw } }];
  source.resource.attributes = { body: raw };
  source.instrumentationScope.attributes = { body: raw };
  const before = JSON.stringify(source);
  const clean = privacyFilter({ base64ImageMaxLength: 32000, hideErrorDetails: false }).span(source);
  expect(clean.attributes["output.value"]).toBe(expected);
  expect(clean.status.message).toBe(expected);
  expect(clean.events[0].attributes?.body).toBe(expected);
  expect(clean.links[0].attributes?.body).toBe(expected);
  expect(clean.resource.attributes.body).toBe(expected);
  expect((clean.instrumentationScope as { attributes?: Attributes }).attributes?.body).toBe(expected);
  expect(JSON.stringify(clean)).not.toContain(MEDIA_BYTES);
  expect(JSON.stringify(source)).toBe(before);
});

test.each(MEDIA_DECLARATIONS)("%s keeps credential and sentinel precedence", (_label, declaration) => {
  expect(scrubValue({ ...declaration, data: MEDIA_BYTES }, [MEDIA_BYTES])).toEqual({ ...declaration, data: "<redacted>" });
  for (const marker of ["__REDACTED__", "[image-redacted]"]) {
    expect(scrubValue({ ...declaration, data: marker, password: marker }))
      .toEqual({ ...declaration, data: marker, password: "<redacted>" });
  }
});

test.each(["type", "modality"])("video %s array/indexed hints combine without granting safe MIME evidence", key => {
  const filter = privacyFilter({ base64ImageMaxLength: 32000 });
  const attrs = { [`parts[0].${key}[0]`]: "video", "parts.0.data": MEDIA_BYTES,
    [`parts.1.${key}`]: ["text", "video"], "parts.1.data": MEDIA_BYTES, "parts.2.data": MEDIA_BYTES };
  expect(filter.admissionAttributes(attrs)).toEqual({ ...attrs, "parts.0.data": MEDIA_OMITTED, "parts.1.data": MEDIA_OMITTED });
  expect(filter.admissionHints({ [`parts.0.${key}`]: "video" }).get("parts.0"))
    .toEqual([true, false, false, false]);
});
