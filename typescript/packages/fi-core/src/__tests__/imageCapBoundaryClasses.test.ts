import { beforeEach, describe, expect, it } from "@jest/globals";
import { SpanStatusCode } from "@opentelemetry/api";
import { BasicTracerProvider, InMemorySpanExporter, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { FITracer } from "../trace/trace-config/FITracer";
import { capBase64Images } from "../trace/trace-config/imageCap";
import { mask, maskAttributes, redactValue } from "../trace/trace-config/maskingRules";
import { generateTraceConfig } from "../trace/trace-config/traceConfig";
import { TraceConfigOptions } from "../trace/trace-config/types";

// Populated counterexamples, authored before the production overlay. No expected
// result is obtained from the cap, a private scanner or a replacement regex.
const CAP = 64;
const MARK = "__REDACTED__";
const SLASH = String.fromCharCode(92);
const HEADER = 'data:image/png;name="x";base64,';
const BIG = HEADER + "QUJD".repeat(40) + "BOUNDARYCANARY";
const EXACT = HEADER + "A".repeat(CAP - HEADER.length);
const SVG = 'data:image/svg+xml;name=base64,%3Csvg%3E';
const options = (base64ImageMaxLength: number): TraceConfigOptions => ({
  base64ImageMaxLength, piiRedaction: false, hideErrorDetails: false,
  hideInputs: false, hideOutputs: false, hideInputText: false, hideOutputText: false,
  hideInputImages: false, hideInputMessages: false, hideOutputMessages: false,
  hideEmbeddingVectors: false,
});
const WRAPPERS: Array<[string, string]> = [
  ["[photo](", ") caption"],
  ["{photo}(", ") caption"],
  ['"photo ', '" caption'],
  [" \t[", "] caption"],
  ['["', '"]'],
  ['{"note":"', '"}'],
];

function hexText(text: string): string {
  return text.split("").map((c) => SLASH + "u" + c.charCodeAt(0).toString(16).padStart(4, "0")).join("");
}

const exporter = new InMemorySpanExporter();
const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
beforeEach(() => exporter.reset());
const linkContext = {
  traceId: "0123456789abcdef0123456789abcdef",
  spanId: "0123456789abcdef",
  traceFlags: 1,
};

// Exercise real exported values, including all FISpan routes that feed maskingRules.
function surfaces(raw: string, expected: string, cap: number, exportedExpected = expected): void {
  const config = generateTraceConfig(options(cap));
  const input = Object.freeze({ value: raw, ordinary: "keep" });
  expect(redactValue(config, raw)).toBe(expected);
  expect(mask({ config, key: "value", value: raw })).toBe(expected);
  expect(maskAttributes({ config, attributes: input })).toEqual({ value: expected, ordinary: "keep" });
  const tracer = new FITracer({ tracer: provider.getTracer("boundary-classes"), traceConfig: options(cap) });
  const span = tracer.startSpan("case", { attributes: { created: raw, ordinary: "keep" } });
  span.setAttribute("single", raw);
  span.setAttributes({ batch: raw, images: [raw, "ordinary"] });
  span.addEvent("request", input, [1, 2]);
  const error = new Error(raw);
  // Deterministic stack bytes: no engine-specific frames or generated expectation.
  error.stack = "Error: " + raw;
  span.recordException(error, [2, 3]);
  span.setStatus({ code: SpanStatusCode.ERROR, message: raw });
  span.addLink({ context: linkContext, attributes: input });
  span.addLinks([{ context: linkContext, attributes: input }]);
  span.end();
  const spans = exporter.getFinishedSpans();
  expect(spans).toHaveLength(1);
  const result = spans[0];
  expect(result.attributes.created).toBe(exportedExpected);
  expect(result.attributes.single).toBe(exportedExpected);
  expect(result.attributes.batch).toBe(exportedExpected);
  expect(result.attributes.images).toEqual([exportedExpected, "ordinary"]);
  expect(result.attributes.ordinary).toBe("keep");
  const request = result.events.find((event) => event.name === "request");
  expect(request?.attributes).toEqual({ value: exportedExpected, ordinary: "keep" });
  expect(request?.time).toEqual([1, 2]);
  const exceptions = result.events.filter((event) => event.name === "exception");
  expect(exceptions).toHaveLength(1);
  expect(exceptions[0].attributes?.["exception.message"]).toBe(exportedExpected);
  expect(exceptions[0].attributes?.["exception.stacktrace"]).toBe("Error: " + exportedExpected);
  expect(exceptions[0].attributes?.["exception.type"]).toBe("Error");
  expect(exceptions[0].time).toEqual([2, 3]);
  expect(result.status).toEqual({ code: SpanStatusCode.ERROR, message: exportedExpected });
  expect(result.links).toHaveLength(2);
  for (const link of result.links) expect(link.attributes).toEqual({ value: exportedExpected, ordinary: "keep" });
  expect(input).toEqual({ value: raw, ordinary: "keep" });
  expect(error.message).toBe(raw);
  expect(error.stack).toBe("Error: " + raw);
}

describe("JSON-looking prose cannot partition an image header", () => {
  it.each(WRAPPERS)("whole over-cap image between %s and %s", (before, after) => {
    expect(capBase64Images(before + BIG + after, CAP)).toBe(before + MARK + after);
    // The wrapper, not just the URL, exceeds CAP. This defeats blanket dropping.
    expect(capBase64Images(before + EXACT + after, CAP)).toBe(before + EXACT + after);
    const under = HEADER + "A".repeat(CAP - HEADER.length - 1);
    expect(capBase64Images(before + under + after, CAP)).toBe(before + under + after);
    expect(capBase64Images(before + "ordinary text" + after, 0)).toBe(before + "ordinary text" + after);
  });

  it.each(['', 'x', ',', ':', ']', '{'])('quote-leading prose with parameter "%s"', (name) => {
    const header = 'data:image/png;name="' + name + '";base64,';
    const value = '"photo ' + header + "QUJD".repeat(40) + '"';
    expect(capBase64Images(value, CAP)).toBe('"photo ' + MARK + '"');
    const exact = header + "A".repeat(CAP - header.length);
    expect(capBase64Images('"photo ' + exact + '"', CAP)).toBe('"photo ' + exact + '"');
  });

  it.each([0, 1, 3])("decoded inner prose retains original outer bytes, depth=%i", (depth) => {
    let raw = '[photo](' + BIG + ')';
    let expected = '[photo](' + MARK + ')';
    for (let i = 0; i < depth; i++) { raw = JSON.stringify(raw); expected = JSON.stringify(expected); }
    const document = '{ "id":9007199254740993,"n":1e400,"same":1,"same":2,"s":"' + SLASH + 'uD800","value":' + JSON.stringify(raw) + ' }';
    const output = '{ "id":9007199254740993,"n":1e400,"same":1,"same":2,"s":"' + SLASH + 'uD800","value":' + JSON.stringify(expected) + ' }';
    expect(capBase64Images(document, CAP)).toBe(output);
  });

  it("decodes raw escapes before measuring and maps only the image interval", () => {
    const raw = "[photo](" + hexText(BIG) + ") " + SLASH + "u0061";
    expect(capBase64Images(raw, CAP)).toBe("[image-redacted]");
    expect(capBase64Images(raw, BIG.length)).toBe("[image-redacted]");
    const exact = "{photo}(" + hexText(EXACT) + ")";
    expect(capBase64Images(exact, CAP)).toBe("[image-redacted]");
    expect(capBase64Images(exact, CAP - 1)).toBe("[image-redacted]");
  });

  it("retains lexical declaration inspection even when the document contains prose", () => {
    const payload = "QUJD".repeat(40);
    const raw = '[diagnostic {"data":"' + payload + '","name":"' + payload + '","type":"image"} ' + BIG + ']';
    const expected = '[diagnostic {"data":"' + MARK + '","name":"' + payload + '","type":"image"} ' + MARK + ']';
    expect(capBase64Images(raw, CAP)).toBe(expected);
  });

  it("does not join independent string tokens into a fabricated image", () => {
    const array = '["data:image/png;name=", ";base64,QUJD"]';
    const object = '{"data:image/png;name=":";base64,QUJD"}';
    for (const cap of [0, 1, CAP]) {
      expect(capBase64Images(array, cap)).toBe(array);
      expect(capBase64Images(object, cap)).toBe(object);
    }
  });

  it("keeps outer-token budgets independent and unions mapped recovery edits", () => {
    const expensive = SLASH + "u005c".repeat(256) + "u0078ordinary " + "z".repeat(2048);
    const prose = "[photo](" + BIG + ")";
    const raw = JSON.stringify({ expensive, prose, exact: EXACT, note: "keep" });
    const expected = JSON.stringify({ expensive: "[image-redacted]", prose: "[photo](" + MARK + ")", exact: EXACT, note: "keep" });
    expect(capBase64Images(raw, CAP)).toBe(expected);
    const input = [prose, EXACT, "ordinary"];
    expect(capBase64Images(input, CAP)).toEqual(["[photo](" + MARK + ")", EXACT, "ordinary"]);
    expect(input).toEqual([prose, EXACT, "ordinary"]);
  });

  it("long quoted headers, wrapped payloads and multiple occurrences are whole edits", () => {
    const url = 'data:image/png;name="' + "x".repeat(4096) + '";base64,QUJD\r\nQUJD';
    const raw = "[photo](" + url + ") then " + BIG + " end";
    expect(capBase64Images(raw, CAP)).toBe("[photo](" + MARK + ") then " + MARK + " end");
    expect(capBase64Images("[photo](" + url + ")", url.length)).toBe("[photo](" + url + ")");
  });

  it.each(WRAPPERS)("real export routes for %s ... %s", (before, after) => {
    surfaces(before + BIG + after, before + MARK + after, CAP);
    exporter.reset();
    surfaces(before + EXACT + after, before + EXACT + after, CAP);
  });

  it("real export routes also inspect an encoded inner non-document", () => {
    surfaces(JSON.stringify({ value: "[photo](" + BIG + ")" }), JSON.stringify({ value: "[photo](" + MARK + ")" }), CAP);
  });
});

describe("MIME parameter values are not encoding flags", () => {
  const NOT_FLAGS = [
    SVG,
    'data:image/svg+xml;name="base64",%3Csvg%3E',
    'data:image/svg+xml;name=";base64",%3Csvg%3E',
    "data:image/svg+xml;name=';BASE64',%3Csvg%3E",
    'data:image/svg+xml;base64=ordinary,%3Csvg%3E',
    'data:image/svg+xml;base64suffix,%3Csvg%3E',
    'data:image/base64;name=ordinary,%3Csvg%3E',
    'data:image/svg+xml;name=ordinary,%3Csvg%3E',
  ];
  it.each([0, 1])("keeps non-base64 SVGs byte-identical at cap %i", (cap) => {
    for (const value of NOT_FLAGS) {
      expect(capBase64Images(value, cap)).toBe(cap === 0 ? "[image-redacted]" : value);
      expect(capBase64Images("[photo](" + value + ")", cap)).toBe(cap === 0 ? "[image-redacted]" : "[photo](" + value + ")");
      const document = JSON.stringify({ value });
      expect(capBase64Images(document, cap)).toBe(cap === 0 ? '{"value":"[image-redacted]"}' : document);
    }
  });

  it.each([
    "data:image/png;base64,QUJD",
    "data:image/png;BASE64,QUJD",
    'data:image/png;name="base64;still a parameter";bAsE64,QUJD',
    "data:image/png;name='x,y';base64,QUJD\r\nQUJD",
    "data:image/png;base64;name=ordinary,QUJD",
  ])("genuine encoding flag makes one whole marker: %s", (url) => {
    for (const cap of [0, 1, url.length - 1]) {
      expect(capBase64Images(url, cap)).toBe(cap === 0 ? "[image-redacted]" : MARK);
      expect(capBase64Images("[photo](" + url + ")", cap)).toBe(cap === 0 ? "[image-redacted]" : "[photo](" + MARK + ")");
    }
    expect(capBase64Images("[photo](" + url + ")", url.length)).toBe("[photo](" + url + ")");
  });

  it.each([0, 1])("real export routes distinguish MIME values from flags at cap %i", (cap) => {
    if (cap === 0) {
      const zeroSurfaces = (raw: string): void => {
        const expected = "[image-redacted]";
        const config = generateTraceConfig(options(cap));
        const input = Object.freeze({ value: raw, ordinary: "keep" });
        expect(redactValue(config, raw)).toBe(expected);
        expect(mask({ config, key: "value", value: raw })).toBe(expected);
        expect(maskAttributes({ config, attributes: input })).toEqual({ value: expected, ordinary: "keep" });
        const tracer = new FITracer({ tracer: provider.getTracer("boundary-classes"), traceConfig: options(cap) });
        const span = tracer.startSpan("case", { attributes: { created: raw, ordinary: "keep" } });
        span.setAttribute("single", raw);
        span.setAttributes({ batch: raw, images: [raw, "ordinary"] });
        span.addEvent("request", input, [1, 2]);
        const error = new Error(raw);
        // Deterministic stack bytes: no engine-specific frames or generated expectation.
        error.stack = "Error: " + raw;
        span.recordException(error, [2, 3]);
        span.setStatus({ code: SpanStatusCode.ERROR, message: raw });
        span.addLink({ context: linkContext, attributes: input });
        span.addLinks([{ context: linkContext, attributes: input }]);
        span.end();
        const spans = exporter.getFinishedSpans();
        expect(spans).toHaveLength(1);
        const result = spans[0];
        expect(result.attributes.created).toBe(expected);
        expect(result.attributes.single).toBe(expected);
        expect(result.attributes.batch).toBe(expected);
        expect(result.attributes.images).toEqual([expected, "ordinary"]);
        expect(result.attributes.ordinary).toBe("keep");
        const request = result.events.find((event) => event.name === "request");
        expect(request?.attributes).toEqual({ value: expected, ordinary: "keep" });
        expect(request?.time).toEqual([1, 2]);
        const exceptions = result.events.filter((event) => event.name === "exception");
        expect(exceptions).toHaveLength(1);
        expect(exceptions[0].attributes?.["exception.message"]).toBe(expected);
        expect(exceptions[0].attributes?.["exception.stacktrace"]).toBe(expected);
        expect(exceptions[0].attributes?.["exception.type"]).toBe("Error");
        expect(exceptions[0].time).toEqual([2, 3]);
        expect(result.status).toEqual({ code: SpanStatusCode.ERROR, message: expected });
        expect(result.links).toHaveLength(2);
        for (const link of result.links) expect(link.attributes).toEqual({ value: expected, ordinary: "keep" });
        expect(input).toEqual({ value: raw, ordinary: "keep" });
        expect(error.message).toBe(raw);
        expect(error.stack).toBe("Error: " + raw);
      };
      zeroSurfaces(SVG);
      exporter.reset();
      zeroSurfaces("[photo](data:image/png;name=base64;BASE64,QUJD)");
      return;
    }
    // PENDING I1xF2-PRECEDENCE: F2 keeps non-base64 SVG; I1 still omits data URIs.
    surfaces(SVG, SVG, cap, "<omitted: data URI>");
    exporter.reset();
    surfaces("[photo](data:image/png;name=base64;BASE64,QUJD)", "[photo](" + MARK + ")", cap);
  });
});
