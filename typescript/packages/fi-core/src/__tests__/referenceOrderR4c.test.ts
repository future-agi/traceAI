import { describe, expect, test } from "@jest/globals";
import { indexPrivacyHints, scrubValue } from "../privacy-runtime";

// A known secret that takes a JSON owner out whole leaves the text beside it to the pair rule: the characters the replacement forms with
// the text around it are not a known secret of the text the reference reads, so the pair after the owner is still read.
const none = indexPrivacyHints([]);
const run = (value: unknown, secrets: string[], framed: boolean) => scrubValue(value, secrets, "message", 0, false, undefined, none, framed);
const nest = (depth: number, leaf: unknown) => { let value = leaf; for (let i = 0; i < depth; i++) value = { k: value }; return value; };

describe("a known secret that holds a quote and starts a key, before a pair after the owner", () => {
  for (const framed of [true, false]) {
    const site = framed ? "framed" : "prose";
    test(`the value of the pair is redacted (${site})`, () => {
      const out = run("{\"api_key\":\"\"}api_key=TAILV005F5A", ["\"api_key"], framed);
      expect(out).not.toContain("TAILV005F5A");
      // d2dab4e8 (version 6, round 3): a text that begins as a JSON object, is not one JSON value and holds a credential name as a quoted
      // member name is the placeholder as a whole.
      expect(out).toBe("<redacted>");
    });
  }
});

// A JSON string literal is read as raw text, as the reference reads it: its quotes and backslashes are characters of the text, and the pair rule
// finds a name and a value in it as it finds them in any other text.
describe("a JSON string literal read as raw text", () => {
  test("redacts the value after a quoted name and an equals sign", () => {
    const out = run("\"\\\"session\\\\\\\":\\\"TAILV0D3D35\"", [], true);
    expect(out).not.toContain("TAIL");
    expect(out).toBe("\"\\\"session\\\\\\\":\\\"<redacted>\\\"");
  });
  test("redacts the value after a quoted name that ends in a backslash and a quote", () => {
    const out = run("\"\\\"key\\\\\\\"=TAILV0016A3\"", [], true);
    expect(out).not.toContain("TAIL");
    expect(out).toBe("\"\\\"key\\\\\\\"=<redacted>");
  });
  test("redacts the value after a control sequence between a name and its value", () => {
    const out = run("\"SetCookie%3d\\n\\\"TAILV1ECEBC\"", [], true);
    expect(out).not.toContain("TAIL");
    expect(out).toBe("\"SetCookie%3d<redacted>");
  });
  test("redacts the value after a JSON object text", () => {
    const out = run("\"{\\\"AUTH\\\":[true],\\\"l\\\":\\\"\\\\\\\"TAILV01EF00\\\\\\\"\\\",\\\"\\\":\\\"\\\"}\"", [], true);
    expect(out).not.toContain("TAIL");
    expect(out).toBe("\"{\\\"AUTH\\\":\\\"<redacted>\\\"");
  });
});

describe("a JSON string literal next to the depth limit", () => {
  const literal = "\"\\\"session\\\\\\\":\\\"TAILV0D3D35\"";
  test("is read as the reference reads it one level before the limit", () => {
    expect(run(nest(31, literal), [], true)).toEqual(nest(31, "\"\\\"session\\\\\\\":\\\"<redacted>\\\""));
  });
  test("is read as the reference reads it at the limit", () => {
    expect(run(nest(32, literal), [], true)).toBe("<redacted>");
  });
  test("one without a backslash is returned as it is before the limit", () => {
    expect(run(nest(31, "\"abc\""), [], true)).toEqual(nest(31, "\"abc\""));
  });
});

// A secret that ends inside a JSON-like span, crosses a quote or a bracket, or starts or ends inside a backslash sequence takes the span it
// touches out of the text, and the pair rule still reads the quoted name that follows: the value after it is redacted, as the reference
// redacts it with the secret replaced first. Where the scrubber takes out more than the reference, the expected text is the scrubber's own
// and the reference's text is listed beside it.
describe("a known secret that touches a JSON-like span, before a quoted pair name", () => {
  // [title, text, secrets, expected, the reference text where it differs from the expected text (null: the expected text is the reference text)]
  const cases: [string, string, string[], string, string | null][] = [
    ["ends inside a quoted key", "\"869TAILV\":\"token\":TAILVF", ["869TAILV"],
      "<redacted>:<redacted>", "\"<redacted>\":\"token\":\"<redacted>\""],
    // d2dab4e8 (version 6, round 3): a text that begins as a JSON object, is not one JSON value and holds a credential name as a quoted
    // member name is the placeholder as a whole.
    ["crosses a quote and a brace", "{\"\":\"\"}\\\":\\\"TAsession\":TAILVF", ["}\\\":\\\"TA"],
      "<redacted>", null],
    ["ends at a closing bracket", "{\"secret\":[true]}\":TAILV5", ["\":[true]"],
      "<redacted>", null],
    ["starts inside a quoted value", "\"B\":\"TAILpass\":TAILVB", ["B\":\"TAIL"],
      "<redacted>:<redacted>", "\"<redacted>pass\":\"<redacted>\""],
    ["starts a string that holds a backslash and a quote and a name", "[\"TAILVE\\\"pwd\"}\\\":TAILVB\"", ["[\"TAILVE"],
      "<redacted>", "<redacted>\\\"pwd\"}\\\":\\\"<redacted>\\\""],
    ["starts an empty string and is followed by an unclosed object", "\"\"TAILV40{pass\":TAILV0", ["\"TAILV40"],
      "<redacted>{pass\":<redacted>", "\"<redacted>{pass\":\"<redacted>\""],
    ["takes a quoted key out before an unclosed object", "\"AAAAAAAA\":\"\"{token\":\"TAILV111111", ["AAAAAAAA"],
      "<redacted>{token\":<redacted>", "\"<redacted>\":\"\"{token\":\"<redacted>\""],
    ["takes a quoted key out before a longer run of keys", "\"AAAAAAAA\":\"\"\"auth\":\"\"{token\":\"TAILV111111", ["AAAAAAAA"],
      "<redacted>", "\"<redacted>\":\"\"\"auth\":\"<redacted>\"{token\":\"<redacted>\""],
    ["overlaps another secret in a literal that starts with a brace", "\"{x: TAILV8C0819,pass:\"", ["x: TAILV8C08", "819,pass"],
      "\"{<redacted>19,pass:<redacted>", null],
    ["ends a string that is followed by a closing brace", "\"pass\\\": 1.5}\"\":TAILVF\"", ["\": 1.5}\""],
      "\"<redacted>\"\"<redacted>\"", "\"pass\\<redacted>\":\"<redacted>\""],
  ];
  for (const [title, text, secrets, expected] of cases) {
    for (const depth of [0, 30, 31]) {
      test(`${title} (depth ${depth})`, () => {
        const out = run(nest(depth, text), secrets, true);
        expect(JSON.stringify(out)).not.toMatch(/TAILV[0-9A-F]/);
        expect(out).toEqual(nest(depth, expected));
      });
    }
  }
  test("a secret that ends inside a backslash sequence, in a string literal, is not left in part", () => {
    const out = run("\"=KEY%3D: TAILVCD68D9{\\\"null \\n\"", ["68D9{\\"], true);
    expect(out).not.toContain("68D9");
    // The reference text.
    expect(out).toBe("\"=KEY%3D<redacted>");
  });
});

// Brackets that enclose no JSON are text: next to the depth limit they are read as the reference reads them, not withheld.
describe("text with stray openers next to the depth limit", () => {
  const cases: [string, string[], string][] = [
    ["\"{\\\"{{Cookie%3d\"", [], "\"{\\\"{{Cookie%3d<redacted>"],
    ["\"[\\\"{{session:\"", ["se"], "\"[\\\"{{session:<redacted>"],
  ];
  for (const [text, secrets, expected] of cases) {
    test(`${text} at depth 30`, () => {
      expect(run(nest(30, text), secrets, true)).toEqual(nest(30, expected));
    });
  }
});
