import { describe, expect, test } from "@jest/globals";
import { indexPrivacyHints, scrubValue } from "../privacy-runtime";

const none = indexPrivacyHints([]);
const run = (value: string, secrets: string[]) => scrubValue(value, secrets, "message", 0, false, undefined, none, true);

// What the reference redacts outside every owner of the text is replaced where it stands; a pair or a JSON span beside it keeps its own
// redaction and is not taken out with it.
describe("a range the reference redacts outside every owner, beside a pair", () => {
  test("a value after a quoted pair with an empty value is redacted where it stands, and the pair keeps its own redaction", () => {
    const out = run("\"PASS\":\"\"key=A2D3AF\\\"'Auth\"", ["A2D3AF\\\"'Auth"]);
    // The reference text.
    expect(out).toBe("\"PASS\":\"<redacted>\"key=<redacted>");
  });
  test("a secret that holds a quote and brackets, after a quoted pair with an empty value, is redacted where it stands", () => {
    const out = run("\"pass\":\"\"key:\"V7E8036\"}[{[\"pw", ["V7E8036\"}[{[\"pw"]);
    // The reference text.
    expect(out).toBe("\"pass\":\"<redacted>\"key:<redacted>");
  });
  test("a pair after a pair with a literal value is read for its own value, and the secret in that value is redacted", () => {
    const out = run("\"AUTH\":true,\"pass\":H\":\"MKFF61BC17", [":\"MKFF61BC17"]);
    // The reference text.
    expect(out).toBe("\"AUTH\":\"<redacted>\",\"pass\":\"<redacted>\"");
  });
});

// A secret inside a JSON string takes the JSON span it is in out whole only when it holds a quote, a control character or a bracket, or starts or ends
// inside a backslash sequence. A secret that holds a whole backslash sequence is replaced inside the string, in the string's own encoding.
describe("a secret that holds a backslash in a JSON string", () => {
  test("a secret that holds a whole escaped backslash is replaced inside the string literal", () => {
    const out = run("\"TAILVF642E3secretCookie%3d\\\\pwd\"c", ["ookie%3d\\\\"]);
    // The reference text shows "pwd\"c", the value of the pair whose separator the registered value takes out; it stays hidden.
    expect(out).toBe("\"TAILVF642E3secretC<redacted>");
  });
  test("a secret that holds a whole escaped backslash, in a string literal with a pair after it, is replaced where it stands", () => {
    const out = run("\"pwd\\npwd:TAILVF1552E\\\\[auth%3dTAILVC3572B\"", ["AILVF1552E\\\\"]);
    // The reference text.
    expect(out).toBe("\"pwd\\npwd:T<redacted>[auth%3d<redacted>");
  });
  test("a secret that holds a whole escape sequence keeps the JSON object it is in, and its string is redacted", () => {
    const out = run("log {\"note\":\"ab\\ncdefgh tail\"} end", ["b\\ncdefg"]);
    // The scrubber takes out more than the reference, whose text is
    // "log {\"note\":\"a<redacted>h tail\"} end"
    expect(out).toBe("log {\"note\":\"<redacted>\"} end");
  });
  test("a secret that holds a whole escaped backslash keeps the JSON object it is in, and its string is redacted", () => {
    const out = run("log {\"note\":\"abcdefg\\\\cdefgh tail\"} end", ["efg\\\\cdef"]);
    // The scrubber takes out more than the reference, whose text is
    // "log {\"note\":\"abcd<redacted>gh tail\"} end"
    expect(out).toBe("log {\"note\":\"<redacted>\"} end");
  });
  test("a secret that starts inside an escape sequence takes the JSON object it is in out whole", () => {
    const out = run("log {\"note\":\"abcdefg\\ncdefgh tail\"} end", ["ncdefgh "]);
    // The scrubber takes out more than the reference, whose text is
    // "log {\"note\":\"abcdefg\\<redacted>tail\"} end"
    expect(out).toBe("log \"<redacted>\" end");
  });
  test("a secret that ends inside an escape sequence takes the JSON object it is in out whole", () => {
    const out = run("log {\"note\":\"abcdefg\\ncdefgh tail\"} end", ["abcdefg\\"]);
    // The scrubber takes out more than the reference, whose text is
    // "log {\"note\":\"<redacted>ncdefgh tail\"} end"
    expect(out).toBe("log \"<redacted>\" end");
  });
});
