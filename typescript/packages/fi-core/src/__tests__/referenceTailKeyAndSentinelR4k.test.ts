import { describe, expect, test } from "@jest/globals";
import { scrubText, scrubValue } from "../privacy-runtime";

// Free text that starts as a JSON object or array and ends after a credential member name is redacted as a whole. A cut-off
// member name after prose is still read as text. A quoted literal whose raw-bytes read exceeds its own node allowance is not
// exported using the decoded read that exposed a run of a known secret.

const shortSecret = "PLNTq7Zr4kXw9";
const overlap = [shortSecret, "q7Zr4kXw9é pxx"];
const tokenTail = '{"\\u0074oken":';
const overlapTail = '{"\\u0074oken PLNTq7Zr4kXw9\\u00e9 pxx":';

describe("a JSON text cut off immediately after a quoted member name", () => {
  const hidden: [name: string, input: string, secrets: string[]][] = [
    ["plain name", '{"token":', []],
    ["JSON escape", tokenTail, []],
    ["percent form", '{"pass%77ord":', []],
    ["white space and a byte order mark", ' \t\ufeff {"pass\\u0077ord": ', []],
    ["object in an array", "[" + tokenTail, []],
    ["overlapping registered secrets", overlapTail, overlap],
    ["overlapping registered secrets in reverse order", overlapTail, [...overlap].reverse()],
  ];
  test.each(hidden)("%s: the whole cut-off text is the placeholder", (_name, input, secrets) => {
    expect(scrubValue(input, secrets)).toBe("<redacted>");
    expect(scrubText(input, secrets)).toBe("<redacted>");
  });

  const kept: [name: string, input: string, secrets: string[], expected: string][] = [
    ["credential name after prose", "note " + tokenTail, [], "note " + tokenTail],
    ["overlap after prose", "note " + overlapTail, overlap, 'note {"\\u0074oken <redacted>\\u00e9 pxx":'],
    ["ordinary member name", '{"\\u0074opic":', [], '{"\\u0074opic":'],
    ["overlap in an ordinary name", '{"PLNTq7Zr4kXw9\\u00e9 pxx":', overlap, '{"<redacted>\\u00e9 pxx":'],
    ["complete credential member", '{"\\u0074oken":"VAL1"}', [], '{"token":"<redacted>"}'],
    ["data URI followed by prose", "data:audio/wav;base64,AAAA trailing prose", [], "<omitted: 4 base64 chars> trailing prose"],
  ];
  test.each(kept)("%s: the rest of the text is preserved", (_name, input, secrets, expected) => {
    expect(scrubText(input, secrets)).toBe(expected);
  });
});

describe("a JSON string literal whose raw-bytes read loses the MIME hint", () => {
  const secrets = [...overlap, "audio/wav"];
  const zeros = (n: number) => "[" + Array<number>(n).fill(0).join(",") + "]";
  // Before the MIME hint is hidden, the numeric data array is omitted as media. With the hint already hidden, the read visits
  // every number before it reaches the tail array. The overlapping secrets make the decoded read show PLNT, so that read is
  // not taken unless the raw-bytes read shows at least as much.
  const message = (n: number, mime: string, keyValue: string) => "note " + JSON.stringify(
    `{"mime_type":"${mime}","pad":${zeros(8000)},"data":${zeros(n)},"tail":[0,0,0,0],"k":"${keyValue}\\u00e9 pxx"}`,
  ) + " end";

  test.each([scrubText, scrubValue])("%p exports the raw-bytes read within both node budgets", (scrub) => {
    expect(scrub(message(8372, "audio/wav", shortSecret), secrets)).toBe(message(8372, "<redacted>", "<redacted>"));
  });

  test.each([scrubText, scrubValue])("%p fails closed when the raw-bytes read exhausts its own node budget", (scrub) => {
    expect(() => scrub(message(8375, "audio/wav", shortSecret), secrets)).toThrow(new Error("Privacy structure budget"));
  });
});
