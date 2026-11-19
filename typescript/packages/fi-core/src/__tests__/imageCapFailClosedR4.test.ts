import { describe, expect, it, jest } from "@jest/globals";
import { capBase64Images, __ImageCapEditsForTest } from "../trace/trace-config/imageCap";

// Source-first r4 controls. NOT_RUN: no observed RED or GREEN is claimed.
// Retains the previous packet's witnesses. Existing-test migrations are only
// proposed in 03b-pending-ptc.json, never applied by this source-first packet.
// Outputs below are literal layouts, not computed by another recognizer.
const BS = String.fromCharCode(92);
const P = "QUJD".repeat(40);
const cap = (value: string, maximum = 64): string => capBase64Images(value, maximum) as string;
const hexText = (value: string): string => value.split("").map(
  unit => BS + "u" + unit.charCodeAt(0).toString(16).padStart(4, "0"),
).join("");

const nonfinal = BS + 'u0064ata:image/png;' + BS + 'u0062ase64;name="a' + BS + '" b",' + P;
const longFinal = BS + 'u0064ata:image/png;name="a' + BS + '" b' + "q".repeat(1100) + '";' + BS + 'u0062ase64,' + P;
const nestedPayload = BS + 'u0064ata:image/png;base64;name="a' + BS + '" b",QUJD' + (BS + "u005cu0041").repeat(160);
const transportedHeader = BS + 'u0064ata:image/png;' + BS + 'u0062ase64;name="a' + BS + '" b",';
const longHead = 'data:image/png;name="a' + BS + '" ' + "q".repeat(1200) + '"';
const longHiddenFlag = longHead + ';ba' + BS + 'u0073e64,';

describe("r4 historical precise-recognizer witnesses", () => {
  it.each([
    ["TRANSPORTED_NONFINAL_FLAG", nonfinal],
    ["TRANSPORTED_LONG_FINAL_FLAG", longFinal],
    ["NESTED_TRANSPORTED_PAYLOAD", nestedPayload],
  ])("TS-FAILCLOSED-R3-A-001/%s has a literal whole-scope oracle", (_id, value) => {
    expect(cap(value)).toBe("[image-redacted]");
    expect(cap("caption " + value + " tail")).toBe("[image-redacted]");
    expect(cap(JSON.stringify({ value, keep: "ordinary" })))
      .toBe('{"value":"[image-redacted]","keep":"ordinary"}');
    expect(cap(JSON.stringify({ [value]: "ordinary", keep: 1 })))
      .toBe('{"[image-redacted]":"ordinary","keep":1}');
  });

  it("TS-FAILCLOSED-R3-B-001 registers identical C1/C2 ranges only once", () => {
    const add = jest.spyOn(__ImageCapEditsForTest.prototype, "add");
    try {
      expect(__ImageCapEditsForTest.counted("data:image/png;base64," + "A".repeat(80), 64))
        .toEqual({ value: "__REDACTED__", count: 1 });
      expect(add.mock.calls).toEqual([[0, 102]]);
    } finally {
      add.mockRestore();
    }
  });

  it("D1 close flags union while flags beyond the window remain independent", () => {
    const short = "data:image/png;base64," + "A".repeat(80);
    expect(cap("lead " + short + " between " + short + " tail")).toBe("lead __REDACTED__ tail");
    expect(__ImageCapEditsForTest.counted("lead " + short + " between " + short + " tail", 64))
      .toEqual({ value: "lead __REDACTED__ tail", count: 1 });
    const long = "data:image/png;base64," + "A".repeat(1200);
    expect(cap("lead " + long + " between " + short + " tail"))
      .toBe("lead __REDACTED__ between __REDACTED__ tail");
  });

  it("A-003 inherits the exhausted transport budget through revealed strict JSON", () => {
    expect(cap("%252522%25252541%252522")).toBe("[image-redacted]");
    expect(__ImageCapEditsForTest.counted("%252522%25252541%252522", 64))
      .toEqual({ value: "[image-redacted]", count: 1 });
    // Three real transport passes that do reach a fixed point remain harmless.
    expect(cap("%252541")).toBe("%252541");
  });
});

describe("r4 bound backstop-lens constructions inside the precise recognizer", () => {
  it.each([0x17b4, 0x17b5, 0x115f, 0x3164, 0xffa0, 0x1d173, 0xe0000])(
    "BK-A-TS-001 consumes an ignorable-split raw MIME continuation U+%i",
    code => {
      const header = 'data:image/png;base64;name="a' + BS + '" b",';
      const value = header + P + String.fromCodePoint(code) + P;
      // Unlike the dropped output backstop's B oracle, r4 has an exact raw
      // prefix and endpoint, including the complete continuation: Redaction A.
      expect(cap(value)).toBe("__REDACTED__");
      expect(cap("left " + value + " right")).toBe("left __REDACTED__ right");
      expect(__ImageCapEditsForTest.counted(value, 64))
        .toEqual({ value: "__REDACTED__", count: 1 });
    },
  );

  it("BK-A-TS-002 uses the full named table, including multi-character fjlig", () => {
    const value = transportedHeader + "&fjlig;".repeat(80);
    expect(cap(value)).toBe("[image-redacted]");
    expect(cap(JSON.stringify({ value, keep: "ordinary" })))
      .toBe('{"value":"[image-redacted]","keep":"ordinary"}');
  });

  it("BK-A-TS-002/PADDED_NUMERIC_REFERENCE accepts all leading zero digits", () => {
    const value = transportedHeader + ("&#" + "0".repeat(8) + "65;").repeat(80);
    expect(cap(value)).toBe("[image-redacted]");
  });

  it("BK-A-TS-003 keeps a quoted comma out of the payload delimiter decision", () => {
    const value = BS + 'u0064ata:image/png;' + BS + 'u0062ase64;name="a' + BS + '" b,c",' + P;
    expect(cap(value)).toBe("[image-redacted]");
  });

  it("BK-A-TS-003/LONG_FINAL_FLAG_AFTER_QUOTED_COMMA has no flat-window dependency", () => {
    const value = BS + 'u0064ata:image/png;name="a' + BS + '" b,' + "q".repeat(1100) + '";' + BS + "u0062ase64," + P;
    expect(cap(value)).toBe("[image-redacted]");
  });

  it("BK-A-TS-004 measures the 285-unit raw extent, not only the decoded payload", () => {
    const value = transportedHeader + (BS + "u0041").repeat(40);
    expect(value.length).toBe(285);
    expect(cap(value, 128)).toBe("[image-redacted]");
    expect(cap(value, 285)).toBe(value);
    expect(cap(value, 286)).toBe(value);
  });

  it("BK-B-001/numeric retains unlimited leading-zero hex reference coverage", () => {
    const value = longHiddenFlag + "&#x00000051;UJD".repeat(40);
    expect(cap(value)).toBe("[image-redacted]");
  });

  it("BK-B-001/named admits NewLine before the base64 run", () => {
    const value = longHiddenFlag + "&NewLine;" + P;
    expect(cap(value)).toBe("[image-redacted]");
  });

  it("supplementary JSON surrogate pairs are combined before ignorable removal", () => {
    const value = longHiddenFlag + BS + "udb40" + BS + "udc00" + P;
    expect(cap(value)).toBe("[image-redacted]");
  });

  it("BK-B-003 retains D2 raw extent at the decoded equality boundary", () => {
    const value = longHiddenFlag + P;
    expect(value.length).toBe(1399);
    expect(cap(value, 1393)).toBe("[image-redacted]");
    expect(cap(value, 1399)).toBe(value);
    expect(cap(value, 1400)).toBe(value);
  });

  it("BK-B-004 counts only the final replacement, not an erased intermediate A", () => {
    const value = "data:image/png;base64," + P + "!" + longHiddenFlag + P;
    expect(__ImageCapEditsForTest.counted(value, 64))
      .toEqual({ value: "[image-redacted]", count: 1 });
    const document = JSON.stringify({ value, other: "data:image/png;base64," + P });
    expect(__ImageCapEditsForTest.counted(document, 64))
      .toEqual({ value: '{"value":"[image-redacted]","other":"__REDACTED__"}', count: 2 });
  });
});

describe("r4 benign controls and structural JSON recursion", () => {
  it.each([
    "data:image/png;base64=ordinary,",
    "data:image/png;base64suffix,",
    "data:image/png;name=base64,",
    'data:image/png;name="x;base64;y",',
    'data:image/png;name="x' + BS + '";base64;y",',
  ])("neither C1 nor C2 matches the benign header %s", header => {
    const value = header + P;
    expect(cap(value, 0)).toBe("[image-redacted]");
    const document = JSON.stringify({ value, keep: "ordinary" });
    expect(cap(document, 0)).toBe('{"value":"[image-redacted]","keep":"ordinary"}');
  });

  it.each([
    '["data:image/png;name=", ";base64,QUJD"]',
    '{"data:image/png;name=":";base64,QUJD"}',
  ])("Q1 never joins split tokens, including transported syntax: %s", document => {
    for (const value of [document, encodeURIComponent(document), hexText(document)]) {
      expect(cap(value, 0)).toBe(value);
    }
    const referenceDocument = document.startsWith("{")
      ? "&lbrace;" + document.slice(1, -1) + "&rbrace;"
      : "&lbrack;" + document.slice(1, -1) + "&rbrack;";
    expect(cap(referenceDocument, 0)).toBe(referenceDocument);
  });

  it.each([7, 8, 9])("structural string wrappers are free at depth %i", depth => {
    let ordinary = '{"data:image/png;name=":";base64,QUJD","keep":"ordinary"}';
    let image = '{ "data":"' + P + '","media_type":"image/png","keep":9007199254740993 }';
    let expected = '{ "data":"__REDACTED__","media_type":"image/png","keep":9007199254740993 }';
    for (let i = 0; i < depth; i++) {
      ordinary = JSON.stringify(ordinary);
      image = JSON.stringify(image);
      expected = JSON.stringify(expected);
    }
    expect(cap(ordinary, 0)).toBe(ordinary);
    expect(cap(image)).toBe(expected);
  });

  it("JSON-escaped headers do not inflate exact or below-cap measurements", () => {
    const value = 'data:image/png;name="sample";base64,' + P;
    for (const document of [
      JSON.stringify("[" + value + "]"),
      JSON.stringify(JSON.stringify(value)),
      JSON.stringify({ value, keep: "ordinary" }),
    ]) {
      expect(cap(document, value.length)).toBe(document);
      expect(cap(document, value.length + 1)).toBe(document);
    }
    expect(cap(JSON.stringify({ value, keep: "ordinary" })))
      .toBe('{"value":"__REDACTED__","keep":"ordinary"}');
  });

  it("C1 optional whitespace does not turn base64 values or suffixes into flags", () => {
    expect(cap("data:image/png; base64 ," + P)).toBe("__REDACTED__");
    const equals = "data:image/png; base64=ordinary ," + P;
    const suffix = "data:image/png; base64suffix ," + P;
    expect(cap(equals, 0)).toBe("[image-redacted]");
    expect(cap(suffix, 0)).toBe("[image-redacted]");
  });

  it("C2 trims a non-final flag without decoding raw quoted-pair syntax", () => {
    const value = 'data:image/png;  base64  ;name="a' + BS + '" b,c",' + P;
    expect(cap(value)).toBe("__REDACTED__");
  });

  it("retains non-image text, ordinary URLs and small images byte-for-byte", () => {
    for (const value of [
      "ordinary words", "https://example.invalid/a,b?c=1",
      "data:audio/wav;base64,QUJD", "data:image/svg+xml,%3Csvg%3E",
      "data:image/png;base64,QUJD", "emoji " + String.fromCodePoint(0x1f468, 0x200d, 0x1f469) + " family",
    ]) expect(cap(value)).toBe(value);
  });

  it("D4 rejects over-cap ordinary escaped text before scanning", () => {
    const unit = BS + "t" + BS + "u0078" + BS + "/";
    const value = "ordinary " + unit.repeat(Math.ceil(1_048_576 / unit.length)) + " end";
    expect(value.length).toBe(1_048_593);
    expect(cap(value, 1000)).toBe("[image-redacted]");
  });

  it("D4 cap plus one is whole B even with an unbounded caller threshold", () => {
    expect(cap("x".repeat(1_048_577), Number.MAX_SAFE_INTEGER)).toBe("[image-redacted]");
    expect(cap("x".repeat(1_048_576), Number.MAX_SAFE_INTEGER)).toBe("x".repeat(1_048_576));
  });

  it("JSON lexical escapes are not unfenced transport inflation", () => {
    const url = "data:image/png;base64,QUJD";
    const document = '{ "value":"' + hexText(url) + '","same":1,"same":2 }';
    expect(cap(document, 26)).toBe(document);
    expect(cap(document, 25)).toBe('{ "value":"__REDACTED__","same":1,"same":2 }');
    expect(cap(hexText(url), 26)).toBe("[image-redacted]");
  });

  it("seventeen nested string documents hit the sixteen-fence structural bound", () => {
    let input = "ordinary", expected = "[image-redacted]";
    for (let i = 0; i < 17; i++) input = JSON.stringify(input);
    for (let i = 0; i < 16; i++) expected = JSON.stringify(expected);
    expect(cap(input)).toBe(expected);
  });
});

// r4c3: additional literal controls for allocation-only optimizations.
// The six protected 1MB timings remain in their original files with LIMIT_MS=200.
describe("r4c3 dense decoding and no-change path boundaries", () => {
  it.each([8191, 8192, 8193, 16385])("dense mapped atoms cross output chunks at %i", length => {
    const payload = (BS + "/").repeat(length);
    expect(cap("left data:image/png;base64," + payload + " right"))
      .toBe("left __REDACTED__ right");
    expect(cap("left data:image" + BS + "/png;base64," + payload + " right"))
      .toBe("[image-redacted]");
    const ordinary = "ordinary " + (BS + "t" + BS + "u0078" + BS + "/").repeat(length) + " end";
    expect(cap(ordinary)).toBe(ordinary);
  });

  it("a late declaration and transported token survive many under-cap tokens", () => {
    const small = '"a",'.repeat(2048) + '"a"';
    const input = '{"ordinary":[' + small + '],"data":"' + P + '","type":"image","hidden":"' + BS + BS + 'u0064ata:image/png;base64,' + P + '"}';
    const expected = '{"ordinary":[' + small + '],"data":"__REDACTED__","type":"image","hidden":"[image-redacted]"}';
    expect(cap(input)).toBe(expected);
  });

  it("short no-change checks do not bypass transport, ignorables or Q1 depth", () => {
    expect(cap("%25252541", 1000)).toBe("[image-redacted]");
    expect(cap("data:im" + String.fromCodePoint(0x200b) + "age/png;base64,QUJD", 0))
      .toBe("[image-redacted]");
    expect(cap("data:image/png;base64,QUJD", 0)).toBe("[image-redacted]");
    expect(cap('["data:image/png;name=",";base64,QUJD"]', 0))
      .toBe('["data:image/png;name=",";base64,QUJD"]');
  });

  it("coalesced identity ranges retain the C1 union across unrelated MIME text", () => {
    const input = 'lead data:image/png;name="x;base64,QUJD" text ;base64,' + P + " tail";
    expect(cap(input)).toBe("lead __REDACTED__ tail");
    expect(__ImageCapEditsForTest.counted(input, 64))
      .toEqual({ value: "lead __REDACTED__ tail", count: 1 });
    // A changing view must still keep per-header raw certificates and B fallback.
    expect(cap(input + "!" + BS + "u0064ata:image/png;base64," + P))
      .toBe("[image-redacted]");
  });

  it("D4 ordinary boundary pair also holds with a finite caller cap", () => {
    const ordinary = "x".repeat(1_048_576);
    expect(cap(ordinary, 1000)).toBe(ordinary);
    expect(cap(ordinary + "x", 1000)).toBe("[image-redacted]");
  });
});

// r4c4: literal output/count oracles for the numeric simple-escape path.
// No timing threshold or previous case is changed; these are not benchmarks.
describe("r4c4 simple-escape output equivalence", () => {
  it.each([
    ["simple", BS + "/"],
    ["unicode", BS + "u002f"],
    ["mixed", BS + "/" + BS + "u0041B" + BS + "r" + BS + "n" + "%41&#65;"],
  ])("dense %s payloads retain exact A and hidden-prefix B", (_label, unit) => {
    const payload = unit.repeat(8193) + "A";
    expect(__ImageCapEditsForTest.counted("left data:image/png;base64," + payload + " right", 64))
      .toEqual({ value: "left __REDACTED__ right", count: 1 });
    for (const slash of [BS + "/", BS + "u002f", BS + "u005c/"]) {
      expect(__ImageCapEditsForTest.counted("left data:image" + slash + "png;base64," + payload + " right", 64))
        .toEqual({ value: "[image-redacted]", count: 1 });
    }
  });

  it.each([
    ["tabs/newlines", BS + "t" + BS + "n"],
    ["unicode", BS + "u0078" + BS + "u002f"],
    ["mixed", BS + "t" + BS + "u0078" + BS + "/%41&#65;"],
    ["all simple keys", BS + '"!' + BS + BS + "!" + BS + "/" + BS + "b" + BS + "f" + BS + "n" + BS + "r" + BS + "t"],
  ])("ordinary dense %s keeps every original escape byte", (_label, unit) => {
    const value = "ordinary " + unit.repeat(8193) + " end" + BS + "q" + BS + "u12!" + BS;
    expect(__ImageCapEditsForTest.counted(value, 64)).toEqual({ value, count: 0 });
  });

  it.each(['"', BS, "b", "f", "t"])("simple %s is not a payload alphabet unit", key => {
    const suffix = BS + key + "!tail";
    const value = "left data:image/png;base64," + (BS + "/").repeat(100) + suffix;
    expect(cap(value)).toBe("left __REDACTED__" + suffix);
  });

  it("CR/LF bridge a nonempty run but do not extend its trailing endpoint", () => {
    const newline = BS + "r" + BS + "n";
    const value = "left data:image/png;base64," + newline + (BS + "/").repeat(100) + newline + " tail";
    expect(cap(value)).toBe("left __REDACTED__" + newline + " tail");
    const noDigits = "data:image/png;base64," + newline.repeat(100) + "!";
    expect(cap(noDigits, 0)).toBe("[image-redacted]");
  });

  it("structural JSON unquoting preserves sibling lexemes and declaration ownership", () => {
    const value = '{ "image":"' + (BS + "/").repeat(8193) + '", "type":"image","keep":"' + BS + 't","same":1,"same":2 }';
    const expected = '{ "image":"__REDACTED__", "type":"image","keep":"' + BS + 't","same":1,"same":2 }';
    expect(__ImageCapEditsForTest.counted(value, 64)).toEqual({ value: expected, count: 1 });
  });

  it("simple escapes retain D2 exact-cap boundaries and transport exhaustion", () => {
    const value = "data:image/png;base64," + (BS + "/").repeat(40);
    expect(value.length).toBe(102);
    expect(cap(value, 102)).toBe(value);
    expect(cap(value, 101)).toBe("__REDACTED__");
    const ordinary = "ordinary " + (BS + "t" + BS + "n").repeat(4096) + "%25252541";
    expect(__ImageCapEditsForTest.counted(ordinary, 64))
      .toEqual({ value: "[image-redacted]", count: 1 });
  });
});

// r4c5: additive R-WS / R-TD controls. Existing cases above are unchanged.
describe("r4c5 IMAGECAP-WS-001 joined payload chains", () => {
  const header = "data:image/png;base64,";
  it.each([
    ["SPACE", " "], ["TAB", "\t"], ["FF", "\f"],
    ["SPACE_LF", " \n"], ["CRLF_SPACE", "\r\n "],
    ["percent_SPACE", "%20"], ["percent_TAB", "%09"],
    ["percent_FF", "%0C"], ["percent_LF", "%0A%20"],
    ["decimal_SPACE", "&#32;"], ["hex_TAB", "&#x9;"],
    ["named_NewLine_SPACE", "&NewLine;&#32;"],
    ["json_SPACE", BS + "u0020"], ["json_TAB", BS + "t"],
    ["json_FF", BS + "f"], ["two_pass_SPACE", "%2520"],
    ["three_pass_SPACE", "%252520"],
  ])("R-WS/%s selects whole B in a value and one Q1 token", (_label, separator) => {
    const value = header + Array(40).fill("QUJD").join(separator);
    expect(__ImageCapEditsForTest.counted(value, 64))
      .toEqual({ value: "[image-redacted]", count: 1 });
    const doc = '{ "m":' + JSON.stringify("x " + value + " y") + ',"same":1,"same":2,"keep":"ordinary" }';
    expect(cap(doc)).toBe('{ "m":"[image-redacted]","same":1,"same":2,"keep":"ordinary" }');
    expect(cap(JSON.stringify({ [value]: 0, keep: 1 })))
      .toBe('{"[image-redacted]":0,"keep":1}');
  });

  it.each(["%0A", "&NewLine;"])("R-WS/%s retains legacy LF-only A coverage", separator => {
    const value = header + Array(40).fill("QUJD").join(separator);
    expect(cap(value)).toBe("__REDACTED__");
  });

  it("R-WS mixed raw/transported/ignorable separators share one chain", () => {
    const cycle = "QUJD QUJD\tQUJD" + String.fromCodePoint(0x200b) + "QUJD" + BS + "u0020";
    const value = header + cycle.repeat(10) + "QUJD";
    expect(cap(value)).toBe("[image-redacted]");
    expect(cap(JSON.stringify({ m: value, keep: "ordinary" })))
      .toBe('{"m":"[image-redacted]","keep":"ordinary"}');
  });

  it("R-WS (i) an over-cap later chunk subsumes the first A and its count", () => {
    const value = "diagnostic " + header + P + " " + P;
    expect(__ImageCapEditsForTest.counted(value, 64))
      .toEqual({ value: "[image-redacted]", count: 1 });
    expect(cap(JSON.stringify({ m: value, keep: "ordinary" })))
      .toBe('{"m":"[image-redacted]","keep":"ordinary"}');
  });

  it("R-WS (i) many sub-cap later chunks cannot survive the first A", () => {
    expect(cap(header + P + " " + Array(20).fill("QUJD").join("\t")))
      .toBe("[image-redacted]");
    expect(cap(header + P + " " + "A".repeat(64))).toBe("__REDACTED__ " + "A".repeat(64));
    expect(cap(header + P + " " + "A".repeat(65))).toBe("[image-redacted]");
  });

  it("R-WS (i) preserves a short caption after the exact first A", () => {
    const value = "diagnostic " + header + P + " caption retained";
    expect(cap(value, (header + P).length - 1)).toBe("diagnostic __REDACTED__ caption retained");
    expect(cap(header + P + "%20caption%09retained"))
      .toBe("__REDACTED__%20caption%09retained");
  });

  it("R-WS (ii)-(iii) joined cap boundaries ignore separator width", () => {
    for (const separator of [" ", "\t", "\f", "%20", "&#00000032;", BS + "u0020"]) {
      const value = header + "QUJD" + separator + "QUJD";
      expect(cap(value, 30)).toBe(value);
      expect(cap(value, 31)).toBe(value);
      expect(cap(value, 29)).toBe("[image-redacted]");
    }
    expect(cap(header + " \t\fQUJD QUJD", 29)).toBe("[image-redacted]");
  });

  it("R-WS retains D2 non-whitespace transport inflation in a joined chain", () => {
    const value = header + "%51UJD %51UJD";
    expect(cap(value, 34)).toBe(value);
    expect(cap(value, 33)).toBe("[image-redacted]");
  });

  it("R-WS does not join VT, NBSP, punctuation, non-image flags or Q1 siblings", () => {
    for (const separator of ["\v", String.fromCodePoint(0xa0), "!"]) {
      const value = header + "QUJD" + separator + P;
      expect(cap(value)).toBe(value);
    }
    const split = JSON.stringify([header + "QUJD", "QUJD ".repeat(40)]);
    expect(cap(split)).toBe(split);
    const falseFlag = "data:image/png;name=base64," + Array(40).fill("QUJD").join(" ");
    expect(cap(falseFlag, 0)).toBe("[image-redacted]");
  });

  it("R-WS covers non-final and long C2 headers without borrowing C1", () => {
    for (const prefix of ["data:image/png;base64;name=ordinary,", "data:image/png;name=" + "q".repeat(1100) + ";base64,"]) {
      expect(cap(prefix + P + " " + P)).toBe("[image-redacted]");
    }
  });

  it("R-WS never lets a later C1 A erase an earlier whole-scope decision", () => {
    const value = header + P + " " + P + "!" + header + P;
    expect(cap(value)).toBe("[image-redacted]");
  });
});

describe("r4c5 IMAGECAP-TD-001 transported header delimiters", () => {
  const cases: Array<[string, string]> = [
    ["percent_comma", ";base64%2C"], ["json_comma", ";base64" + BS + "u002c"],
    ["named_comma", ";base64&comma;"], ["decimal_comma", ";base64&#44;"],
    ["hex_comma", ";base64&#x00002c;"],
    ["percent_semicolon", "%3Bbase64,"], ["json_semicolon", BS + "u003bbase64,"],
    ["named_semicolon", "&semi;base64,"], ["decimal_semicolon", "&#59;base64,"],
    ["hex_semicolon", "&#x00003b;base64,"], ["percent_both", "%3Bbase64%2C"],
    ["two_pass_comma", ";base64%252C"], ["three_pass_comma", ";base64%25252C"],
  ];
  it.each(cases)("R-TD/%s short/long plain values and independent keys/values", (_id, flag) => {
    for (const length of [1, 1024, 4096]) {
      const value = "data:image/png;name=" + "q".repeat(length) + flag + P;
      expect(__ImageCapEditsForTest.counted("left " + value + " right", 64))
        .toEqual({ value: "[image-redacted]", count: 1 });
      expect(cap('{"x":' + JSON.stringify(value) + ',"keep":1}'))
        .toBe('{"x":"[image-redacted]","keep":1}');
      expect(cap("{" + JSON.stringify(value) + ':0,"keep":1}'))
        .toBe('{"[image-redacted]":0,"keep":1}');
      expect(cap("{" + JSON.stringify(value) + ':0,"x":' + JSON.stringify(value) + ',"keep":1}'))
        .toBe('{"[image-redacted]":0,"x":"[image-redacted]","keep":1}');
    }
  });

  it.each([1, 1024, 4096])("R-TD raw delimiter pair keeps precise A, header length %i", length => {
    const value = "data:image/png;name=" + "q".repeat(length) + ";base64," + P;
    expect(cap("left " + value + " right")).toBe("left __REDACTED__ right");
    expect(cap('{"x":' + JSON.stringify(value) + ',"keep":1}'))
      .toBe('{"x":"__REDACTED__","keep":1}');
    expect(cap("{" + JSON.stringify(value) + ':0,"keep":1}'))
      .toBe('{"__REDACTED__":0,"keep":1}');
  });

  it("R-TD non-final flags and non-flag transported separators count", () => {
    const long = "q".repeat(1100);
    for (const value of [
      "data:image/png%3Bname=" + long + ";base64," + P,
      "data:image/png;base64%3Bname=" + long + "," + P,
      "data:image/png%3Bbase64;name=" + long + "," + P,
    ]) expect(cap(value)).toBe("[image-redacted]");
  });

  it("R-TD provenance excludes ignorable normalization and unrelated atoms", () => {
    const value = "data:image/png;name=" + "q".repeat(1100) + String.fromCodePoint(0x200b) + ";base64," + P;
    expect(cap(value)).toBe("__REDACTED__");
    const ordinaryAtom = "data:image/png;name=" + "q".repeat(1100) + "%41;base64," + P;
    expect(cap(ordinaryAtom)).toBe("__REDACTED__");
  });

  it("R-TD does not turn a raw quoted-pair false flag into a real flag", () => {
    const value = 'data:image/png;name="x' + BS + '";base64;y' + "q".repeat(1100) + '",%51UJD' + P;
    expect(cap(value, 0)).toBe("[image-redacted]");
    const transportedComma = value.replace(",%51", "%2C%51");
    expect(cap(transportedComma, 0)).toBe("[image-redacted]");
  });

  it("R-TD requires a complete flag and nonempty payload", () => {
    for (const flag of ["base64suffix", "base64=ordinary", "name=base64"]) {
      const value = "data:image/png;name=" + "q".repeat(1100) + "%3B" + flag + "%2C" + P;
      expect(cap(value, 0)).toBe("[image-redacted]");
    }
    const empty = "data:image/png;name=" + "q".repeat(1100) + ";base64%2C!";
    expect(cap(empty, 0)).toBe("[image-redacted]");
  });

  it("R-TD under-cap transport and Q1 lexical escapes remain byte-identical", () => {
    const short = "data:image/png;base64%2CQUJD";
    expect(cap(short, short.length)).toBe(short);
    expect(cap(short, short.length - 1)).toBe("[image-redacted]");
    const lexical = '{"x":"data:image/png;base64' + BS + 'u002cQUJD"}';
    expect(cap(lexical, 26)).toBe(lexical);
    expect(cap(lexical, 25)).toBe('{"x":"__REDACTED__"}');
  });
});

// r4c6: additive R-REF / R-SUB / R-FIX controls. All preceding bytes are retained.
// Literal oracles only; the packet does not claim these tests were executed.
describe("r4c6 R-REF character-reference ownership in MIME tokens", () => {
  const flags: Array<[string, string]> = [
    ["decimal", "ba&#115;e64"],
    ["hexadecimal", "ba&#x73;e64"],
    ["padded_decimal", "ba&#00000000115;e64"],
    ["padded_hexadecimal", "ba&#x0000000073;e64"],
    ["long_zero_decimal", "ba&#" + "0".repeat(64) + "115;e64"],
    ["long_zero_hexadecimal", "ba&#x" + "0".repeat(64) + "73;e64"],
    ["semicolonless_decimal", "ba&#115e64"],
    ["named_ignorable", "ba&ApplyFunction;se64"],
    ["named_shy", "ba&shy;se64"],
  ];

  it("R4C5-TS-REDACTION-COMPLETENESS-001 literal example", () => {
    const value = "data:image/png;ba&#115;e64;name=x," + "QUJD".repeat(20);
    expect(__ImageCapEditsForTest.counted(value, 64))
      .toEqual({ value: "[image-redacted]", count: 1 });
  });

  it.each(flags)("R-REF/%s non-final and long-final raw/key/value/scalar", (_id, flag) => {
    for (const value of [
      "data:image/png;" + flag + ";name=x," + P,
      "data:image/png;name=" + "q".repeat(1024) + ";" + flag + "," + P,
    ]) {
      expect(__ImageCapEditsForTest.counted(value, 64))
        .toEqual({ value: "[image-redacted]", count: 1 });
      expect(cap("caption " + value + " tail")).toBe("[image-redacted]");
      expect(cap("{" + JSON.stringify(value) + ':0,"keep":1}'))
        .toBe('{"[image-redacted]":0,"keep":1}');
      expect(cap('{"x":' + JSON.stringify(value) + ',"keep":1}'))
        .toBe('{"x":"[image-redacted]","keep":1}');
      expect(cap(JSON.stringify(value))).toBe('"[image-redacted]"');
    }
  });

  it.each([
    ["BK-A-PY-002/decimal", "ba&#00000000115;e64"],
    ["BK-A-PY-002/hexadecimal", "ba&#x0000000073;e64"],
  ])("%s and its key/value/scalar historical members", (_id, flag) => {
    const value = longHead + ";" + flag + "," + P;
    expect(cap(value)).toBe("[image-redacted]");
    expect(cap("{" + JSON.stringify(value) + ':0,"keep":1}'))
      .toBe('{"[image-redacted]":0,"keep":1}');
    expect(cap('{"x":' + JSON.stringify(value) + ',"keep":1}'))
      .toBe('{"x":"[image-redacted]","keep":1}');
    expect(cap(JSON.stringify(value))).toBe('"[image-redacted]"');
    const first = __ImageCapEditsForTest.counted(value, 64);
    expect(__ImageCapEditsForTest.counted(first.value, 64))
      .toEqual({ value: "[image-redacted]", count: 0 });
  });

  it.each(flags)("R-REF/%s retains exact and below-cap bytes", (_id, flag) => {
    const value = "data:image/png;" + flag + ";name=x,QUJD";
    const document = '{"x":' + JSON.stringify(value) + ',"keep":1}';
    expect(cap(value, value.length)).toBe(value);
    expect(cap(value, value.length + 1)).toBe(value);
    expect(cap(value, value.length - 1)).toBe("[image-redacted]");
    expect(cap(document, value.length)).toBe(document);
    expect(cap(document, value.length - 1)).toBe('{"x":"[image-redacted]","keep":1}');
  });

  it("R-REF reference terminators never introduce separate false flags", () => {
    for (const header of [
      "data:image/png;ba&#115;e64suffix;name=x,",
      "data:image/png;ba&#115;e64=ordinary;name=x,",
      "data:image/png;name=ba&#115;e64;name=x,",
      "data:image/png;name=&amp;base64;name=x,",
      'data:image/png;name="x;ba&#115;e64;y",',
      'data:image/png;name="x' + BS + '";ba&#115;e64;y",',
    ]) {
      const value = header + P;
      expect(__ImageCapEditsForTest.counted(value, 0)).toEqual({ value: "[image-redacted]", count: 1 });
      const document = JSON.stringify({ value, keep: "ordinary" });
      expect(cap(document, 0)).toBe('{"value":"[image-redacted]","keep":"ordinary"}');
    }
  });
});

describe("r4c6 R-SUB subtype recognition in admitted views", () => {
  const subtypes: Array<[string, string]> = [
    ["literal_U200B", "p" + String.fromCodePoint(0x200b) + "ng"],
    ["json_U200B", "p" + BS + "u200bng"],
    ["percent_U200B", "p%E2%80%8Bng"],
    ["numeric_U200B", "p&#8203;ng"],
    ["named_ignorable", "p&ApplyFunction;ng"],
    ["literal_supplementary", "p" + String.fromCodePoint(0xe0000) + "ng"],
    ["json_surrogate_pair", "p" + BS + "udb40" + BS + "udc00ng"],
    ["json_n", "p" + BS + "u006eg"],
    ["percent_n", "p%6Eg"],
    ["numeric_n", "p&#110;g"],
    ["two_pass_n", "p%256Eg"],
    ["three_pass_n", "p%25256Eg"],
  ];

  it("R4C5-TS-REDACTION-COMPLETENESS-002 literal and additional examples", () => {
    for (const subtype of ["p" + String.fromCodePoint(0x200b) + "ng", "p" + BS + "u006eg"]) {
      expect(cap("data:image/" + subtype + ";base64;name=x," + "QUJD".repeat(20)))
        .toBe("[image-redacted]");
    }
  });

  it.each(subtypes)("R-SUB/%s non-final and long-final independent owners", (_id, subtype) => {
    for (const value of [
      "data:image/" + subtype + ";base64;name=x," + P,
      "data:image/" + subtype + ";name=" + "q".repeat(1024) + ";base64," + P,
    ]) {
      expect(__ImageCapEditsForTest.counted(value, 64))
        .toEqual({ value: "[image-redacted]", count: 1 });
      expect(cap("{" + JSON.stringify(value) + ':0,"keep":1}'))
        .toBe('{"[image-redacted]":0,"keep":1}');
      expect(cap('{"x":' + JSON.stringify(value) + ',"keep":1}'))
        .toBe('{"x":"[image-redacted]","keep":1}');
      expect(cap(JSON.stringify(value))).toBe('"[image-redacted]"');
      expect(__ImageCapEditsForTest.counted("{" + JSON.stringify(value) + ':0,"x":' + JSON.stringify(value) + ',"keep":1}', 64))
        .toEqual({ value: '{"[image-redacted]":0,"x":"[image-redacted]","keep":1}', count: 2 });
    }
  });

  it.each(subtypes)("R-SUB/%s D2 raw extent is not JSON lexical width", (_id, subtype) => {
    const value = "data:image/" + subtype + ";base64;name=x,QUJD";
    const document = '{"x":' + JSON.stringify(value) + ',"keep":1}';
    expect(cap(value, value.length)).toBe(value);
    expect(cap(value, value.length + 1)).toBe(value);
    expect(cap(value, value.length - 1)).toBe("[image-redacted]");
    expect(cap(document, value.length)).toBe(document);
    expect(cap(document, value.length - 1)).toBe('{"x":"[image-redacted]","keep":1}');
  });

  it("R-SUB raw-token boundaries cannot terminate inside references", () => {
    for (const subtype of ["p&#32;ng", "p&sol;ng", "p" + BS + "u0020ng"]) {
      const value = "data:image/" + subtype + ";base64;name=x," + P;
      expect(__ImageCapEditsForTest.counted(value, 0)).toEqual({ value: "[image-redacted]", count: 1 });
    }
    const ordinary = "data:image/png;base64;name=x," + P;
    expect(cap("left " + ordinary + " right")).toBe("left __REDACTED__ right");
  });

  it("R-REF plus R-SUB preserves raw quoted-pair ownership", () => {
    const hiddenSubtype = "p" + BS + "u006eg";
    const value = 'data:image/' + hiddenSubtype + ';name="x' + BS + '";ba&#115;e64;y",' + P;
    expect(cap(value, 0)).toBe("[image-redacted]");
    const positive = 'data:image/' + hiddenSubtype + ';name="x' + BS + '";quoted;y";ba&#115;e64;name=z,' + P;
    expect(cap(positive)).toBe("[image-redacted]");
  });
});

describe("r4c6 R-FIX bounded same-recognizer output check", () => {
  const inner = "data:image/png;name=" + "q".repeat(1100) + ";base64," + P;
  const value = "data:image/ !" + inner + "!;base64," + P;

  it("R4C5-TS-COMPATIBILITY-001 first output B, identical second, zero second count", () => {
    expect(value.length).toBe(1470);
    const first = __ImageCapEditsForTest.counted(value, 64);
    expect(first).toEqual({ value: "[image-redacted]", count: 1 });
    expect(cap(value)).toBe("[image-redacted]");
    expect(cap(cap(value))).toBe("[image-redacted]");
    expect(__ImageCapEditsForTest.counted(first.value, 64))
      .toEqual({ value: "[image-redacted]", count: 0 });
  });

  it("R-FIX preserves independent Q1 key/value owners and sibling lexemes", () => {
    const document = '{ "x":' + JSON.stringify(value) + ',"other":"data:image/png;base64,' + P + '","keep":"ordinary","same":1,"same":2 }';
    const expected = '{ "x":"[image-redacted]","other":"__REDACTED__","keep":"ordinary","same":1,"same":2 }';
    const first = __ImageCapEditsForTest.counted(document, 64);
    expect(first).toEqual({ value: expected, count: 2 });
    expect(__ImageCapEditsForTest.counted(first.value, 64)).toEqual({ value: expected, count: 0 });
    const key = '{' + JSON.stringify(value) + ':0,"keep":1}';
    expect(cap(key)).toBe('{"[image-redacted]":0,"keep":1}');
    expect(__ImageCapEditsForTest.counted(cap(key), 64))
      .toEqual({ value: '{"[image-redacted]":0,"keep":1}', count: 0 });
    expect(cap(JSON.stringify(value))).toBe('"[image-redacted]"');
  });

  it("R-FIX does not join Q1 tokens after an independent A shortens one", () => {
    const document = JSON.stringify(["data:image/ !", inner, "!;base64," + P]);
    const expected = '["data:image/ !","__REDACTED__","!;base64,' + P + '"]';
    expect(__ImageCapEditsForTest.counted(document, 64)).toEqual({ value: expected, count: 1 });
    expect(__ImageCapEditsForTest.counted(expected, 64)).toEqual({ value: expected, count: 0 });
    const object = JSON.stringify({ "data:image/ !": inner, tail: "!;base64," + P });
    const expectedObject = '{"data:image/ !":"__REDACTED__","tail":"!;base64,' + P + '"}';
    expect(cap(object)).toBe(expectedObject);
    expect(__ImageCapEditsForTest.counted(expectedObject, 64)).toEqual({ value: expectedObject, count: 0 });
  });

  it("R-FIX B subsumes multiple A edits but preserves a stable precise A", () => {
    const multiple = "data:image/ !" + inner + "!" + inner + "!;base64," + P;
    expect(__ImageCapEditsForTest.counted(multiple, 64))
      .toEqual({ value: "[image-redacted]", count: 1 });
    const precise = "left " + inner + " right";
    expect(__ImageCapEditsForTest.counted(precise, 64))
      .toEqual({ value: "left __REDACTED__ right", count: 1 });
    expect(__ImageCapEditsForTest.counted("left __REDACTED__ right", 64))
      .toEqual({ value: "left __REDACTED__ right", count: 0 });
  });

  it("R-FIX reuses the false-flag grammar, not a broad output detector", () => {
    const falseFlag = 'data:image/png;name="x' + BS + '";base64;y",' + P;
    const input = falseFlag + "!" + inner;
    const expected = falseFlag + "!__REDACTED__";
    expect(__ImageCapEditsForTest.counted(input, 64)).toEqual({ value: expected, count: 1 });
    expect(__ImageCapEditsForTest.counted(expected, 64)).toEqual({ value: expected, count: 0 });
  });

  it("R-FIX preserves inherited budgets and free nested Q1 scopes", () => {
    const document = '{"x":' + JSON.stringify(value) + ',"keep":1}';
    // Only braces and quotes are transported; positive tokens themselves are
    // literal in the revealed document and inherit the remaining two passes.
    const transported = document.replace(/[{}"]/g, unit => "%" + unit.charCodeAt(0).toString(16));
    const expected = "%7b%22x%22:%22[image-redacted]%22,%22keep%22:1%7d";
    const first = __ImageCapEditsForTest.counted(transported, 64);
    expect(first).toEqual({ value: expected, count: 1 });
    expect(__ImageCapEditsForTest.counted(first.value, 64)).toEqual({ value: expected, count: 0 });
    let nested = document, nestedExpected = '{"x":"[image-redacted]","keep":1}';
    for (let depth = 1; depth <= 9; depth++) {
      nested = JSON.stringify(nested); nestedExpected = JSON.stringify(nestedExpected);
      expect(cap(nested)).toBe(nestedExpected);
      expect(__ImageCapEditsForTest.counted(nestedExpected, 64)).toEqual({ value: nestedExpected, count: 0 });
    }
  });
});
