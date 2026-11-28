import { describe, expect, test } from "@jest/globals";
import { scrubAttribute, scrubText, scrubValue } from "../privacy-runtime";

// A JSON string literal of a text that is not one JSON string once its known secrets are replaced on its escaped bytes (here a
// secret that starts after the `\u` of a unicode escape, so its four hex digits are cut) is exported as the quoted placeholder: the
// whole literal goes, and no run of the secret or of the characters the escape began is readable. The reference replaces the secret
// in the raw bytes and keeps the rest of the literal (`"\u<redacted>FGHIJ"`), so this hides more than the reference does.
const cases: [input: string, secret: string, expected: string][] = [
  ['"\\u0041BCDEFGHIJ"', "0041BCDE", '"<redacted>"'],
  ['say "\\u0041BCDEFGHIJ" ok', "0041BCDE", 'say "<redacted>" ok'],
  ['{"a":1} "x\\u00e9BCDEFGHIJ" y', "00e9BCDEF", '{"a":1} "<redacted>" y'],
];

describe("a JSON string literal that is not one JSON string once its known secrets are replaced", () => {
  test.each(cases.flatMap(([input, secret, expected]) => [0, 29].flatMap(depth => [true, false].map(framed => [input, secret, expected, depth, framed] as [string, string, string, number, boolean]))))(
    "%j with %j at depth %i, framed %s", (input, secret, expected, depth, framed) => {
      expect(scrubValue(input, [secret], "", depth, false, undefined, undefined, framed)).toBe(expected);
    });
  test.each(cases)("%j with %j through the attribute and text filters", (input, secret, expected) => {
    expect(scrubAttribute(input, [secret])).toBe(expected);
    expect(scrubText(input, [secret])).toBe(expected);
  });
});
