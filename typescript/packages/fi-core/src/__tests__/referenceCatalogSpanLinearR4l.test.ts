import { describe, expect, test } from "@jest/globals";
import { scrubText, scrubValue } from "../privacy-runtime";

// The union of the URL catalog's edits and the registered secret occurrences they cut stays linear in the text: four times the
// text takes at most about five times as long (best of five, least of three ratios), for scrubValue and scrubText alike.
type Shape = [name: string, make: (n: number) => [string, string[]]];
const shapes: Shape[] = [
  // One long occurrence at the front that spans every later URL token, and a short one in each token.
  ["a long occurrence over many URL tokens", n => { const s = "https://e.invalid/p?a=PLNTHead9 ".repeat(n); return ["log " + s + "next", [s, "PLNTHead9"]]; }],
  // A long registered secret that a replaced URL value cuts, then many delimiter-followed replaced values.
  ["a long cut secret before many replaced values", n => { const s = "PLNTHead9" + "&k".repeat(n); return ["https://e.invalid/p?%74oken=" + s + "&" + "%74oken=v&a=x&".repeat(n), [s]]; }],
  // The same short secret cut in every pair of one URL token.
  ["a short secret cut in every pair", n => ["log https://e.invalid/p?" + "%74oken=PLNTHead9&PLNTTail7&".repeat(n) + " next", ["PLNTHead9&PLNTTail7"]]],
  // Self-overlapping occurrences that run from a replaced value.
  ["self-overlapping occurrences from a replaced value", n => ["log https://e.invalid/p?%74oken=" + "ab".repeat(n) + "&x=" + "ab".repeat(n) + " next", ["abababababababab"]]],
  // A long occurrence over many userinfo replacements, which cut no occurrence.
  ["a long occurrence over many userinfo replacements", n => { const u = "https://u:p@e.invalid ".repeat(n / 4); return ["log " + u + u + "next", [u]]; }],
  // The most secrets that are kept prepared (64), each with many overlapping occurrences from one replaced value.
  ["many secrets with overlapping occurrences", n => ["https://e.invalid/?%74oken=" + "ab".repeat(n) + "&safe=yes", Array.from({ length: 64 }, (_, i) => "ab".repeat(i + 4))]],
  // Many URL tokens, each with a secret cut by its replaced value.
  ["a cut secret in each of many URL tokens", n => ["see " + "https://e.invalid/p?%74oken=PLNTHead9&PLNTq7Zr4kXw9 ".repeat(n), ["PLNTHead9&PLNTq7Zr4kXw9"]]],
];
const scrubbers: [string, (text: string, secrets: string[]) => unknown][] = [
  ["scrubValue", (text, secrets) => scrubValue(text, secrets, "")],
  ["scrubText", (text, secrets) => scrubText(text, secrets)],
];
const best = (scrub: (text: string, secrets: string[]) => unknown, [text, secrets]: [string, string[]]) => {
  let low = Infinity;
  for (let r = 0; r < 5; r++) { const started = process.hrtime.bigint(); scrub(text, secrets); low = Math.min(low, Number(process.hrtime.bigint() - started) / 1e6); }
  return low;
};

describe.each(scrubbers)("%s: the catalog's span union", (_, scrub) => {
  test.each(shapes)("%s: linear time", (_, make) => {
    const small = make(2000), large = make(8000);
    best(scrub, small);
    let ratio = Infinity;
    for (let attempt = 0; attempt < 3 && ratio >= 5.5; attempt++) ratio = Math.min(ratio, best(scrub, large) / Math.max(best(scrub, small), 1));
    expect(ratio).toBeLessThan(5.5);
  });
});
