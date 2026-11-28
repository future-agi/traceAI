import { describe, expect, test } from "@jest/globals";
import { scrubText, scrubValue } from "../privacy-runtime";

// A percent-spelled credential name in a URL query or fragment ('?%74oken=...') has its value replaced by the URL catalog. That
// replaced value (the placeholder, then '&', '#', ';' or the end of the text, inside the URL token) is not read again as a prose
// pair, so the query pairs and the fragment after it stay as the in-tree reference (40c761f6) exports them. Outside a URL token, a
// percent-spelled pair is read as a pair and replaced to the end of its value, as d2dab4e8 does, also where its value starts with
// the placeholder text or a registered secret was replaced there first.

const SECRET = "PLNTq7Zr4kXw9";

// [input, expected output]
const URL_PLACEHOLDER: [string, string][] = [
  [`see https://example.invalid/object?%74oken=${SECRET}&model=keep&%74oken=second#anchor next`,
    "see https://example.invalid/object?%74oken=<redacted>&model=keep&%74oken=<redacted>#anchor next"],
  [`https://e.invalid/p?pass%77ord=${SECRET}#frag rest`, "https://e.invalid/p?pass%77ord=<redacted>#frag rest"],
  [`see https://e.invalid/p?a=1&%74oken=${SECRET}&safe=yes#top next`, "see https://e.invalid/p?a=1&%74oken=<redacted>&safe=yes#top next"],
  [`https://e.invalid/p?%73ecret=${SECRET}&safe=yes`, "https://e.invalid/p?%73ecret=<redacted>&safe=yes"],
];

const PROSE_PAIR: [string, string][] = [
  [`tok%65n=${SECRET}&safe=yes`, "tok%65n=<redacted>"],
  [`log tok%65n=${SECRET} end`, "log tok%65n=<redacted>"],
  ["tok%65n=<redacted>x&y=1", "tok%65n=<redacted>"],
  ["tok%65n=<redacted>&safe=yes", "tok%65n=<redacted>"],
  ["tok%65n%3D<redacted>&x=1", "tok%65n%3D<redacted>"],
  ["tok%65n=<redacted>#keep", "tok%65n=<redacted>"],
  ["tok%65n=<redacted>;keep=1", "tok%65n=<redacted>;keep=1"],
];

// [input, registered secrets, expected output]: the placeholder in prose came from the input or from a registered secret.
const PROSE_SECRET: [string, string[], string][] = [
  ["log tok%65n=<redacted>&PLNTTail7", ["<redacted>&PLNTTail7"], "log tok%65n=<redacted>"],
  ["log tok%65n=<redacted>#PLNTTail7", ["<redacted>#PLNTTail7"], "log tok%65n=<redacted>"],
  ["log tok%65n=PLNTHead9&PLNTTail7", ["PLNTHead9"], "log tok%65n=<redacted>"],
  ["log tok%65n=PLNTHead9#PLNTTail7", ["PLNTHead9"], "log tok%65n=<redacted>"],
];

// [input, registered secrets, expected output]: a registered secret that runs from a URL value the catalog replaces over the delimiter
// after it is replaced whole; no part of it is left after the placeholder (the d2dab4e8 Reference's output).
const URL_SECRET: [string, string[], string][] = [
  ["https://e.invalid/#%74oken=PLNTHead9&PLNTq7Zr4kXw9 next", ["PLNTHead9&PLNTq7Zr4kXw9"], "https://e.invalid/#%74oken=<redacted>"],
  ["/p?%74oken=PLNTHead9&PLNTq7Zr4kXw9 next", ["PLNTHead9&PLNTq7Zr4kXw9"], "/p?%74oken=<redacted>"],
  ["log https://e.invalid/p;a=1;pa%73sword=PLNTHead9#PLNTq7Zr4kXw9 next", ["PLNTHead9#PLNTq7Zr4kXw9"], "log https://e.invalid/p;a=1;pa%73sword=<redacted>"],
  ["{\"note\": \"https://e.invalid/?a=1&pa%73sword=PLNTHead9&PLNTq7Zr4kXw9 next\"}", ["PLNTHead9&PLNTq7Zr4kXw9"], "{\"note\":\"https://e.invalid/?a=1&pa%73sword=<redacted>\"}"],
  ["log https://e.invalid/?Co%6Fkie=PLNTHead9#PLNTq7Zr4kXw9=PLNTHead9 next", ["PLNTHead9#PLNTq7Zr4kXw9"], "log https://e.invalid/?Co%6Fkie=<redacted>"],
];

// [input, registered secrets, expected output]: the same in a whole URL value, after a name read once decoded or left undecided.
const WHOLE_URL_SECRET: [string, string[], string][] = [
  ["https://e.invalid/p?ke%2579=PLNTq7Zr4kXw9&b=PLNTHead9", ["PLNTq7Zr4kXw9&b=PLNTHead9"], "https://e.invalid/p?ke%2579=<redacted>"],
  ["https://e.invalid/p/ke%2579=PLNTq7Zr4kXw9?b=PLNTHead9", ["PLNTq7Zr4kXw9?b=PLNTHead9"], "https://e.invalid/p/ke%2579=<redacted>"],
  ["https://e.invalid/p?a=PLNTq7Zr4kXw9&safe=yes", ["PLNTq7Zr4kXw9"], "https://e.invalid/p?a=<redacted>&safe=yes"],
];

describe("a value the URL catalog already replaced is not read again as a percent-spelled prose pair", () => {
  test.each(URL_PLACEHOLDER)("%j keeps the pairs and fragment after the replaced value", (input, expected) => {
    expect(scrubValue(input, [], "")).toEqual(expected);
  });
  test.each(URL_SECRET)("%j (registered %j) leaves no part of the secret after the replaced URL value", (input, secrets, expected) => {
    const out = scrubValue(input, secrets, "");
    expect(out).toEqual(expected);
    expect(String(out)).not.toContain("q7Zr");
  });
  test.each(WHOLE_URL_SECRET)("%j (registered %j) as a whole URL value leaves no part of the secret", (input, secrets, expected) => {
    const out = scrubValue(input, secrets, "");
    expect(out).toEqual(expected);
    expect(String(out)).not.toContain("Head");
  });
  test("a URL with many values that cut the same registered secret is read in time linear in its length", () => {
    const input = "https://e.invalid/p?" + "%74oken=PLNTHead9&PLNTq7Zr4kXw9&".repeat(8000);
    const started = Date.now();
    const out = scrubText(input, ["PLNTHead9&PLNTq7Zr4kXw9"]);
    expect(Date.now() - started).toBeLessThan(8000);
    expect(String(out)).not.toContain("q7Zr");
  });
  test.each(PROSE_SECRET)("%j (registered %j) is still read as a pair to the end of its value", (input, secrets, expected) => {
    const out = scrubValue(input, secrets, "");
    expect(out).toEqual(expected);
    expect(String(out)).not.toContain("PLNTTail7");
  });
  test("a long JSON array after a prose placeholder value is replaced with the value, as before", () => {
    const input = "log tok%65n=<redacted>&" + JSON.stringify(Array(16383).fill(0));
    expect(scrubValue(input, [], "")).toEqual("log tok%65n=<redacted>");
  });
  test.each(PROSE_PAIR)("%j is still read as a pair", (input, expected) => {
    const out = scrubValue(input, [], "");
    expect(out).toEqual(expected);
    expect(String(out)).not.toContain(SECRET.slice(0, 4));
  });
});
