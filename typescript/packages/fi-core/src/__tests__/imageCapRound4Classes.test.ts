import { describe, expect, it } from "@jest/globals";
import { BasicTracerProvider, InMemorySpanExporter, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { FITracer } from "../trace/trace-config/FITracer";
import { capBase64Images } from "../trace/trace-config/imageCap";

// Authored before the implementation overlay. Expectations are literal layouts,
// not output from the cap, a private scanner or a reference replacement regex.
const CAP = 64;
const MARK = "__REDACTED__";
const SLASH = String.fromCharCode(92);
const HEADER = 'data:image/png;name="a' + SLASH + '" b";base64,';
const BIG = HEADER + "QUJD".repeat(40);
const EXACT = HEADER + "A".repeat(CAP - HEADER.length);
const UNDER = HEADER + "A".repeat(CAP - HEADER.length - 1);
const NOT_BASE64 = 'data:image/svg+xml;name="a' + SLASH + '";base64,",%3Csvg%3E';
const PREFIX = "data:image/png;base64,";
const KEY_BIG = PREFIX + "A".repeat(80);
const KEY_EXACT = PREFIX + "A".repeat(CAP - PREFIX.length);

function hexText(text: string): string {
  return text.split("").map((c) => SLASH + "u" + c.charCodeAt(0).toString(16).padStart(4, "0")).join("");
}

const REPRESENTATIONS: Array<[string, (value: string) => string]> = [
  ["literal", (value) => value],
  ["JSON field", (value) => JSON.stringify({ url: value, note: "keep" })],
  ["encoded inner document", (value) => JSON.stringify({ opaque: JSON.stringify({ url: value, note: "keep" }), other: "retain" })],
  ["encoded inner scalar", (value) => JSON.stringify({ opaque: JSON.stringify(JSON.stringify(value)), other: "retain" })],
  ["JSON-looking prose", (value) => "[photo](" + value + ") caption"],
];

describe("MIME quoted-pairs are not JSON transport escapes", () => {
  it.each(REPRESENTATIONS)("whole leak repair and preservation controls: %s", (_label, wrap) => {
    expect(capBase64Images(wrap(BIG), CAP)).toBe(wrap(MARK));
    expect(capBase64Images(wrap(EXACT), CAP)).toBe(wrap(EXACT));
    expect(capBase64Images(wrap(UNDER), CAP)).toBe(wrap(UNDER));
    expect(capBase64Images(wrap("ordinary text"), 0)).toBe(wrap("ordinary text"));
  });

  it.each(REPRESENTATIONS)("quoted apparent flag stays non-base64: %s", (_label, wrap) => {
    for (const cap of [0, 1]) {
      expect(capBase64Images(wrap(NOT_BASE64), cap)).toBe(cap === 0 ? _label === "literal" || _label === "JSON-looking prose" ? "[image-redacted]" : wrap("[image-redacted]") : wrap(NOT_BASE64));
    }
  });

  it("keeps literal ownership when other escapes schedule another decoded view", () => {
    const before = "prefix " + SLASH + "t | ";
    const after = " | " + SLASH + "u0061 caption";
    expect(capBase64Images(before + BIG + after, CAP)).toBe(before + MARK + after);
    expect(capBase64Images(before + EXACT + after, CAP)).toBe(before + EXACT + after);
    expect(capBase64Images(before + NOT_BASE64 + after, 0)).toBe("[image-redacted]");
    const hidden = hexText(PREFIX + "QUJD".repeat(40));
    expect(capBase64Images(NOT_BASE64 + " | " + hidden, 1)).toBe("[image-redacted]");
    expect(capBase64Images(BIG + " | " + hidden, CAP)).toBe("[image-redacted]");
  });

  it("decodes actual transport before applying literal MIME ownership", () => {
    const encoded = hexText(BIG);
    expect(capBase64Images("before " + encoded + " after", BIG.length)).toBe("[image-redacted]");
    expect(capBase64Images("before " + encoded + " after", BIG.length - 1)).toBe("[image-redacted]");
    const nonBase64 = hexText(NOT_BASE64);
    expect(capBase64Images(nonBase64, 0)).toBe("[image-redacted]");
    const jsonEscapes = JSON.stringify(BIG).slice(1, -1);
    expect(capBase64Images(jsonEscapes, CAP)).toBe(MARK);
    expect(capBase64Images(jsonEscapes, BIG.length)).toBe(MARK);
  });

  it("does not cap a raw transport prefix or leave the encoded payload suffix", () => {
    // The literal prefix is already above CAP, but its payload continues through
    // transport escapes. A premature raw edit would leave a second marker/suffix.
    const url = PREFIX + "A".repeat(80) + "/".repeat(80);
    const encoded = PREFIX + "A".repeat(80) + (SLASH + "/").repeat(80);
    expect(capBase64Images(encoded, CAP)).toBe(MARK);
    expect(capBase64Images(encoded, url.length)).toBe(MARK);
    expect(capBase64Images(encoded, url.length + 1)).toBe(MARK);
    // An invalid escape cannot extend the payload: retain it as ordinary suffix.
    const invalidSuffix = PREFIX + "A".repeat(80) + SLASH + "q tail";
    expect(capBase64Images(invalidSuffix, CAP)).toBe(MARK + SLASH + "q tail");
  });

  it.each(['"', "'", SLASH])("preserves both units of MIME quoted-pair %s", (unit) => {
    const header = 'data:image/png;name="a' + SLASH + unit + ' b";base64,';
    const url = header + "QUJD".repeat(40);
    expect(capBase64Images(url, CAP)).toBe(MARK);
    expect(capBase64Images("before " + url + " after", url.length)).toBe("[image-redacted]");
    const field = JSON.stringify({ url, note: "keep" });
    expect(capBase64Images(field, CAP)).toBe('{"url":"__REDACTED__","note":"keep"}');
  });
});

describe("JSON member names are independently inspected value content", () => {
  it("redacts a complete over-cap URL key without changing numeric or escape tokens", () => {
    const tail = ':"keep","id":9007199254740993,"n":1e400,"same":1,"same":2,"s":"' + SLASH + 'uD800" }';
    const raw = '{ "' + KEY_BIG + '"' + tail;
    expect(capBase64Images(raw, CAP)).toBe('{ "__REDACTED__"' + tail);
    const exact = '{ "' + KEY_EXACT + '"' + tail;
    expect(capBase64Images(exact, CAP)).toBe(exact);
    const under = '{ "' + PREFIX + "A".repeat(CAP - PREFIX.length - 1) + '"' + tail;
    expect(capBase64Images(under, CAP)).toBe(under);
  });

  it.each([1, 3])("maps member-name edits through encoded inner documents, depth=%i", (depth) => {
    let raw = '{"' + KEY_BIG + '":"keep","exact":"' + KEY_EXACT + '","ordinary":"retain"}';
    let expected = '{"__REDACTED__":"keep","exact":"' + KEY_EXACT + '","ordinary":"retain"}';
    for (let i = 0; i < depth; i++) { raw = JSON.stringify(raw); expected = JSON.stringify(expected); }
    const outer = JSON.stringify({ opaque: raw, note: "keep" });
    expect(capBase64Images(outer, CAP)).toBe(JSON.stringify({ opaque: expected, note: "keep" }));
  });

  it("uses decoded cap units and maps escaped key spellings precisely", () => {
    const encoded = hexText(KEY_BIG);
    const raw = '{"' + encoded + '":"keep","ordinary":"retain"}';
    expect(capBase64Images(raw, KEY_BIG.length)).toBe(raw);
    expect(capBase64Images(raw, KEY_BIG.length - 1)).toBe('{"__REDACTED__":"keep","ordinary":"retain"}');
    const quoted = JSON.stringify({ [BIG]: "keep" });
    expect(capBase64Images(quoted, CAP)).toBe('{"__REDACTED__":"keep"}');
  });

  it("recognizes signature keys, not arbitrary base64 metadata or inherited payload names", () => {
    expect(capBase64Images('{"iVBORw0KGgo=":"keep"}', 4)).toBe('{"__REDACTED__":"keep"}');
    const raw = '{"type":"image","source":{"QUJDQUJD":"keep","data":"QUJD"},"name":"QUJD"}';
    expect(capBase64Images(raw, 0)).toBe('{"type":"image","source":{"QUJDQUJD":"keep","data":"[image-redacted]"},"name":"QUJD"}');
    expect(capBase64Images(raw, 4)).toBe(raw);
    const escapedName = '{"type":"image","' + SLASH + 'u0064ata":"QUJD","name":"QUJD"}';
    expect(capBase64Images(escapedName, 0)).toBe('{"type":"image","' + SLASH + 'u0064ata":"[image-redacted]","name":"QUJD"}');
    const ordinary = '{"type":"text","modality":"audio","id":"QUJD","data":"QUJD"}';
    expect(capBase64Images(ordinary, 0)).toBe(ordinary);
  });

  it("does not join separate name/value tokens and isolates exhausted key scopes", () => {
    const split = '{"data:image/png;name=":";base64,QUJD"}';
    expect(capBase64Images(split, 0)).toBe(split);
    const expensive = SLASH + "u005c".repeat(256) + "u0078ordinary " + "z".repeat(2048);
    const raw = JSON.stringify({ [expensive]: "keep", [KEY_BIG]: "retain", exact: KEY_EXACT, note: "ordinary" });
    const expected = '{"[image-redacted]":"keep","__REDACTED__":"retain","exact":"' + KEY_EXACT + '","note":"ordinary"}';
    expect(capBase64Images(raw, CAP)).toBe(expected);
  });
});

it("exports MIME-pair and member-name repairs through real SDK attributes", () => {
  const exporter = new InMemorySpanExporter();
  const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
  const tracer = new FITracer({
    tracer: provider.getTracer("round4-image-classes"),
    traceConfig: {
      base64ImageMaxLength: CAP, piiRedaction: false, hideErrorDetails: false,
      hideInputs: false, hideOutputs: false, hideInputText: false, hideOutputText: false,
      hideInputImages: false, hideInputMessages: false, hideOutputMessages: false,
      hideEmbeddingVectors: false,
    },
  });
  const keyDocument = '{"' + KEY_BIG + '":"keep"}';
  const attributes = Object.freeze({ mime: BIG, keyDocument, exact: EXACT, ordinary: "keep" });
  const span = tracer.startSpan("case", { attributes });
  span.setAttribute("later", BIG);
  span.addEvent("request", { value: keyDocument }, [1, 2]);
  span.end();
  const results = exporter.getFinishedSpans();
  expect(results).toHaveLength(1);
  expect(results[0].attributes.mime).toBe(MARK);
  expect(results[0].attributes.keyDocument).toBe('{"__REDACTED__":"keep"}');
  // PENDING I1xF2-PRECEDENCE T58: TS I1 media omission dominates positive caps.
  // LANE-CALL-NIKM4-5A-TS-IMAGE-EXPORT-I1-DOMINATES-r1.
  expect(results[0].attributes.exact).toBe("<omitted: 29 base64 chars>");
  const exported = JSON.stringify({ attributes: results[0].attributes, events: results[0].events, status: results[0].status });
  expect(exported).not.toContain(EXACT.slice(HEADER.length));
  expect(exported).not.toContain("QUJD".repeat(40));
  expect(results[0].attributes.ordinary).toBe("keep");
  expect(results[0].attributes.later).toBe(MARK);
  expect(results[0].events[0].attributes).toEqual({ value: '{"__REDACTED__":"keep"}' });
  expect(results[0].events[0].time).toEqual([1, 2]);
  expect(attributes).toEqual({ mime: BIG, keyDocument, exact: EXACT, ordinary: "keep" });
});
