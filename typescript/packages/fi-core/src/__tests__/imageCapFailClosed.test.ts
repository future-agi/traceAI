import { describe, expect, it } from "@jest/globals";
import { BasicTracerProvider, InMemorySpanExporter, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { capBase64Images, __ImageCapEditsForTest } from "../trace/trace-config/imageCap";
import { FITracer } from "../trace/trace-config/FITracer";

// Source-first contract controls. No executed RED/GREEN is asserted.
const A = "__REDACTED__";
const B = "[image-redacted]";
const CAP = 64;
const SCAN = 1_048_576;
const BS = String.fromCharCode(92);
const P = "QUJD".repeat(40);
const H = "data:image/png;base64,";
const URL = H + P;
const hexText = (s: string): string => s.split("").map(c => BS + "u" + c.charCodeAt(0).toString(16).padStart(4, "0")).join("");

// Constructors below build inputs only; output layouts are independent literals.
describe("fail-closed r3 raw and decoded occurrences", () => {
  it("R01 exact raw occurrences preserve captions, with strict thresholds", () => {
    // Keep the second flag beyond the first prefix's 1024-unit header window.
    const long = URL + "A".repeat(1000);
    expect(capBase64Images("left " + long + " | " + long + " right", CAP))
      .toBe("left __REDACTED__ | __REDACTED__ right");
    // With nearer flags the quote-independent windows overlap and union once.
    expect(capBase64Images("left " + URL + " | " + URL + " right", CAP))
      .toBe("left __REDACTED__ right");
    expect(capBase64Images("left data:image/png;base64,QUJD right", 26))
      .toBe("[image-redacted]");
    expect(capBase64Images("left data:image/png;base64,QUJD right", 25))
      .toBe("left __REDACTED__ right");
    expect(capBase64Images("data:image/png;base64,", 0)).toBe(B);
    expect(capBase64Images("caption %64ata:image/png;base64,QUJD tail", 4096))
      .toBe("caption %64ata:image/png;base64,QUJD tail");
  });

  it.each([
    'DaTa: \n IMAGE/png, ignored;BaSe64,' + P,
    'data:image/png stray"quote;base64,' + P,
    "data: \n image/png;name='anything,anything';base64," + P,
    'data:image/svg+xml;name=";base64,' + P + '",%3Csvg%3E',
    'data:image/svg+xml stray;name=";base64,' + P + '",%3Csvg%3E',
  ])("R02 quote-independent populated raw headers: %s", raw => {
    const suffix = raw.endsWith('",%3Csvg%3E') ? '",%3Csvg%3E' : "";
    expect(capBase64Images(raw, CAP)).toBe(A + suffix);
  });

  it.each([
    BS + "u0064ata:image/png;base64," + P,
    "data:image" + BS + "/png;base64," + P,
    hexText(URL),
    "%64ata%3Aimage%2Fpng%3Bbase64%2C" + P,
    "&#100;ata&colon;image&sol;png&semi;base64&comma;" + P,
    "data&colon;image&sol;png&semi;base64&comma;" + P,
    "&#x64;ata:image/png;base64," + P,
    "d" + String.fromCodePoint(0xe0100) + "ata:image/png;base64," + P,
    "da" + String.fromCharCode(0x200b) + "ta:image/png;base64," + P,
    "data:" + String.fromCharCode(0x2060) + "ima" + String.fromCharCode(0xfeff) + "ge/png;base64," + P,
  ])("R03 decoded-only headers select whole B: %s", raw => {
    expect(capBase64Images("caption " + raw + " tail", CAP)).toBe(B);
    expect(capBase64Images(JSON.stringify({ value: raw, note: "keep" }), CAP))
      .toBe('{"value":"[image-redacted]","note":"keep"}');
    expect(capBase64Images(JSON.stringify({ [raw]: "keep", note: "retain" }), CAP))
      .toBe('{"[image-redacted]":"keep","note":"retain"}');
  });

  it.each(["%41", "%2F", "&#65;", "&#x41;", BS + "u0041", BS + "/"])(
    "R04 exact raw transport continuations consume the entire payload: %s", unit => {
      expect(capBase64Images("left " + H + "QUJD" + unit.repeat(160) + " right", CAP))
        .toBe("left __REDACTED__ right");
      expect(capBase64Images("left " + URL + unit + "QUJD right", CAP))
        .toBe("left __REDACTED__ right");
    });

  it.each([BS + "u0064", "%64", "&#100;"])(
    "R05 adjacent hidden prefixes are checked before, never after, redaction: %s", hidden => {
      const raw = URL + hidden + "ata:image/png;base64," + P;
      expect(capBase64Images(raw, CAP)).toBe(B);
      expect(capBase64Images(JSON.stringify({ value: raw, note: "keep" }), CAP))
        .toBe('{"value":"[image-redacted]","note":"keep"}');
    });

  it.each(["%E2%80%8B", "%C2%AD", "%EF%BB%BF", "&#8203;"])(
    "R06 a removed invisible cannot leave a second payload block: %s", invisible => {
      expect(capBase64Images(URL + invisible + P + " suffix", CAP)).toBe(B);
    });

  it("R07 run ends retain delimiters, not trailing alphabetic data", () => {
    expect(capBase64Images(URL + "data: caption", CAP)).toBe("__REDACTED__: caption");
    expect(capBase64Images("left " + H + "QUJD\r\nQUJD\r\n right", 0))
      .toBe("[image-redacted]");
    expect(capBase64Images(URL + BS + "q tail", CAP)).toBe(A + BS + "q tail");
  });

  it("R08 all raw candidates and all terminators are visited independently", () => {
    const header = "data:image/png stray;base64,";
    expect(capBase64Images(header + header + P, CAP)).toBe(A);
    const r7 = 'data:image/svg+xml;name="hold data:image/png;name="a;base64,QUJD";base64,' + P + " suffix";
    expect(capBase64Images(r7, CAP)).toBe("__REDACTED__ suffix");
    expect(capBase64Images(H + "QUJD" + H + P, 25)).toBe(A);
  });
});

describe("bound round-five and round-six quote witnesses", () => {
  it("R09 hidden-prefix MIME pair, rejected prefix and earlier open quote", () => {
    const hidden = BS + "u0064ata:image/";
    const later = hidden + 'png;name="a' + BS + '" b";base64,' + P;
    for (const raw of [later, hidden + "x ".repeat(120) + later, hidden + 'png stray;name=",' + later]) {
      for (const cap of [194, 195, 196]) expect(capBase64Images(raw, cap)).toBe(B);
      expect(capBase64Images(raw, CAP)).toBe(B);
      expect(capBase64Images(JSON.stringify({ value: raw, note: "keep" }), CAP))
        .toBe('{"value":"[image-redacted]","note":"keep"}');
    }
  });

  it.each([
    'data:image/svg+xml stray;name="a' + BS + '";base64,' + P + '",%3Csvg%3E',
    'data:image/svg+xml; name="a' + BS + '";base64,' + P + '",%3Csvg%3E',
    'data:image/svg+xml;name="a' + BS + '";base64,' + P + '",%3Csvg%3E',
    'data:image/svg+xml stray;name=' + BS + 'u0022;base64,' + P + '",%3Csvg%3E',
    'data:image/svg+xml;name' + BS + 'u003d"a;base64,' + P + '",%3Csvg%3E',
  ])("R10 literal and manufactured-ownership decoys redact: %s", raw => {
    expect(capBase64Images(raw, CAP)).toBe('__REDACTED__",%3Csvg%3E');
  });

  it("R11 an empty decoy run and non-whitespace prefix gap are benign", () => {
    const raw = BS + 'u0064ata:image/svg+xml;name="a' + BS + '";base64,",%3Csvg%3E';
    expect(capBase64Images(raw, 0)).toBe(B);
    const gap = "data: arbitrary IMAGE/png;base64," + P;
    expect(capBase64Images(gap, CAP)).toBe(gap);
    const oldGap = "left DaTa: arbitrary IMAGE/png, ignored;BaSe64," + P + " right";
    expect(capBase64Images(oldGap, CAP)).toBe(oldGap);
  });
});

describe("Q1 before every whole-view edit", () => {
  it.each([
    '{"data:image/png;name=":";base64,' + P + '"}',
    '["data:image/png;name=", ";base64,' + P + '"]',
  ])("R12 split tokens, transported syntax and nested documents stay identical: %s", inner => {
    const braces = inner.startsWith("{") ? BS + "u007b" + inner.slice(1, -1) + BS + "u007d" : inner;
    for (const raw of [inner, BS + "u0020" + inner, "%20" + inner, "&#32;" + inner, braces]) {
      expect(capBase64Images(raw, CAP)).toBe(raw);
      const outer = JSON.stringify({ opaque: raw, other: "retain" });
      expect(capBase64Images(outer, CAP)).toBe(outer);
    }
    let nested = inner;
    for (let i = 0; i < 3; i++) nested = JSON.stringify({ opaque: nested, outside: "keep" });
    expect(capBase64Images(nested, 0)).toBe(nested);
  });

  it("R13 syntax, duplicate members, numeric lexemes and unrelated escapes survive", () => {
    const raw = '{ "id":9007199254740993,"n":1e400,"same":1,"same":2,"a":"left ' + URL + ' right","b":"caption %64ata:image/png;base64,' + P + ' tail","s":"' + BS + 'uD800" }';
    expect(capBase64Images(raw, CAP))
      .toBe('{ "id":9007199254740993,"n":1e400,"same":1,"same":2,"a":"left __REDACTED__ right","b":"[image-redacted]","s":"' + BS + 'uD800" }');
  });

  it("R14 replacing keys retains lexical count and order, including collisions", () => {
    const raw = '{"%64ata:image/png;base64,' + P + '":1,"&#100;ata:image/png;base64,' + P + '":2,"ordinary":3}';
    expect(capBase64Images(raw, CAP)).toBe('{"[image-redacted]":1,"[image-redacted]":2,"ordinary":3}');
    const distinct = '{"' + URL + '":1,"%64ata:image/png;base64,' + P + '":2,"ordinary":3}';
    expect(capBase64Images(distinct, CAP)).toBe('{"__REDACTED__":1,"[image-redacted]":2,"ordinary":3}');
  });
});

describe("declared payload compatibility and genuine positives", () => {
  it.each([0, 9, 11, 31])("R15 raw control U+%i remains damaged", code => {
    for (const lead of ["QUJD", BS + "u0051UJD"]) {
      const raw = '{"type":"image","data":"' + lead + P + String.fromCharCode(code) + P + '","note":"keep"}';
      expect(capBase64Images(raw, CAP)).toBe('{"type":"image","data":"__REDACTED__","note":"keep"}');
    }
  });

  it.each(["t", "b", "f", "u0000", "u0001", "u001f"])("R16 legal escaped %s is never reclassified as damage", escape => {
    for (const end of ["}", ",}", ""]) {
      const inner = '{"type":"image","data":"' + P + BS + escape + P + '","note":"keep"' + end;
      expect(capBase64Images(inner, CAP)).toBe(inner);
      const outer = JSON.stringify({ opaque: inner, other: "retain" });
      expect(capBase64Images(outer, CAP)).toBe(outer);
    }
  });

  it("R17 declarations retain thresholds, source inheritance and array barriers", () => {
    const raw = '{"type":"image","source":{"data":"QUJD"},"name":"QUJD","other":[{"data":"QUJD"}]}';
    expect(capBase64Images(raw, 4)).toBe(raw);
    expect(capBase64Images(raw, 3)).toBe('{"type":"image","source":{"data":"__REDACTED__"},"name":"QUJD","other":[{"data":"QUJD"}]}');
    expect(capBase64Images("iVBORw0KGgo=", 12)).toBe("iVBORw0KGgo=");
    expect(capBase64Images("iVBORw0KGgo=", 11)).toBe(A);
    const audio = '{"type":"input_audio","data":"' + P + '"}';
    expect(capBase64Images(audio, 0)).toBe(audio);
  });

  it("R18 positive declaration recovery revisits newly revealed malformed syntax", () => {
    const missing = '{"type":"image" "data":"' + P + '"}';
    expect(capBase64Images(missing, CAP)).toBe('{"type":"image" "data":"__REDACTED__"}');
    expect(capBase64Images(JSON.stringify({ opaque: missing, other: "retain" }), CAP))
      .toBe(JSON.stringify({ opaque: '{"type":"image" "data":"__REDACTED__"}', other: "retain" }));
    const escaped = '{"type":"image",' + BS + 'u0022data' + BS + 'u0022:' + BS + 'u0022' + P + BS + 'u0022]';
    expect(capBase64Images(escaped, CAP)).toBe('{"type":"image",' + BS + 'u0022data' + BS + 'u0022:' + BS + 'u0022__REDACTED__' + BS + 'u0022]');
    const escapedMissing = '{"type":"image" ' + BS + 'u0022data' + BS + 'u0022:' + BS + 'u0022' + P + BS + 'u0022}';
    expect(capBase64Images(escapedMissing, CAP)).toBe('{"type":"image" ' + BS + 'u0022data' + BS + 'u0022:' + BS + 'u0022__REDACTED__' + BS + 'u0022}');
    const array = '[' + BS + 'u007b' + BS + 'u0022type' + BS + 'u0022:' + BS + 'u0022image' + BS + 'u0022 ' + BS + 'u0022data' + BS + 'u0022:' + BS + 'u0022' + P + BS + 'u0022' + BS + 'u007d]';
    expect(capBase64Images(array, CAP)).toBe('[' + BS + 'u007b' + BS + 'u0022type' + BS + 'u0022:' + BS + 'u0022image' + BS + 'u0022 ' + BS + 'u0022data' + BS + 'u0022:' + BS + 'u0022__REDACTED__' + BS + 'u0022' + BS + 'u007d]');
    const ordinary = missing.replace('"image"', '"text"');
    expect(capBase64Images(ordinary, CAP)).toBe(ordinary);
  });

  it("R19 long literal MIME headers and non-final encoding flags retain positive coverage", () => {
    for (const url of ['data:image/png;name="' + "q".repeat(4096) + '";base64,' + P, "data:image/png;BASE64;name=ordinary," + P]) {
      expect(capBase64Images("prefix " + url + " suffix", CAP)).toBe("prefix __REDACTED__ suffix");
      expect(capBase64Images("[photo](" + url + ")", CAP)).toBe("[photo](__REDACTED__)");
      expect(capBase64Images("[photo](" + url + ")", 1)).toBe("[photo](__REDACTED__)");
    }
    const declared = '{"type":"image","url":"data:image/png;BASE64;name=ordinary,' + P + '","note":"keep"}';
    expect(capBase64Images(declared, CAP)).toBe('{"type":"image","url":"__REDACTED__","note":"keep"}');
    expect(capBase64Images(declared, 1)).toBe('{"type":"image","url":"__REDACTED__","note":"keep"}');
  });

  it("R20 the new arbitrary-header rule deliberately includes later apparent flags", () => {
    const raw = "data:image/svg+xml,%3Csvg%3E ;base64,QUJD";
    expect(capBase64Images(raw, 0)).toBe(B);
    expect(capBase64Images(JSON.stringify({ note: raw, keep: 1 }), 0)).toBe('{"note":"[image-redacted]","keep":1}');
  });
});

describe("admission, fixed points, benign values and counts", () => {
  it.each([
    ["ordinary base64 words QUJD", "ordinary base64 words QUJD"],
    ["https://example.invalid/" + "x".repeat(4096), "https://example.invalid/" + "x".repeat(4096)],
    ["data:audio/wav;base64,QUJD", "data:audio/wav;base64,QUJD"],
    ["data:image/svg+xml,%3Csvg%3E", B],
    ["data:image/png;name=base64,QUJD", B],
    ["data:image/png;base64suffix,QUJD", B],
    ["ordinary %78 &amp; " + BS + "u0078", "ordinary %78 &amp; " + BS + "u0078"],
    ["ordinary " + String.fromCharCode(0x200b) + " text", "ordinary " + String.fromCharCode(0x200b) + " text"],
    ["[image-redacted] __REDACTED__ ordinary marker text", "[image-redacted] __REDACTED__ ordinary marker text"],
    ["ordinary " + BS + "u00g0 " + BS + "q", "ordinary " + BS + "u00g0 " + BS + "q"],
  ])("R21 benign originals retain exact bytes: %s", (raw, expected) => {
    expect(capBase64Images(raw, 0)).toBe(expected);
    const json = JSON.stringify({ value: raw, note: "keep" });
    expect(capBase64Images(json, 0)).toBe(JSON.stringify({ value: expected, note: "keep" }));
  });

  it("R22 oversize fallback precedes a huge caller image limit", () => {
    expect(capBase64Images("x".repeat(SCAN + 1), Number.MAX_SAFE_INTEGER)).toBe(B);
    expect(capBase64Images("x".repeat(SCAN), Number.MAX_SAFE_INTEGER)).toBe("x".repeat(SCAN));
    expect(capBase64Images('{"value":"' + "x".repeat(SCAN) + '"}', Number.MAX_SAFE_INTEGER)).toBe(B);
  });

  it("R23 the three-pass fixed point is not a passthrough depth limit", () => {
    expect(capBase64Images("%25252578ordinary", CAP)).toBe(B);
    expect(capBase64Images(BS + "u005cu005cu005cu0078ordinary", 0)).toBe(B);
    expect(capBase64Images("%252578ordinary", 0)).toBe("%252578ordinary");
    expect(capBase64Images("caption %25252564ata:image/png;base64," + P + " tail", CAP)).toBe(B);
    expect(capBase64Images("%26%23x5c%3Bu0064ata:image/png;base64," + P, CAP)).toBe(B);
    expect(capBase64Images("caption " + H + "QUJD%2525252F" + P + " tail", CAP)).toBe(B);
  });

  it("R24 exact 1024-unit header window and decoded-only shortened header", () => {
    const header = "data:image/" + "x".repeat(1005) + ";base64,";
    expect(header.length).toBe(1024);
    expect(capBase64Images("left " + header + P + " right", CAP)).toBe("left __REDACTED__ right");
    const encoded = "%64ata:image/" + "&#32;".repeat(220) + ";base64," + P;
    expect(capBase64Images("left " + encoded + " right", CAP)).toBe(B);
  });

  it("R25 scalar and array dispatch preserve identities and inputs", () => {
    const input = [URL, "ordinary", "%64ata:image/png;base64," + P];
    const before = [...input];
    expect(capBase64Images(input, CAP)).toEqual([A, "ordinary", B]);
    expect(input).toEqual(before);
    expect(capBase64Images(17, 0)).toBe(17);
    expect(capBase64Images(false, 0)).toBe(false);
    const numbers = [1, 2, 3];
    expect(capBase64Images(numbers, 0)).toBe(numbers);
  });

  it("R26 internal count counts emitted replacements, unions and B subsumption", () => {
    // Existing deep-module test seam; no package-index API or span attribute added.
    expect(__ImageCapEditsForTest.counted([URL + "A".repeat(1000) + " | " + URL, "%64ata:image/png;base64," + P, "[image-redacted] __REDACTED__"], CAP))
      .toEqual({ value: ["__REDACTED__ | __REDACTED__", B, "[image-redacted] __REDACTED__"], count: 3 });
    expect(__ImageCapEditsForTest.counted(URL + "%64ata:image/png;base64," + P, CAP))
      .toEqual({ value: B, count: 1 });
    expect(__ImageCapEditsForTest.counted(H + "QUJD" + H + P, 25)).toEqual({ value: A, count: 1 });
    expect(__ImageCapEditsForTest.counted("data:image/png;base64,QUJD data:image/png;base64,QUJD", 0))
      .toEqual({ value: "[image-redacted]", count: 1 });
    expect(__ImageCapEditsForTest.counted("[image-redacted] __REDACTED__", 0))
      .toEqual({ value: "[image-redacted] __REDACTED__", count: 0 });
  });

  it("R27 manual FITracer does not invent the auto-mode count attribute", () => {
    const exporter = new InMemorySpanExporter();
    const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
    const tracer = new FITracer({ tracer: provider.getTracer("image-failclosed-manual"), traceConfig: { base64ImageMaxLength: CAP, piiRedaction: false } });
    const attrs = Object.freeze({ exact: URL, decoded: "%64ata:image/png;base64," + P, ordinary: "keep" });
    tracer.startSpan("case", { attributes: attrs }).end();
    const results = exporter.getFinishedSpans();
    expect(results).toHaveLength(1);
    expect(results[0].attributes.exact).toBe(A);
    expect(results[0].attributes.decoded).toBe(B);
    expect(results[0].attributes["traceai.capture.dropped_content_count"]).toBeUndefined();
    expect(attrs.exact).toBe(URL);
  });
});


describe("r3 bounded continuation witnesses", () => {
  it.each([
    BS + 'u0064ata:image/png;BASE64;name="a' + BS + '" b",' + P,
    BS + 'u0064ata:image/png;name="a' + BS + '" b' + "q".repeat(300) + '";base64,' + P,
    BS + 'u0064ata:image/png;name="a' + BS + '" b' + "q".repeat(4096) + '";base64,' + P,
  ])("R28 hidden prefix with original MIME quoted-pair selects B: %s", raw => {
    expect(capBase64Images(raw, CAP)).toBe("[image-redacted]");
    expect(capBase64Images("caption " + raw + " tail", CAP)).toBe("[image-redacted]");
    expect(capBase64Images(JSON.stringify({ value: raw, note: "keep" }), CAP))
      .toBe('{"value":"[image-redacted]","note":"keep"}');
    expect(capBase64Images(JSON.stringify({ [raw]: "keep", note: "retain" }), CAP))
      .toBe('{"[image-redacted]":"keep","note":"retain"}');
  });

  it("R29 declared ownership survives transported data and source edges", () => {
    const payload = BS + "u0051UJD" + P;
    const image = JSON.stringify({ type: "image", data: payload, note: "keep" });
    expect(capBase64Images(image, CAP)).toBe('{"type":"image","data":"__REDACTED__","note":"keep"}');
    const text = JSON.stringify({ type: "text", data: payload, note: "keep" });
    expect(capBase64Images(text, CAP)).toBe(text);
    const source = JSON.stringify({ type: "image", source: { data: payload }, other: [{ data: payload }] });
    expect(capBase64Images(source, CAP)).toBe('{"type":"image","source":{"data":"__REDACTED__"},"other":[{"data":' + JSON.stringify(payload) + '}]}');
    expect(capBase64Images(image, 164)).toBe(image);
    expect(capBase64Images(image, 163)).toBe('{"type":"image","data":"__REDACTED__","note":"keep"}');
    const legal = JSON.stringify({ type: "image", data: P + BS + "t" + P });
    expect(capBase64Images(legal, CAP)).toBe(legal);
  });

  it("R30 D1 includes the old 267-unit positive and refuses only outside 1024", () => {
    const old = "data:image/" + "x ".repeat(124) + ";base64,";
    expect(capBase64Images("before " + old + P + " after", CAP)).toBe("before __REDACTED__ after");
    const inside = "data:image/" + "x ".repeat(502) + "x;base64,";
    const outside = "data:image/" + "x ".repeat(503) + ";base64,";
    expect(inside.length).toBe(1024);
    expect(outside.length).toBe(1025);
    expect(capBase64Images(inside + P, CAP)).toBe("__REDACTED__");
    expect(capBase64Images(outside + P, CAP)).toBe(outside + P);
  });

  it("R31 D2 uses original extent but preserves genuinely noncounting controls", () => {
    expect(capBase64Images("caption %64ata:image/png;base64,QUJD tail", 26)).toBe("[image-redacted]");
    expect(capBase64Images("caption %64ata:image/png;base64,QUJD tail", 28))
      .toBe("[image-redacted]");
    const noFlags = "data:image/ ordinary ".repeat(256);
    expect(capBase64Images(noFlags, 0)).toBe(noFlags);
    expect(__ImageCapEditsForTest.counted(noFlags, 0)).toEqual({ value: noFlags, count: 0 });
    const emptyRuns = 'data:image/png;base64," '.repeat(256);
    expect(capBase64Images(emptyRuns, 0)).toBe(B);
  });

  it("R32 timebox round-seven witnesses retain no apparent-flag survivor", () => {
    for (const run of ["QUJD", P]) {
      const raw = JSON.stringify({ value: 'data:image/png;name=";base64,' + run + '";base64,' + P, note: "keep" });
      expect(capBase64Images(raw, CAP)).toBe('{"value":"__REDACTED__","note":"keep"}');
    }
    const pair = 'data:image/png;name="a' + BS + '";base64,AAAA";base64,' + P;
    expect(capBase64Images(pair, CAP)).toBe("__REDACTED__");
    const decoy = 'data:image/svg+xml;name="' + BS + '%22;base64,' + P + '",%3Csvg%3E';
    expect(capBase64Images(decoy, CAP)).toBe('__REDACTED__",%3Csvg%3E');
    const legal = '{"type":"image","data":"' + P + BS + "t" + P + BS + '"tail","note":"keep"} null';
    expect(capBase64Images(legal, CAP)).toBe(legal);
    const expensive = BS + "u005c".repeat(256) + "u0078ordinary " + "z".repeat(8192);
    const malformed = '{"expensive":' + JSON.stringify(expensive) + ',"exact":"' + H + "A".repeat(42) + '","note":"keep",}';
    expect(capBase64Images(malformed, CAP)).toBe("[image-redacted]");
  });
});
