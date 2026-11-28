import { describe, expect, test } from "@jest/globals";
import { scrubValue } from "../privacy-runtime";

// The reads that compare a decoded JSON string literal, object or name and value pair with its raw bytes stay linear in the text:
// four times the text takes at most about five times as long (best of five).
const words = (n: number) => { let s = "", i = 0; while (s.length < n) s += "word" + (i++ % 97) + " "; return s; };
const fill = (n: number, unit: (i: number) => string) => { const parts: string[] = []; let length = 0, i = 0; while (length < n) { const u = unit(i++); parts.push(u); length += u.length + 1; } return parts.join(" "); };
const nested = (layers: number) => { let t = JSON.stringify({ k: "LQ64F0FD13\" p", m: "note" }); for (let l = 0; l < layers; l++) t = "w " + JSON.stringify(t); return t; };
const shapes: [name: string, make: (n: number) => string, secrets: string[]][] = [
  ["JSON string literals of overlapping secrets spelled apart", n => fill(n, () => "w " + JSON.stringify(JSON.stringify({ k: "LQ64F0FD13\\u00e9 pxx", n: 31415926 })) + " " + words(400)), ["LQ64F0FD13", "F0FD13é pxx", "31415926"]],
  ["JSON string literals four layers deep", n => fill(n, () => nested(4) + " " + words(400)), ["LQ64F0FD13", "F0FD13\" p"]],
  ["JSON objects and pairs in prose", n => fill(n, () => 'a {"k": "LQ64F0FD13\\u00e9 pxx"} "n":"LQ64F0FD13\\" p" ' + words(300)), ["LQ64F0FD13", "F0FD13é pxx", 'F0FD13" p']],
];
const best = (text: string, secrets: string[]) => {
  let low = Infinity;
  for (let r = 0; r < 5; r++) { const started = process.hrtime.bigint(); scrubValue(text, secrets); low = Math.min(low, Number(process.hrtime.bigint() - started) / 1e6); }
  return low;
};

describe("decoded reads compared with their raw bytes", () => {
  test.each(shapes)("%s: linear time", (_, make, secrets) => {
    const small = make(100_000), large = make(400_000);
    best(small, secrets);
    // The least of three measured ratios, so a burst of load on the machine does not decide it.
    let ratio = Infinity;
    for (let attempt = 0; attempt < 3 && ratio >= 5.5; attempt++) ratio = Math.min(ratio, best(large, secrets) / Math.max(best(small, secrets), 1));
    expect(ratio).toBeLessThan(5.5);
  });
});
