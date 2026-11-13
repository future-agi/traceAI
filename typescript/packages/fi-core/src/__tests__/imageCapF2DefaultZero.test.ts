import { afterEach, beforeEach, describe, expect, it, jest } from "@jest/globals";
import { diag, SpanStatusCode } from "@opentelemetry/api";
import { BasicTracerProvider, InMemorySpanExporter, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { capBase64Images, __ImageCapEditsForTest } from "../trace/trace-config/imageCap";
import { DEFAULT_BASE64_IMAGE_MAX_LENGTH, DefaultTraceConfig } from "../trace/trace-config/constants";
import { generateTraceConfig } from "../trace/trace-config/traceConfig";
import { mask, maskAttributes, redactValue } from "../trace/trace-config/maskingRules";
import { FITracer } from "../trace/trace-config/FITracer";
import { TraceConfigOptions } from "../trace/trace-config/types";

// F2: the default image cap is 0.
// Cap-0 trigger: in one Q1 scope view, a case-insensitive "data:", then "image/",
// then ",". The protected split-token pair stays byte-identical.
// TS-F2-R6 (PENDING PTC-TS-F2-R6-A1): each view is searched with the ASCII members
// of Python str.isspace() removed.
// No expected output is obtained from a scanner, regex, or the function under test.
const BS = String.fromCharCode(92);
const P = "QUJD".repeat(40);
const H = "data:image/png;base64,";
const ONE = "data:image/png;base64,A";
const FLAGS = [
  "FI_BASE64_IMAGE_MAX_LENGTH", "FI_PII_REDACTION", "FI_HIDE_INPUTS", "FI_HIDE_OUTPUTS",
  "FI_HIDE_INPUT_MESSAGES", "FI_HIDE_OUTPUT_MESSAGES", "FI_HIDE_INPUT_TEXT", "FI_HIDE_OUTPUT_TEXT",
  "FI_HIDE_INPUT_IMAGES", "FI_HIDE_EMBEDDING_VECTORS", "FI_HIDE_ERROR_DETAILS",
];
const saved = new Map(FLAGS.map(key => [key, process.env[key]]));
beforeEach(() => { for (const key of FLAGS) delete process.env[key]; });
afterEach(() => {
  for (const key of FLAGS) {
    const value = saved.get(key);
    if (value === undefined) delete process.env[key]; else process.env[key] = value;
  }
  jest.restoreAllMocks();
});
const options = (base64ImageMaxLength: number): TraceConfigOptions => ({
  base64ImageMaxLength, piiRedaction: false, hideErrorDetails: false,
  hideInputs: false, hideOutputs: false, hideInputMessages: false, hideOutputMessages: false,
  hideInputText: false, hideOutputText: false, hideInputImages: false, hideEmbeddingVectors: false,
});
const zero = { ...DefaultTraceConfig, ...options(0) } as typeof DefaultTraceConfig;
const hexText = (text: string): string => text.split("").map(c => BS + "u" + c.charCodeAt(0).toString(16).padStart(4, "0")).join("");

// Each entry has a distinct RED-first source oracle, including the counted seam.
describe("F2 census reads preserve zero", () => {
  it("constants / metadata / public default", () => {
    expect(DEFAULT_BASE64_IMAGE_MAX_LENGTH).toBe(0);
    expect(DefaultTraceConfig.base64ImageMaxLength).toBe(0);
    expect(generateTraceConfig().base64ImageMaxLength).toBe(0);
    expect(capBase64Images(ONE, generateTraceConfig().base64ImageMaxLength)).toBe("[image-redacted]");
  });
  it("direct imageCap read", () => expect(capBase64Images(ONE, 0)).toBe("[image-redacted]"));
  it("counted imageCap read", () => {
    expect(__ImageCapEditsForTest.counted(ONE, 0)).toEqual({ value: "[image-redacted]", count: 1 });
  });
  it("redactValue read", () => expect(redactValue(zero, ONE)).toBe("[image-redacted]"));
  it("mask route", () => expect(mask({ config: zero, key: "custom.image", value: ONE })).toBe("[image-redacted]"));
  it("maskAttributes route", () => {
    const input = Object.freeze({ image: ONE, ordinary: "keep" });
    expect(maskAttributes({ config: zero, attributes: input })).toEqual({ image: "[image-redacted]", ordinary: "keep" });
    expect(input.image).toBe("data:image/png;base64,A");
  });
  it("environment string zero does not fall back to a positive default", () => {
    process.env.FI_BASE64_IMAGE_MAX_LENGTH = "0";
    const config = generateTraceConfig();
    expect(config.base64ImageMaxLength).toBe(0);
    expect(redactValue(config, ONE)).toBe("[image-redacted]");
  });
  it("explicit zero wins over a positive environment cap", () => {
    process.env.FI_BASE64_IMAGE_MAX_LENGTH = "32000";
    const config = generateTraceConfig(options(0));
    expect(config.base64ImageMaxLength).toBe(0);
    expect(redactValue(config, ONE)).toBe("[image-redacted]");
  });
  it("one-character declared payload and empty payload boundary", () => {
    expect(capBase64Images('{"type":"image","data":"A","keep":"ordinary"}', 0))
      .toBe('{"type":"image","data":"[image-redacted]","keep":"ordinary"}');
    expect(capBase64Images('{"type":"image","data":"","keep":"ordinary"}', 0))
      .toBe('{"type":"image","data":"","keep":"ordinary"}');
  });
  it("arrays preserve benign identity and never mutate the caller", () => {
    const input = [ONE, "ordinary", "https://example.invalid/a.png"];
    expect(capBase64Images(input, 0)).toEqual(["[image-redacted]", "ordinary", "https://example.invalid/a.png"]);
    expect(input[0]).toBe("data:image/png;base64,A");
    const benign = ["ordinary", "data:audio/wav;base64,QUJD"];
    expect(capBase64Images(benign, 0)).toBe(benign);
    expect(capBase64Images(17, 0)).toBe(17);
    expect(capBase64Images(false, 0)).toBe(false);
  });
});

const INVALID: Array<[string, unknown]> = [
  ["negative", -1], ["NaN", Number.NaN], ["fraction", 0.5], ["infinite", Infinity],
  ["unparsable", "abc"], ["unsafe integer", Number.MAX_SAFE_INTEGER + 1],
  ["null", null], ["boolean", true], ["empty string", ""], ["object", {}],
];
describe("F2 invalid-cap fail-closed parity contract", () => {
  it.each(INVALID)("direct invalid %s is zero with a value-free warning", (_name, value) => {
    const warn = jest.spyOn(diag, "warn").mockImplementation(() => undefined);
    expect(capBase64Images(ONE, value as number)).toBe("[image-redacted]");
    expect(warn.mock.calls).toEqual([["Invalid base64 image cap; using 0."]]);
  });
  it.each([0, "0"])("direct parity zero %s redacts", value => {
    expect(capBase64Images(ONE, value as number)).toBe("[image-redacted]");
  });
  // r2 binds and repairs the initializer. These remain source-only acceptance
  // controls, not an executed RED/GREEN or a measured Python parity result.
  it.each(INVALID)("initializer invalid %s must neither throw nor recover a positive cap", (_name, value) => {
    process.env.FI_BASE64_IMAGE_MAX_LENGTH = "32000";
    const warn = jest.spyOn(diag, "warn").mockImplementation(() => undefined);
    const config = generateTraceConfig(options(value as number));
    expect(config.base64ImageMaxLength).toBe(0);
    expect(redactValue(config, ONE)).toBe("[image-redacted]");
    expect(warn.mock.calls).toEqual([["Invalid base64 image cap; using 0."]]);
  });
  it.each(["-1", "NaN", "0.5", "abc", "1junk", "Infinity", "", "   ", "1e3", "0x10", "12.0", "9007199254740992"])(
    "invalid environment %s must fail closed at initialization", value => {
      const warn = jest.spyOn(diag, "warn").mockImplementation(() => undefined);
      process.env.FI_BASE64_IMAGE_MAX_LENGTH = value;
      const config = generateTraceConfig();
      expect(config.base64ImageMaxLength).toBe(0);
      expect(redactValue(config, ONE)).toBe("[image-redacted]");
      expect(warn.mock.calls).toEqual([["Invalid base64 image cap; using 0."]]);
    });
  it("runtime initializer string zero is zero with a value-free type warning", () => {
    process.env.FI_BASE64_IMAGE_MAX_LENGTH = "32000";
    const warn = jest.spyOn(diag, "warn").mockImplementation(() => undefined);
    const config = generateTraceConfig(options("0" as unknown as number));
    expect(config.base64ImageMaxLength).toBe(0);
    expect(redactValue(config, ONE)).toBe("[image-redacted]");
    expect(warn.mock.calls).toEqual([["Invalid base64 image cap; using 0."]]);
  });
  it.each(["0", " 0 ", "000", "\t0\n"])("valid environment zero %s is not a warning or fallback", value => {
    process.env.FI_BASE64_IMAGE_MAX_LENGTH = value;
    const warn = jest.spyOn(diag, "warn").mockImplementation(() => undefined);
    const config = generateTraceConfig();
    expect(config.base64ImageMaxLength).toBe(0);
    expect(redactValue(config, ONE)).toBe("[image-redacted]");
    expect(warn).not.toHaveBeenCalled();
  });
  it("default and explicit numeric zero do not warn, even over an invalid environment", () => {
    const warn = jest.spyOn(diag, "warn").mockImplementation(() => undefined);
    expect(generateTraceConfig().base64ImageMaxLength).toBe(0);
    process.env.FI_BASE64_IMAGE_MAX_LENGTH = "abc";
    expect(generateTraceConfig(options(0)).base64ImageMaxLength).toBe(0);
    expect(warn).not.toHaveBeenCalled();
  });
  it.each([1, 64, 32000, Number.MAX_SAFE_INTEGER])("valid explicit positive cap %s wins without warning", value => {
    process.env.FI_BASE64_IMAGE_MAX_LENGTH = "abc";
    const warn = jest.spyOn(diag, "warn").mockImplementation(() => undefined);
    expect(generateTraceConfig(options(value)).base64ImageMaxLength).toBe(value);
    expect(warn).not.toHaveBeenCalled();
  });
  it.each<[string, number]>([["1", 1], [" 064 ", 64], ["32000", 32000], ["9007199254740991", Number.MAX_SAFE_INTEGER]])(
    "valid positive environment %s retains its exact cap", (value, expected) => {
      process.env.FI_BASE64_IMAGE_MAX_LENGTH = value;
      const warn = jest.spyOn(diag, "warn").mockImplementation(() => undefined);
      expect(generateTraceConfig().base64ImageMaxLength).toBe(expected);
      expect(warn).not.toHaveBeenCalled();
    });
  it("invalid caller objects are not coerced or interpolated while warning", () => {
    const value = { toString: () => { throw new Error("must not coerce caller cap"); } };
    const warn = jest.spyOn(diag, "warn").mockImplementation(() => undefined);
    const config = generateTraceConfig(options(value as unknown as number));
    expect(config.base64ImageMaxLength).toBe(0);
    expect(warn.mock.calls).toEqual([["Invalid base64 image cap; using 0."]]);
  });
});

const transportedHeader = BS + 'u0064ata:image/png;' + BS + 'u0062ase64;name="a' + BS + '" b",';
const longHead = 'data:image/png;name="a' + BS + '" ' + "q".repeat(1200) + '"';
const longFlag = longHead + ';ba' + BS + 'u0073e64,';
const hidden = BS + "u0064ata:image/";
const later = hidden + 'png;name="a' + BS + '" b";base64,' + P;
const WITNESSES: Array<[string, string]> = [
  ["r1 plain one-character image", ONE],
  ["r1 captions", "left " + H + P + " right"],
  ["r1 case and colon whitespace", "DaTa: \n IMAGE/png, ignored;BaSe64," + P],
  ["r1 quote-independent header", 'data:image/png stray"quote;base64,' + P],
  ["r2 JSON prefix", BS + "u0064ata:image/png;base64," + P],
  ["r2 slash escape", "data:image" + BS + "/png;base64," + P],
  ["r2 all JSON escapes", hexText(H + P)],
  ["r2 percent", "%64ata%3Aimage%2Fpng%3Bbase64%2C" + P],
  ["r2 numeric and named references", "&#100;ata&colon;image&sol;png&semi;base64&comma;" + P],
  ["r2 padded numeric", "&#x00000064;ata:image/png;base64," + P],
  ["r3 transported nonfinal flag", transportedHeader + P],
  ["r3 transported long final flag", hidden + 'png;name="a' + BS + '" b' + "q".repeat(1100) + '";' + BS + "u0062ase64," + P],
  ["r3 nested transported payload", hidden + 'png;base64;name="a' + BS + '" b",QUJD' + (BS + "u005cu0041").repeat(160)],
  ["r3 duplicate C1/C2 image", H + "A".repeat(80)],
  ["r4 MIME quoted pair", 'data:image/png;name="a' + BS + '" b";base64,' + P],
  ["r4 long raw MIME", 'data:image/png;name="' + "q".repeat(4096) + '";base64,' + P],
  ["r4 non-final flag", "data:image/png;BASE64;name=ordinary," + P],
  ["r4 no 1024 header window", "data:image/" + "x ".repeat(503) + ";base64," + P],
  ["r5 hidden prefix MIME pair", later],
  ["r5 rejected prefix", hidden + "x ".repeat(120) + later],
  ["r6 A001 earlier open quote", hidden + 'png stray;name=",' + later],
  ["r6 overlapping prefixes", "data:image/png stray;base64,data:image/png stray;base64," + P],
  ["r6 stray-whitespace decoy", 'data:image/svg+xml stray;name="a' + BS + '";base64,' + P + '",%3Csvg%3E'],
  ["r6 semicolon-space decoy", 'data:image/svg+xml; name="a' + BS + '";base64,' + P + '",%3Csvg%3E'],
  ["r7 A001 short apparent run", 'data:image/png;name=";base64,QUJD";base64,' + P],
  ["r7 A001 long apparent run", 'data:image/png;name=";base64,' + P + '";base64,' + P],
  ["r7 B001 MIME pair apparent run", 'data:image/png;name="a' + BS + '";base64,AAAA";base64,' + P],
  ["r7 B003 transported quote decoy", 'data:image/svg+xml;name="' + BS + '%22;base64,' + P + '",%3Csvg%3E'],
  ["BK-A-TS-002 fjlig", transportedHeader + "&fjlig;".repeat(80)],
  ["BK-A-TS-002 padded numeric", transportedHeader + ("&#" + "0".repeat(8) + "65;").repeat(80)],
  ["BK-A-TS-003 quoted comma", hidden + 'png;' + BS + 'u0062ase64;name="a' + BS + '" b,c",' + P],
  ["BK-A-TS-003 long flag after comma", hidden + 'png;name="a' + BS + '" b,' + "q".repeat(1100) + '";' + BS + "u0062ase64," + P],
  ["BK-A-TS-004 transported extent", transportedHeader + (BS + "u0041").repeat(40)],
  ["BK-B-001 numeric", longFlag + "&#x00000051;UJD".repeat(40)],
  ["BK-B-001 named newline", longFlag + "&NewLine;" + P],
  ["BK-B-002 supplementary surrogate", longFlag + BS + "udb40" + BS + "udc00" + P],
  ["BK-B-003 decoded equality is irrelevant at zero", longFlag + P],
  ["BK-B-004 whole subsumes earlier image", H + P + "!" + longFlag + P],
  ["F2 case and whitespace after each literal fragment", "DATA: \t \n IMAGE/ \f png, A"],
  ["F2 no MIME subtype required", "data:image/!!!???,A"],
  ["F2 non-base64 SVG over-redaction", "data:image/svg+xml,%3Csvg%3E"],
  ["F2 false flag over-redaction", "data:image/png;name=base64,QUJD"],
  ["F2 empty payload mention", "data:image/png;base64,"],
  ["F2 non-whitespace gap within one scope", "data: arbitrary image/png;base64,A"],
];

describe("F2 positive historical witnesses have literal whole-scope outputs", () => {
  it.each(WITNESSES)("%s", (_name, value) => {
    expect(capBase64Images(value, 0)).toBe("[image-redacted]");
    expect(capBase64Images("caption " + value + " tail", 0)).toBe("[image-redacted]");
    expect(capBase64Images(JSON.stringify({ value, keep: "ordinary" }), 0))
      .toBe('{"value":"[image-redacted]","keep":"ordinary"}');
    expect(capBase64Images(JSON.stringify({ [value]: "ordinary", keep: 1 }), 0))
      .toBe('{"[image-redacted]":"ordinary","keep":1}');
    expect(capBase64Images(JSON.stringify({ opaque: JSON.stringify({ value, keep: "ordinary" }), other: "retain" }), 0))
      .toBe('{"opaque":"{\\"value\\":\\"[image-redacted]\\",\\"keep\\":\\"ordinary\\"}","other":"retain"}');
  });
  it.each([0x17b4, 0x17b5, 0x115f, 0x3164, 0xffa0, 0x1d173, 0xe0000, 0xe0100, 0x200b, 0xfeff])(
    "BK-A-TS-001 / invisible U+%i in prefix, separators and continuation", code => {
      const z = String.fromCodePoint(code);
      for (const value of ["d" + z + "ata:image/png;base64," + P,
        "data" + z + ":image" + z + "/png;base64" + z + ",A",
        'data:image/png;base64;name="a' + BS + '" b",' + P + z + P]) {
        expect(capBase64Images(value, 0)).toBe("[image-redacted]");
      }
    });
  it.each(["%E2%80%8B", "%C2%AD", "%EF%BB%BF", "&#8203;", BS + "udb40" + BS + "udc00"])(
    "transported invisible %s cannot conceal a prefix", z => {
      expect(capBase64Images("da" + z + "ta:image/png;base64,A", 0)).toBe("[image-redacted]");
    });
  it("BK-B-005 deep structural arrays retain independent token scopes", () => {
    const raw = "[".repeat(2000) + JSON.stringify({ keep: "ordinary words", x: longFlag + P }) + "]".repeat(2000);
    const expected = "[".repeat(2000) + '{"keep":"ordinary words","x":"[image-redacted]"}' + "]".repeat(2000);
    expect(capBase64Images(raw, 0)).toBe(expected);
  });
  it("BK-B-004 emitted count is final whole scope count", () => {
    expect(__ImageCapEditsForTest.counted(H + P + "!" + longFlag + P, 0))
      .toEqual({ value: "[image-redacted]", count: 1 });
  });
});

describe("IMAGECAP-WS-001 and IMAGECAP-TD-001 at zero", () => {
  it.each([" ", "\t", "\f", "\n", "\r\n", " \n", "%20", "&#9;", BS + "t"])("WS separator %s", separator => {
    const value = H + Array(40).fill("QUJD").join(separator);
    expect(capBase64Images(value, 0)).toBe("[image-redacted]");
    expect(capBase64Images(JSON.stringify({ value, keep: "ordinary" }), 0))
      .toBe('{"value":"[image-redacted]","keep":"ordinary"}');
  });
  it.each([
    ["percent comma", ";base64%2C"], ["JSON comma", ";base64" + BS + "u002c"],
    ["named comma", ";base64&comma;"], ["numeric comma", ";base64&#44;"],
    ["percent semicolon", "%3Bbase64,"], ["JSON semicolon", BS + "u003bbase64,"],
    ["named semicolon", "&semi;base64,"], ["numeric semicolon", "&#59;base64,"],
    ["percent both", "%3Bbase64%2C"], ["JSON both", BS + "u003bbase64" + BS + "u002c"],
    ["named both", "&semi;base64&comma;"],
  ])("TD %s with long header, raw/value/key", (_name, ending) => {
    const value = "data:image/png;name=" + "q".repeat(1024) + ending + P;
    expect(capBase64Images(value, 0)).toBe("[image-redacted]");
    expect(capBase64Images(JSON.stringify({ value, keep: "ordinary" }), 0))
      .toBe('{"value":"[image-redacted]","keep":"ordinary"}');
    expect(capBase64Images(JSON.stringify({ [value]: "ordinary", keep: 1 }), 0))
      .toBe('{"[image-redacted]":"ordinary","keep":1}');
  });
});

describe("F2 Q1 scopes, fixed points and preservation", () => {
  it("does not join the alphabet from a different token", () => {
    expect(capBase64Images('{"data:image/":"A","keep":1}', 0)).toBe('{"data:image/":"A","keep":1}');
    expect(capBase64Images('["data:image/","A"]', 0)).toBe('["data:image/","A"]');
    expect(capBase64Images('["data:image/",",A"]', 0)).toBe('["data:image/",",A"]');
    expect(capBase64Images('data:image/","A', 0)).toBe("[image-redacted]");
    expect(capBase64Images('data:image/" "A', 0)).toBe('data:image/" "A');
  });
  it("r2 trigger needs data:, then image/, then a comma in one scope", () => {
    for (const value of ["data:image/png;base64,", "data:image/,", "data:image/!!!,", "x data: y image/ z , w"]) {
      expect(capBase64Images(value, 0)).toBe("[image-redacted]");
    }
    expect(__ImageCapEditsForTest.counted("data:image/png;base64,", 0)).toEqual({ value: "[image-redacted]", count: 1 });
    for (const value of [
      "data:image/", "data:image/!!!", "data:image/png;name=", "data:image/png;base64",
      ",data:image/png;base64", "image/png, data:", "data:, image/png",
    ]) {
      expect(capBase64Images(value, 0)).toBe(value);
      expect(__ImageCapEditsForTest.counted(value, 0)).toEqual({ value, count: 0 });
    }
    // PTC-TS-F2-R6-A1 PENDING (Z-OVER, CAP0-OVERREDACTION-r2 parity): head pinned this
    // member unchanged. Python removes whitespace before the same search.
    const spaced = "DATA \t : IMAGE /png, A";
    expect(capBase64Images(spaced, 0)).toBe("[image-redacted]");
    expect(__ImageCapEditsForTest.counted(spaced, 0)).toEqual({ value: "[image-redacted]", count: 1 });
  });
  it("data and image fragments in different Q1 scopes never combine", () => {
    const object = '{"data:":"image/png;base64,A"}';
    const array = '["data:","image/png;base64,A"]';
    const sibling = '{"data:image/png":"x,y"}';
    const three = '["data:","image/",","]';
    expect(capBase64Images(object, 0)).toBe(object);
    expect(capBase64Images(array, 0)).toBe(array);
    expect(capBase64Images(sibling, 0)).toBe(sibling);
    expect(capBase64Images(three, 0)).toBe(three);
  });
  it("r6 split-token witnesses preserve independent prefix tokens at zero", () => {
    for (const token of ["data:image/png;name=", ";base64,QUJD"]) {
      expect(capBase64Images(token, 0)).toBe(token);
    }
    expect(capBase64Images('["data:image/png;name=",";base64,QUJD"]', 0)).toBe('["data:image/png;name=",";base64,QUJD"]');
    const object = '{"data:image/png;name=":";base64,QUJD"}';
    const array = '["data:image/png;name=", ";base64,QUJD"]';
    const transported = BS + 'u007b"data:image/png;name=":";base64,QUJD"' + BS + "u007d";
    const inner = BS + 'u0020{"data:image/png;name=":";base64,QUJD"}';
    const outer = JSON.stringify({ opaque: inner, other: "retain" });
    expect(capBase64Images(object, 0)).toBe(object);
    expect(capBase64Images(array, 0)).toBe(array);
    expect(capBase64Images(transported, 0)).toBe(transported);
    expect(capBase64Images(outer, 0)).toBe(outer);
  });
  it("duplicate keys, numeric spellings and benign escapes are not reserialized", () => {
    const value = '{ "same":1,"same":2,"id":9007199254740993,"n":1e400,"url":"' + ONE + '","s":"' + BS + 'uD800" }';
    expect(capBase64Images(value, 0))
      .toBe('{ "same":1,"same":2,"id":9007199254740993,"n":1e400,"url":"[image-redacted]","s":"' + BS + 'uD800" }');
  });
  it.each(["%25252578ordinary", BS + "u005cu005cu005cu0078ordinary", "%252522%25252541%252522"])(
    "still changing after three passes selects whole scope %s", value => {
      expect(capBase64Images(value, 0)).toBe("[image-redacted]");
    });
  it("r7 B002 malformed scope does not hide exhaustion behind apparent token fences", () => {
    const expensive = BS + "u005c".repeat(256) + "u0078ordinary " + "z".repeat(8192);
    const raw = '{"expensive":' + JSON.stringify(expensive) + ',"exact":"' + H + "A".repeat(42) + '","note":"keep",}';
    expect(capBase64Images(raw, 0)).toBe("[image-redacted]");
  });
  it.each([
    "ordinary words", "",
    "data:application/pdf;base64,QUJD", "data:audio/wav;base64,QUJD", "https://example.invalid/a.png",
    "ordinary %78 &amp; " + BS + "u0078", "%252578ordinary", "ordinary " + BS + "q",
    "emoji " + String.fromCodePoint(0x1f468, 0x200d, 0x1f469) + " family",
    '{ "n":1e400,"same":1,"same":2,"s":"' + BS + 'uD800" }',
    "[image-redacted] __REDACTED__ ordinary marker text",
  ])("benign original is byte-identical: %s", value => {
    expect(capBase64Images(value, 0)).toBe(value);
  });
  it("r6 B002 / r7 A002 legal non-image escaped declarations remain benign", () => {
    const inner = '{"type":"image","data":"' + P + BS + "t" + P + '","note":"keep",}';
    const legal = '{"type":"image","data":"' + P + BS + "t" + P + BS + '"tail","note":"keep"} null';
    expect(capBase64Images(JSON.stringify({ opaque: inner, other: "retain" }), 0))
      .toBe(JSON.stringify({ opaque: inner, other: "retain" }));
    expect(capBase64Images(legal, 0)).toBe(legal);
  });
  it("bare signatures and malformed declared image scopes remain protected", () => {
    expect(capBase64Images("iVBORw0KGgo=", 0)).toBe("[image-redacted]");
    expect(capBase64Images('{"type":"image" "data":"A"}', 0)).toBe("[image-redacted]");
  });
});

// These are adapter payload-shape controls through the common masking seam,
// not claims to execute unbound instrumentor implementations.
const SHAPES: Array<[string, string, string]> = [
  ["OpenAI chat", '[{"role":"user","content":[{"type":"image_url","image_url":{"url":"data:image/png;base64,A"}}]}]', '[{"role":"user","content":[{"type":"image_url","image_url":{"url":"[image-redacted]"}}]}]'],
  ["OpenAI Responses / computer screenshot", '[{"type":"input_image","image_url":"data:image/png;base64,A"}]', '[{"type":"input_image","image_url":"[image-redacted]"}]'],
  ["Anthropic / Bedrock invoke-model", '{"type":"image","source":{"type":"base64","media_type":"image/png","data":"A"}}', '{"type":"image","source":{"type":"base64","media_type":"image/png","data":"[image-redacted]"}}'],
  ["Bedrock converse flattened image", 'data:image/png;base64,A', '[image-redacted]'],
  ["Bedrock caption image", '[{"role":"user","content":"describe [image: data:image/png;base64,A] please"}]', '[{"role":"user","content":"[image-redacted]"}]'],
  ["Google GenAI inlineData", '{"contents":[{"parts":[{"inlineData":{"mimeType":"image/png","data":"A"}}]}]}', '{"contents":[{"parts":[{"inlineData":{"mimeType":"image/png","data":"[image-redacted]"}}]}]}'],
  ["OTel GenAI blob", '{"type":"blob","modality":"image","mime_type":"image/png","content":"A"}', '{"type":"blob","modality":"image","mime_type":"image/png","content":"[image-redacted]"}'],
];
it.each(SHAPES)("F2 one-character integration shape %s", (_name, input, expected) => {
  expect(maskAttributes({ config: zero, attributes: { "input.value": input } }))
    .toEqual({ "input.value": expected });
});

const SURFACES = ["creation", "setAttribute", "setAttributes", "event", "exception", "status", "link", "links"] as const;
it.each(SURFACES)("F2 real FITracer retained-value route %s", surface => {
  const exporter = new InMemorySpanExporter();
  const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
  const tracer = new FITracer({ tracer: provider.getTracer("f2-zero"), traceConfig: options(0) });
  const span = tracer.startSpan("case", { attributes: surface === "creation" ? { image: ONE } : {} });
  const link = { context: { traceId: "0123456789abcdef0123456789abcdef", spanId: "0123456789abcdef", traceFlags: 1 }, attributes: { image: ONE } };
  if (surface === "setAttribute") span.setAttribute("image", ONE);
  if (surface === "setAttributes") span.setAttributes({ image: ONE });
  if (surface === "event") span.addEvent("request", { image: ONE });
  if (surface === "exception") { const error = new Error(ONE); error.stack = "Error: " + ONE; span.recordException(error); }
  if (surface === "status") span.setStatus({ code: SpanStatusCode.ERROR, message: ONE });
  if (surface === "link") span.addLink(link);
  if (surface === "links") span.addLinks([link]);
  span.end();
  const results = exporter.getFinishedSpans();
  expect(results).toHaveLength(1);
  const result = results[0];
  if (surface === "creation" || surface === "setAttribute" || surface === "setAttributes") expect(result.attributes.image).toBe("[image-redacted]");
  if (surface === "event") expect(result.events[0].attributes?.image).toBe("[image-redacted]");
  if (surface === "exception") {
    expect(result.events[0].attributes?.["exception.message"]).toBe("[image-redacted]");
    expect(result.events[0].attributes?.["exception.stacktrace"]).toBe("[image-redacted]");
  }
  if (surface === "status") expect(result.status).toEqual({ code: SpanStatusCode.ERROR, message: "[image-redacted]" });
  if (surface === "link" || surface === "links") expect(result.links[0].attributes?.image).toBe("[image-redacted]");
  expect(result.attributes["traceai.capture.dropped_content_count"]).toBeUndefined();
});

it("explicit positive caps retain r4c6 behavior, including repaired WS/TD classes", () => {
  expect(capBase64Images(ONE, ONE.length)).toBe("data:image/png;base64,A");
  expect(capBase64Images("left " + ONE + " right", ONE.length - 1)).toBe("left __REDACTED__ right");
  expect(capBase64Images("data:image/svg+xml,%3Csvg%3E", 64)).toBe("data:image/svg+xml,%3Csvg%3E");
  expect(capBase64Images("data:image/png;base64,", 64)).toBe("data:image/png;base64,");
  const ws = H + Array(40).fill("QUJD").join(" ");
  const td = "data:image/png;name=" + "q".repeat(1024) + ";base64%2C" + P;
  expect(capBase64Images(ws, 64)).toBe("[image-redacted]");
  expect(capBase64Images(td, 64)).toBe("[image-redacted]");
});

// TS-F2-R6 A-WHITESPACE-PARITY (lens F2-TS-CAP0-COMPLETENESS finding 1). Runtime NOT_RUN.
// Python parity source: reference _image_cap.py _zero_scope_mentions_image removes
// whitespace before the data:, image/, comma search. TS removes the ASCII members of
// Python str.isspace() on each view of one Q1 scope. Expected outputs are literals.
const PNG = [
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ",
  "AAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==",
].join("");
const SPLIT: Array<[string, string]> = [
  ["TAB inside data:", "DATA\t:IMAGE/png;base64," + PNG],
  ["LF inside data:", "da\nta:image/png;base64," + PNG],
  ["CR LF inside image/", "data:ima\r\nge/png;base64," + PNG],
  ["TAB inside image/", "data:image\t/png;base64," + PNG],
  ["SPACE inside data: and image/", "da ta:ima ge/png;base64,A"],
  ["TAB before the colon, SPACE before the slash", "data\t:image /png;base64,A"],
  ["VT, FF and U+001C-U+001F", "d\x1ca\x1dt\x1ea\x1f:ima\vge\f/png,A"],
  ["percent TAB, a real TAB in transport view 1 only", "da%09ta:image/png;base64," + PNG],
  ["head :293 member", "DATA \t : IMAGE /png, A"],
];

describe("TS-F2-R6 ASCII whitespace inside data: or image/ cannot shelter a mention at zero", () => {
  it.each(SPLIT)("%s", (_name, value) => {
    expect(capBase64Images(value, 0)).toBe("[image-redacted]");
    expect(__ImageCapEditsForTest.counted(value, 0)).toEqual({ value: "[image-redacted]", count: 1 });
    expect(capBase64Images("caption " + value + " tail", 0)).toBe("[image-redacted]");
    expect(capBase64Images(JSON.stringify({ value, keep: "ordinary" }), 0))
      .toBe('{"value":"[image-redacted]","keep":"ordinary"}');
    expect(capBase64Images(JSON.stringify({ [value]: "ordinary", keep: 1 }), 0))
      .toBe('{"[image-redacted]":"ordinary","keep":1}');
    expect(capBase64Images(JSON.stringify({ opaque: JSON.stringify({ value, keep: "ordinary" }), other: "retain" }), 0))
      .toBe('{"opaque":"{\\"value\\":\\"[image-redacted]\\",\\"keep\\":\\"ordinary\\"}","other":"retain"}');
  });
  it("lens token form with a JSON TAB escape redacts only that token", () => {
    const value = '{"url":"da' + BS + 'tta:image/png;base64,' + PNG + '"}';
    expect(capBase64Images(value, 0)).toBe('{"url":"[image-redacted]"}');
  });
  it("whitespace-split fragments in different Q1 tokens never combine", () => {
    for (const value of [
      JSON.stringify(["da\tta:image/", ",A"]),
      JSON.stringify(["DA\tTA:IMA\nGE/", ",A"]),
      JSON.stringify({ "da ta:": "ima ge/png;base64,A" }),
      JSON.stringify(["da ta:", "ima ge/", ","]),
    ]) {
      expect(capBase64Images(value, 0)).toBe(value);
    }
  });
  it.each([
    "DA\tTA:AUDIO/wav;base64,QUJD",
    "da ta: audio / wav, ordinary",
    "image / png , da ta :",
    "da\nta:image/png;name=",
  ])("benign or comma-free whitespace-split value is byte-identical: %j", value => {
    expect(capBase64Images(value, 0)).toBe(value);
    expect(__ImageCapEditsForTest.counted(value, 0)).toEqual({ value, count: 0 });
    const wrapped = JSON.stringify({ value, keep: "ordinary" });
    expect(capBase64Images(wrapped, 0)).toBe(wrapped);
  });
  it("explicit positive caps keep head output for whitespace-split spellings", () => {
    for (const value of ["DATA\t:IMAGE/png;base64," + PNG, "da ta:ima ge/png;base64," + P]) {
      expect(capBase64Images(value, 64)).toBe(value);
    }
  });
});
