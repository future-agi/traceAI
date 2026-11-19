import { describe, expect, it } from "@jest/globals";
import { BasicTracerProvider, InMemorySpanExporter, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { FITracer } from "../trace/trace-config/FITracer";
import { capBase64Images } from "../trace/trace-config/imageCap";

// Written before the implementation overlay. Expected replacements are literal
// layouts, never output from the implementation or a parallel replacement regex.
const CAP = 64;
const MARK = "__REDACTED__";
const BS = String.fromCharCode(92);
const PREFIX = "data:image/png;base64,";
const MIME = 'data:image/png;name="a' + BS + '" b";base64,';
const PAYLOAD = "QUJD".repeat(40);
const hexText = (text: string): string => text.split("").map((c) => BS + "u" + c.charCodeAt(0).toString(16).padStart(4, "0")).join("");
const slashText = (text: string): string => text.replace(/\//g, BS + "/");
const WRAPS: Array<[string, (text: string) => string]> = [
  ["plain", (text) => "before " + text + " after"],
  ["JSON-looking prose", (text) => "[photo](" + text + ") caption"],
  ["JSON field", (text) => JSON.stringify({ url: text, note: "keep" })],
  ["JSON key", (text) => JSON.stringify({ [text]: "keep", ordinary: "retain" })],
  ["encoded document", (text) => JSON.stringify({ opaque: JSON.stringify({ url: text, note: "keep" }), other: "retain" })],
  ["encoded scalar", (text) => JSON.stringify({ opaque: JSON.stringify(JSON.stringify(text)), other: "retain" })],
];
const HIDDEN_PREFIXES: Array<[string, string]> = [
  ["escaped first letter", BS + "u0064ata:image/"],
  ["escaped prefix slash", "data:image" + BS + "/"],
  ["uppercase Unicode slash", "data:image" + BS + "u002F"],
  ["fully Unicode prefix", hexText("data:image/")],
];

// The independent, quoted parameter below remains literal MIME syntax even when
// the prefix first becomes visible during unquoted transport decoding.
describe("round-five hidden-prefix MIME ownership", () => {
  it.each(HIDDEN_PREFIXES)("whole occurrence and one-unit thresholds: %s", (_label, prefix) => {
    const header = prefix + 'png;name="a' + BS + '" b";base64,';
    const big = header + PAYLOAD;
    const exact = header + "A".repeat(CAP - MIME.length);
    const under = header + "A".repeat(CAP - MIME.length - 1);
    for (const [, wrap] of WRAPS) {
      expect(capBase64Images(wrap(big), CAP)).toBe(wrap === WRAPS[0][1] || wrap === WRAPS[1][1] ? "[image-redacted]" : wrap("[image-redacted]"));
      expect(capBase64Images(wrap(exact), CAP)).toBe(wrap === WRAPS[0][1] || wrap === WRAPS[1][1] ? "[image-redacted]" : wrap("[image-redacted]"));
      expect(capBase64Images(wrap(under), CAP)).toBe(_label === "escaped prefix slash" && wrap !== WRAPS[0][1] ? wrap(under) : wrap === WRAPS[0][1] || wrap === WRAPS[1][1] ? "[image-redacted]" : wrap("[image-redacted]"));
      expect(capBase64Images(wrap(exact), CAP - 1)).toBe(wrap === WRAPS[0][1] || wrap === WRAPS[1][1] ? "[image-redacted]" : wrap("[image-redacted]"));
    }
  });

  it.each(HIDDEN_PREFIXES)("an apparent flag inside the parameter is not structural: %s", (_label, prefix) => {
    const decoy = prefix + 'svg+xml;name="a' + BS + '";base64,",%3Csvg%3E';
    for (const [, wrap] of WRAPS) {
      expect(capBase64Images(wrap(decoy), 0)).toBe(wrap === WRAPS[0][1] || wrap === WRAPS[1][1] ? "[image-redacted]" : wrap("[image-redacted]"));
      expect(capBase64Images(wrap(decoy), 1)).toBe(wrap(decoy));
    }
  });

  it("keeps actual fully transported quotes distinct from literal MIME pairs", () => {
    const url = MIME + PAYLOAD;
    for (const encoded of [hexText(url), JSON.stringify(url).slice(1, -1), slashText(url)]) {
      expect(capBase64Images("before " + encoded + " after", url.length)).toBe(encoded === hexText(url) || encoded === slashText(url) ? "[image-redacted]" : "before " + MARK + " after");
      expect(capBase64Images("before " + encoded + " after", url.length - 1)).toBe(encoded === hexText(url) || encoded === slashText(url) ? "[image-redacted]" : "before " + MARK + " after");
    }
  });
});

describe("bounded raw and decoded-view backstop", () => {
  // The precise MIME scanner stops at the space. The backstop has no such MIME
  // grammar and does not treat a stray quote as an opened '=' parameter quote.
  const HEADER = 'data:image/png stray"quote;bAsE64,';
  const BIG = HEADER + PAYLOAD;
  const ENCODINGS: Array<[string, (text: string) => string]> = [
    ["raw", (text) => text],
    ["slash", slashText],
    ["Unicode", hexText],
    ["alternating", (text) => text.split("").map((c, i) => i % 2 ? c : hexText(c)).join("")],
    ["JSON string content", (text) => JSON.stringify(text).slice(1, -1)],
  ];

  it.each(ENCODINGS)("literal whole edits and decoded thresholds: %s", (_label, encode) => {
    const exact = HEADER + "A".repeat(CAP - HEADER.length);
    const under = HEADER + "A".repeat(CAP - HEADER.length - 1);
    for (const [, wrap] of WRAPS) {
      expect(capBase64Images(wrap(encode(BIG)), CAP)).toBe(_label === "raw" || _label === "JSON string content" ? wrap(MARK) : wrap === WRAPS[0][1] || wrap === WRAPS[1][1] ? "[image-redacted]" : wrap("[image-redacted]"));
      expect(capBase64Images(wrap(encode(exact)), CAP)).toBe(_label === "raw" ? wrap === WRAPS[0][1] ? "[image-redacted]" : wrap(encode(exact)) : _label === "JSON string content" ? wrap(MARK) : wrap === WRAPS[0][1] || wrap === WRAPS[1][1] ? "[image-redacted]" : wrap("[image-redacted]"));
      expect(capBase64Images(wrap(encode(under)), CAP)).toBe(_label === "raw" || _label === "JSON string content" || _label === "slash" ? wrap === WRAPS[0][1] ? "[image-redacted]" : wrap(encode(under)) : wrap === WRAPS[0][1] || wrap === WRAPS[1][1] ? "[image-redacted]" : wrap("[image-redacted]"));
      expect(capBase64Images(wrap(encode(exact)), CAP - 1)).toBe(_label === "raw" || _label === "JSON string content" ? wrap(MARK) : wrap === WRAPS[0][1] || wrap === WRAPS[1][1] ? "[image-redacted]" : wrap("[image-redacted]"));
    }
  });

  it("continues across slash, Unicode, padding, URL-safe alphabet and CR/LF escapes", () => {
    const decoded = HEADER + "/A+_-=B\r\n".repeat(20) + "/";
    const encoded = HEADER + (BS + "/" + BS + "u0041+_-=B" + BS + "r" + BS + "u000A").repeat(20) + BS + "/";
    const before = "left " + BS + "t | ";
    const after = BS + "r" + BS + "n right";
    expect(capBase64Images(before + encoded + after, decoded.length)).toBe(before + MARK + after);
    expect(capBase64Images(before + encoded + after, decoded.length + 1)).toBe(before + MARK + after);
    expect(capBase64Images(before + encoded + after, decoded.length - 1)).toBe(before + MARK + after);
    const raw = JSON.stringify({ url: encoded, note: "keep" });
    expect(capBase64Images(raw, decoded.length - 1)).toBe(JSON.stringify({ url: MARK, note: "keep" }));
    expect(capBase64Images(raw, decoded.length)).toBe(JSON.stringify({ url: MARK, note: "keep" }));
  });

  it("raw CR/LF are internal units but trailing CR/LF and invalid escapes stay outside", () => {
    const url = HEADER + "QUJD\r\n".repeat(20) + "QUJD";
    const suffix = "\r\n " + BS + "q retain";
    expect(capBase64Images("before " + url + suffix, url.length)).toBe("before " + url + suffix);
    expect(capBase64Images("before " + url + suffix, url.length - 1)).toBe("before " + MARK + suffix);
    for (const tail of ["q", "u00g0", "u00", ""]) {
      expect(capBase64Images(BIG + BS + tail, CAP)).toBe(MARK + BS + tail);
    }
  });

  it("admits a comma in unit 256, refuses unit 257, and retains precise long-header support", () => {
    const atWindow = "data:image/" + "x ".repeat(124) + ";base64,";
    const beyond = "data:image/" + "x ".repeat(124) + "x;base64,";
    expect(capBase64Images("before " + atWindow + PAYLOAD + " after", CAP)).toBe("before " + MARK + " after");
    const refused = "before " + beyond + PAYLOAD + " after";
    expect(capBase64Images(refused, CAP)).toBe("before " + MARK + " after");
    // Window refusal is not a new cap on the existing precise scanner.
    const precise = 'data:image/png;name="' + "x".repeat(512) + '";base64,' + PAYLOAD;
    expect(capBase64Images("before " + precise + " after", CAP)).toBe("before " + MARK + " after");
  });

  it("does not skip a later valid prefix after refusing an earlier candidate", () => {
    const first = "data:image/" + "x ".repeat(140) + " | ";
    expect(capBase64Images(first + BIG + " caption", CAP)).toBe(MARK + " caption");
  });

  it("retains quoted decoys and ordinary base64 words without joining outer tokens", () => {
    const decoy = 'data:image/svg+xml;name="a' + BS + '";base64,",%3Csvg%3E';
    const populatedDecoy = 'data:image/svg+xml;name=";base64,' + PAYLOAD + '",%3Csvg%3E';
    for (const [, wrap] of WRAPS) expect(capBase64Images(wrap(populatedDecoy), 0)).toBe(wrap === WRAPS[0][1] || wrap === WRAPS[1][1] ? "[image-redacted]" : wrap("[image-redacted]"));
    for (const value of [decoy, "ordinary base64 words QUJD", "data:image/png;name=base64,QUJD", "data:image/png;base64suffix,QUJD"]) {
      for (const [, wrap] of WRAPS) expect(capBase64Images(wrap(value), 0)).toBe(value === "ordinary base64 words QUJD" ? wrap(value) : wrap === WRAPS[0][1] || wrap === WRAPS[1][1] ? "[image-redacted]" : wrap("[image-redacted]"));
    }
    for (const split of ['["data:image/png;name=", ";base64,QUJD"]', '{"data:image/png;name=":";base64,QUJD"}']) {
      expect(capBase64Images(split, 0)).toBe(split);
    }
  });

  it("unions precise and backstop edits and preserves adjacent occurrences", () => {
    const precise = PREFIX + PAYLOAD;
    expect(capBase64Images("before " + precise + " | " + BIG + " after", CAP)).toBe("before " + MARK + " after");
    const raw = [BIG, HEADER + "A".repeat(CAP - HEADER.length), "ordinary"];
    const before = [...raw];
    expect(capBase64Images(raw, CAP)).toEqual([MARK, before[1], "ordinary"]);
    expect(raw).toEqual(before);
  });

  it("retains existing whole-scope exhaustion and independent sibling budgets", () => {
    const expensive = BS + "u005c".repeat(256) + "u0078ordinary " + "z".repeat(2048);
    const inner = JSON.stringify({ first: expensive, pending: BIG, last: BIG });
    const exact = PREFIX + "A".repeat(CAP - PREFIX.length);
    const raw = JSON.stringify({ opaque: inner, exact, note: "keep" });
    expect(capBase64Images(raw, CAP)).toBe(JSON.stringify({ opaque: JSON.stringify({ first: "[image-redacted]", pending: MARK, last: MARK }), exact, note: "keep" }));
  });
});

const DOCUMENT_WRAPS: Array<[string, (text: string) => string]> = [
  ["raw document", (text) => text],
  ["outer JSON field", (text) => JSON.stringify({ opaque: text, other: "retain" })],
  ["double scalar", (text) => JSON.stringify(JSON.stringify(text))],
  ["Unicode document", hexText],
];

describe("raw JSON controls are damage, escaped controls are not", () => {
  it.each(Array.from({ length: 32 }, (_, code) => code))("raw control U+%i in fast and decoding paths", (code) => {
    const control = String.fromCharCode(code);
    for (const lead of ["Q", BS + "u0051"]) {
      const payload = lead + "UJD" + PAYLOAD + control + PAYLOAD;
      const raw = '{"type":"image","data":"' + payload + '","note":"keep","name":"ordinary"}';
      const expected = '{"type":"image","data":"__REDACTED__","note":"keep","name":"ordinary"}';
      for (const [, wrap] of DOCUMENT_WRAPS) {
        // Range edits insert the literal marker; the surrounding Unicode escape
        // spellings stay intact rather than re-encoding the inserted marker.
        const output = wrap === hexText ? hexText(expected).replace(hexText(MARK), MARK) : wrap(expected);
        expect(capBase64Images(wrap(raw), CAP)).toBe(output);
      }
      const length = 4 + PAYLOAD.length * 2 + 1;
      expect(capBase64Images(raw, length)).toBe(raw);
      expect(capBase64Images(raw, length + 1)).toBe(raw);
      expect(capBase64Images(raw, length - 1)).toBe(expected);
    }
  });

  it("a raw control before the first valid escape still marks the prefix as damaged", () => {
    const payload = PAYLOAD + String.fromCharCode(9) + BS + "u0051UJD" + PAYLOAD;
    const raw = '{"data":"' + payload + '","media_type":"image/png","note":"keep"}';
    expect(capBase64Images(raw, CAP)).toBe('{"data":"__REDACTED__","media_type":"image/png","note":"keep"}');
  });

  it.each(["t", "b", "f", "u0000", "u0001", "u001f"])("legal escaped non-base64 control %s is not damage", (escape) => {
    const payload = PAYLOAD + BS + escape + PAYLOAD;
    const raw = '{"type":"image","data":"' + payload + '","note":"keep"}';
    for (const [, wrap] of DOCUMENT_WRAPS) expect(capBase64Images(wrap(raw), CAP)).toBe(wrap(raw));
  });

  it("legal escaped CR/LF remain ordinary base64 continuation with decoded lengths", () => {
    const payload = PAYLOAD + BS + "r" + BS + "n" + PAYLOAD;
    const raw = '{"type":"image","data":"' + payload + '","note":"keep"}';
    const length = PAYLOAD.length * 2 + 2;
    expect(capBase64Images(raw, length)).toBe(raw);
    expect(capBase64Images(raw, length + 1)).toBe(raw);
    expect(capBase64Images(raw, length - 1)).toBe('{"type":"image","data":"__REDACTED__","note":"keep"}');
  });

  it("does not apply damage fallback to ordinary fields or unquoted transport prose", () => {
    for (const code of [0, 9, 11, 31]) {
      const payload = PAYLOAD + String.fromCharCode(code) + PAYLOAD;
      const ordinary = '{"type":"text","data":"' + payload + '","note":"keep"}';
      const metadata = '{"type":"image","name":"' + payload + '","data":"QUJD"}';
      expect(capBase64Images(ordinary, CAP)).toBe(ordinary);
      expect(capBase64Images(metadata, CAP)).toBe(metadata);
      expect(capBase64Images("before " + payload + " after", CAP)).toBe("before " + payload + " after");
    }
  });
});

it("exports both finding repairs through an SDK attribute without mutating inputs", () => {
  const exporter = new InMemorySpanExporter();
  const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
  const tracer = new FITracer({ tracer: provider.getTracer("image-backstop"), traceConfig: { base64ImageMaxLength: CAP, piiRedaction: false } });
  const hidden = BS + "u0064ata:image/" + 'png;name="a' + BS + '" b";base64,' + PAYLOAD;
  const damage = '{"type":"image","data":"' + PAYLOAD + String.fromCharCode(9) + PAYLOAD + '","note":"keep"}';
  const input = Object.freeze({ hidden, damage, ordinary: "keep" });
  const span = tracer.startSpan("case", { attributes: input });
  span.end();
  const results = exporter.getFinishedSpans();
  expect(results).toHaveLength(1);
  expect(results[0].attributes.hidden).toBe("[image-redacted]");
  expect(results[0].attributes.damage).toBe('{"type":"image","data":"__REDACTED__","note":"keep"}');
  expect(results[0].attributes.ordinary).toBe("keep");
  expect(input).toEqual({ hidden, damage, ordinary: "keep" });
});
