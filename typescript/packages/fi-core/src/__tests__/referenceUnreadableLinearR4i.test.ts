import { describe, expect, test } from "@jest/globals";
import { scrubValue } from "../privacy-runtime";

// The read of a text that begins as a JSON object or array and is not one JSON value (its JSON prefix, its quoted member names,
// its JSON string forms spelled out and read by the pair rule) stays linear in the text: four times the text takes at most about
// five times as long (best of five, least of three ratios).
const words = (n: number) => { let s = "", i = 0; while (s.length < n) s += "word" + (i++ % 97) + " "; return s; };
const shapes: [name: string, make: (n: number) => string][] = [
  ["a run of quotes after an opening bracket", n => "[" + '"'.repeat(n) + " x"],
  ["JSON string forms in a member value", n => '{"note": "' + "\\u0041b\\n".repeat(n / 9) + '"} x'],
  ["opening brackets with no close", n => "[".repeat(n / 2) + " x"],
  ["quoted names before colons with no values", n => "{" + '"note":'.repeat(n / 7) + " x"],
  ["an object then prose with spelled pairs", n => '{"note": "a"} ' + "k\\u003dv ".repeat(n / 9) + words(200)],
];
const best = (text: string) => {
  let low = Infinity;
  for (let r = 0; r < 5; r++) { const started = process.hrtime.bigint(); scrubValue(text, []); low = Math.min(low, Number(process.hrtime.bigint() - started) / 1e6); }
  return low;
};

describe("a text that begins as a JSON object or array and is not one JSON value", () => {
  test.each(shapes)("%s: linear time", (_, make) => {
    const small = make(100_000), large = make(400_000);
    best(small);
    let ratio = Infinity;
    for (let attempt = 0; attempt < 3 && ratio >= 5.5; attempt++) ratio = Math.min(ratio, best(large) / Math.max(best(small), 1));
    expect(ratio).toBeLessThan(5.5);
  });
});
