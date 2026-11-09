import { beforeEach, describe, expect, it } from "@jest/globals";
import { SpanStatusCode } from "@opentelemetry/api";
import { BasicTracerProvider, InMemorySpanExporter, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { FITracer } from "../trace/trace-config/FITracer";
import { TraceConfigOptions } from "../trace/trace-config/types";
import { capBase64Images } from "../trace/trace-config/imageCap";

// These tests import existing public paths at b068b715; no new-helper import is
// needed for a RED. Assertions inspect actual completed in-memory SDK exports.
const MAX = 256;
const MARK = "__REDACTED__";
const PREFIX = "data:image/png;base64,";
const PAYLOAD = "QUJD".repeat(500) + "TAILPAYLOAD";
const BIG = PREFIX + PAYLOAD;
const EMAIL = "jane.doe@example.com";
const at = (length: number) => PREFIX + "A".repeat(length - PREFIX.length);

const exporter = new InMemorySpanExporter();
const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
beforeEach(() => exporter.reset());
function tracer(options: TraceConfigOptions = {}) {
  return new FITracer({ tracer: provider.getTracer("class-fix"), traceConfig: {
    base64ImageMaxLength: MAX, piiRedaction: false, hideErrorDetails: false, ...options,
  } });
}
function only() {
  const spans = exporter.getFinishedSpans();
  expect(spans).toHaveLength(1);
  return spans[0];
}
function valueExport(value: string, options: TraceConfigOptions = {}) {
  const input = Object.freeze({ "input.value": value });
  const span = tracer(options).startSpan("case", { attributes: input });
  span.setAttribute("later", value);
  span.end();
  const result = only();
  expect(result.attributes.later).toBe(result.attributes["input.value"]);
  expect(input["input.value"]).toBe(value);
  return result.attributes["input.value"];
}
function surfaces(value: string, expected: string, options: TraceConfigOptions = {}) {
  const attrs = Object.freeze({ "input.value": value });
  const event = Object.freeze({ value });
  const error = new Error(value);
  const originalStack = error.stack;
  const span = tracer(options).startSpan("case", { attributes: attrs });
  span.setAttribute("later", value);
  span.addEvent("request", event, [1, 2]);
  span.recordException(error, [2, 3]);
  span.setStatus({ code: SpanStatusCode.ERROR, message: value });
  span.end();
  const result = only();
  expect(result.attributes["input.value"]).toBe(expected);
  expect(result.attributes.later).toBe(expected);
  expect(result.events.find((e) => e.name === "request")?.attributes?.value).toBe(expected);
  expect(result.events.find((e) => e.name === "request")?.time).toEqual([1, 2]);
  const exceptions = result.events.filter((e) => e.name === "exception");
  expect(exceptions).toHaveLength(1);
  expect(exceptions[0].attributes?.["exception.message"]).toBe(expected);
  expect(exceptions[0].time).toEqual([2, 3]);
  expect(result.status.message).toBe(expected);
  if (!expected.includes("TAILPAYLOAD")) {
    expect(String(exceptions[0].attributes?.["exception.stacktrace"])).not.toContain("TAILPAYLOAD");
  }
  expect(error.message).toBe(value);
  expect(error.stack).toBe(originalStack);
  expect(attrs["input.value"]).toBe(value);
  expect(event.value).toBe(value);
}

const wrapped = (first: number, separator: string) => {
  const rest = PAYLOAD.slice(first).match(/.{1,76}/g) ?? [];
  return PREFIX + [PAYLOAD.slice(0, first), ...rest].join(separator);
};
const URL_CASES: Array<[string, string]> = [
  ["long-header", "data:image/png;name=" + "a".repeat(600) + ";base64," + PAYLOAD],
  ["LF-short-first", wrapped(8, "\n")],
  ["CRLF-short-first", wrapped(8, "\r\n")],
  ["LF-long-first", wrapped(600, "\n")],
  ["quoted-header", 'data:image/png;name="' + "a".repeat(600) + '";base64,' + PAYLOAD],
  ["escaped-prose", "data:image\\/png;base64," + PAYLOAD],
];

describe("class-fix", () => {
  it.each(URL_CASES)("boundaries %s", (_label, url) => {
    for (const prefix of ["400 invalid image ", ""]) {
      exporter.reset();
      surfaces(prefix + url + " caption retained", _label === "escaped-prose" ? "[image-redacted]" : prefix + MARK + " caption retained");
    }
  });
  it.each(URL_CASES)("boundary exact control %s", (_label, url) => {
    // PENDING I1xF2-PRECEDENCE T39-T44: TS I1 media omission dominates positive caps.
    // LANE-CALL-NIKM4-5A-TS-IMAGE-EXPORT-I1-DOMINATES-r1. Counts include the
    // original LF/CRLF payload units, matching I1 rather than a normalized decoder.
    const omissions: Record<string, string> = {
      "long-header": "<omitted: 2011 base64 chars>",
      "LF-short-first": "<omitted: 2038 base64 chars>",
      "CRLF-short-first": "<omitted: 2065 base64 chars>",
      "LF-long-first": "<omitted: 2030 base64 chars>",
      "quoted-header": "<omitted: 2011 base64 chars>",
      "escaped-prose": "<omitted: 2011 base64 chars>",
    };
    expect(capBase64Images(url, url.length)).toBe(url);
    surfaces(url, omissions[_label], { base64ImageMaxLength: url.length });
    const result = only();
    expect(JSON.stringify({ attributes: result.attributes, events: result.events, status: result.status }))
      .not.toContain("TAILPAYLOAD");
  });
  it.each([MAX - 1, MAX, MAX + 1])("caption occurrence %i", (length) => {
    const url = at(length), caption = " caption " + "diagnostic prose ".repeat(300);
    for (const prefix of ["", "image: "]) {
      exporter.reset();
      surfaces(prefix + url + caption, "[image-redacted]");
    }
  });
  it.each([63, 64, 65])("object depth %i", (depth) => {
    const deep = "[".repeat(depth) + JSON.stringify(BIG) + "]".repeat(depth);
    const raw = '{"deep":' + deep + ',"ordinary":"keep","shallow":' + JSON.stringify(BIG) + '}';
    // PENDING I1xF2-PRECEDENCE: I1 structure budget remains stricter than the token scanner.
    expect(capBase64Images(raw, MAX)).toBe(raw.split(BIG).join(MARK));
    expect(valueExport(raw)).toBeUndefined();
    exporter.reset();
    expect(valueExport("ordinary shallow control")).toBe("ordinary shallow control");
  });
  it.each([7, 8, 9])("encoded depth %i", (depth) => {
    let raw = '{"data":"' + PAYLOAD + '","media_type":"image/png"}';
    let expected = '{"data":"' + MARK + '","media_type":"image/png"}';
    for (let i = 0; i < depth; i++) { raw = JSON.stringify(raw); expected = JSON.stringify(expected); }
    const result = valueExport(raw);
    expect(result).not.toContain("TAILPAYLOAD");
    expect(result).toBe(expected);
  });
  it.each(["complete", "missing-close", "unterminated-string"])("escaped declared %s", (kind) => {
    const stem = '{"\\u006dedia_type":"image\\/png","d\\u0061ta":"';
    const suffix = kind === "complete" ? '"}' : kind === "missing-close" ? '"' : '';
    // PENDING I1xF2-PRECEDENCE: I1 refuses incomplete JSON even after F2 token repair.
    expect(capBase64Images(stem + PAYLOAD + suffix, MAX)).toBe(stem + MARK + suffix);
    if (kind === "complete") {
      expect(valueExport(stem + PAYLOAD + suffix)).toBe(stem + MARK + suffix);
    } else {
      expect(valueExport(stem + PAYLOAD + suffix)).toBe("<omitted: incomplete audio>");
    }
  });
  it.each(["invalid-escape", "partial-escape"])("damaged payload token %s", (kind) => {
    const stem = '{"media_type":"image/png","note":"ordinary sibling","data":"';
    const damage = kind === "invalid-escape" ? "\\q" : "\\u0";
    const suffix = kind === "invalid-escape" ? '"}' : '';
    // PENDING I1xF2-PRECEDENCE: incomplete repaired JSON retains I1 fail-closed handling.
    expect(capBase64Images(stem + PAYLOAD + damage + suffix, MAX)).toBe(stem + MARK + suffix);
    if (kind === "invalid-escape") {
      expect(valueExport(stem + PAYLOAD + damage + suffix)).toBe(stem + MARK + suffix);
    } else {
      expect(valueExport(stem + PAYLOAD + damage + suffix)).toBe("<omitted: incomplete audio>");
    }
    exporter.reset();
    const ordinary = '{"type":"image","name":"' + "ordinary prose ".repeat(100);
    expect(capBase64Images(ordinary, MAX)).toBe(ordinary);
    expect(valueExport(ordinary)).toBe("<omitted: incomplete audio>");
  });
  it("lossless numeric and escape tokens", () => {
    const raw = '{ "id":9007199254740993, "n":1e400,"same":1,"same":2,"s":"\\uD800","esc":"\\u0061\\/b","url":' + JSON.stringify(BIG) + '}';
    const result = valueExport(raw);
    expect(result).toBe(raw.replace(BIG, MARK));
    expect(() => JSON.parse(result as string)).not.toThrow();
  });
  it("deep ordinary sibling and lone surrogate preserve all non-image tokens", () => {
    const deep = "[".repeat(6000) + '"keep"' + "]".repeat(6000);
    const raw = '{ "id":9007199254740993, "n":1e400, "same":1,"same":2, "s":"\\uD800", "esc":"\\u0061\\/b", "deep":' + deep + ', "url":' + JSON.stringify(BIG) + '}';
    // PENDING I1xF2-PRECEDENCE: I1 structure budget remains stricter than the token scanner.
    expect(capBase64Images(raw, MAX)).toBe(raw.replace(BIG, MARK));
    expect(valueExport(raw)).toBeUndefined();
    exporter.reset();
    expect(valueExport("ordinary shallow control")).toBe("ordinary shallow control");
  });
  it.each([64, 65])("escaped payload boundary %i", (repeats) => {
    const payload = "\\u0051\\u0055\\u004a\\u0044".repeat(repeats);
    const raw = '{"media_type":"image/png","data":"' + payload + '"}';
    // PENDING I1xF2-PRECEDENCE T53: TS I1 media omission dominates positive caps.
    // LANE-CALL-NIKM4-5A-TS-IMAGE-EXPORT-I1-DOMINATES-r1.
    expect(capBase64Images(raw, MAX)).toBe(repeats * 4 > MAX ? raw.replace(payload, MARK) : raw);
    const result = valueExport(raw);
    expect(result).toBe(raw.replace(payload, repeats * 4 > MAX ? MARK : "<omitted: 256 base64 chars>"));
    expect(result).not.toContain(payload);
    expect(result).not.toContain("QUJD".repeat(repeats));
  });
  it("exact image omission preserves lossless non-image JSON tokens", () => {
    const raw = '{"id":9007199254740993,"n":1e400,"url":' + JSON.stringify(at(MAX)) + '}';
    // PENDING I1xF2-PRECEDENCE T54: TS I1 media omission dominates positive caps.
    // LANE-CALL-NIKM4-5A-TS-IMAGE-EXPORT-I1-DOMINATES-r1.
    expect(capBase64Images(raw, MAX)).toBe(raw);
    const result = valueExport(raw);
    expect(result).toBe(raw.replace(at(MAX), "<omitted: 234 base64 chars>"));
    expect(result).not.toContain(at(MAX).slice(PREFIX.length));
  });
  it.each([0, 1, 4])("schema metadata cap %i", (maximum) => {
    const raw = '{"type":"image","modality":"image","id":"QUJD","name":"image","schema":"base64","other":"QUJD","source":{"type":"base64","media_type":"image/png","data":"QUJD"}}';
    // PENDING I1xF2-PRECEDENCE T56: TS I1 media omission dominates positive caps.
    // LANE-CALL-NIKM4-5A-TS-IMAGE-EXPORT-I1-DOMINATES-r1. Cap-zero T55 keeps its r1 marker fix.
    const expected = maximum === 0 ? raw.replace('"data":"QUJD"', '"data":"[image-redacted]"') : maximum < 4 ? raw.replace('"data":"QUJD"', '"data":"' + MARK + '"') : raw.replace('"data":"QUJD"', '"data":"<omitted: 4 base64 chars>"');
    const result = valueExport(raw, { base64ImageMaxLength: maximum });
    const parsed = JSON.parse(result as string);
    expect(parsed.type).toBe("image");
    expect(parsed.source.type).toBe("base64");
    expect(parsed.id).toBe("QUJD");
    expect(result).toBe(expected);
    // The same text remains in ordinary metadata; absence is scoped to the payload leaf.
    expect(parsed.source.data).not.toContain("QUJD");
    expect(result).not.toContain('"data":"QUJD"');
    expect(parsed.other).toBe("QUJD");
  });
  it("source inherits image declaration without MIME; ordinary sibling does not", () => {
    const raw = '{"source":{"type":"base64","data":"' + PAYLOAD + '"},"type":"image","name":"' + PAYLOAD + '"}';
    expect(valueExport(raw)).toBe(raw.replace('"data":"' + PAYLOAD + '"', '"data":"' + MARK + '"'));
  });
  it("audio and unknown alphabet-only fields are not images", () => {
    const raw = '{"type":"audio","source":{"type":"base64","data":"' + PAYLOAD + '"}}';
    // PENDING I1xF2-PRECEDENCE: the image-cap non-image control is not an audio export grant.
    expect(capBase64Images(raw, 0)).toBe(raw);
    expect(valueExport(raw, { base64ImageMaxLength: 0 })).toBe(raw.replace(PAYLOAD, "<omitted: 2011 base64 chars>"));
  });
  it.each([["email", EMAIL, "<EMAIL_ADDRESS>"], ["image", BIG, MARK], ["ordinary", "TypeError", "TypeError"]])("derived type %s", (_label, raw, expected) => {
    const attributes = Object.freeze({ "exception.type": raw });
    const span = tracer({ piiRedaction: true }).startSpan("case");
    span.addEvent("exception", attributes);
    span.setStatus({ code: SpanStatusCode.ERROR });
    span.end();
    const result = only();
    expect(result.attributes["error.type"]).toBe(expected);
    expect(result.events[0].attributes?.["exception.type"]).toBe(expected);
    expect(attributes["exception.type"]).toBe(raw);
  });
  it("provider code is masked at final derived export", () => {
    const error = Object.assign(new Error("failed"), { code: "sk_live_" + "A".repeat(24) });
    const span = tracer({ piiRedaction: true }).startSpan("case");
    span.recordException(error);
    span.setStatus({ code: SpanStatusCode.ERROR });
    span.end();
    expect(only().attributes["error.type"]).toBe("<API_KEY>");
    expect(error.code).toBe("sk_live_" + "A".repeat(24));
  });
  it("explicit type precedence and PII off remain", () => {
    const span = tracer().startSpan("case", { attributes: { "error.type": EMAIL } });
    span.addEvent("exception", { "exception.type": "TypeError" });
    span.setStatus({ code: SpanStatusCode.ERROR });
    span.end();
    expect(only().attributes["error.type"]).toBe(EMAIL);
  });
  it.each([Infinity, -1, NaN, 0.5])("invalid cap remains finite %s", (maximum) => {
    expect(valueExport(at(40000), { base64ImageMaxLength: maximum })).toBe("[image-redacted]");
  });
  it("stronger hide flags are independent of exact image controls", () => {
    const span = tracer({ hideInputs: true }).startSpan("case", { attributes: { "input.value": at(MAX) } });
    span.end();
    expect(only().attributes["input.value"]).toBe(MARK);
  });
  it.each([false, true])("full path no-at prose resource control pii=%s", (piiRedaction) => {
    const start = process.cpuUsage();
    for (const size of [16384, 65536]) {
      exporter.reset();
      const text = "ordinary prose " + "a".repeat(size);
      surfaces(text, text, { piiRedaction });
    }
    const elapsed = process.cpuUsage(start);
    expect((elapsed.user + elapsed.system) / 1e6).toBeLessThan(5);
  }, 15000);
});
