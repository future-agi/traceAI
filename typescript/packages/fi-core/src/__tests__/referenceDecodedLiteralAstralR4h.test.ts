import { describe, expect, test } from "@jest/globals";
import { scrubAttribute, scrubText, scrubValue } from "../privacy-runtime";

// Overlapping known secrets spelled apart once decoded, where the shorter one holds a character outside the basic plane: a reader reads
// runs of four code points, so none of the shorter secret's runs is shown, as in the reference, which hides it whole. And the reads
// that decode a text stay linear where it holds an unclosed run of escaped quotes.
const A = "a\u{1F600}bcF0FD", B = "F0FDa\u{1F600}bZ\u{1F600}bcé";
const leaf = "a\u{1F600}bcF0FDa\u{1F600}bZ\u{1F600}bc\\u00e9";
function readings(out: string): string[] {
  const seen = new Set<string>();
  let layer = [out];
  for (let depth = 0; depth < 8 && layer.length; depth++) {
    const next: string[] = [];
    for (const view of layer) {
      if (seen.has(view)) continue;
      seen.add(view);
      for (const match of view.matchAll(/"(?:[^"\\\n]|\\.)*"/g)) { try { const s = JSON.parse(match[0]); if (typeof s === "string") next.push(s); } catch { /* not a literal */ } }
      if (view.includes("\\")) next.push(view.replace(/\\(u[0-9a-fA-F]{4}|["\\/bfnrt])/g, (_, g: string) => g[0] === "u" ? String.fromCharCode(parseInt(g.slice(1), 16)) : ({ b: "\b", f: "\f", n: "\n", r: "\r", t: "\t" } as Record<string, string>)[g] ?? g));
    }
    layer = next;
  }
  return [...seen].map(view => view.split("<redacted>").join("\0"));
}
function shownCodePointRuns(out: string, secret: string): string[] {
  const points = Array.from(secret), shown = new Set<string>();
  for (let i = 0; i + 4 <= points.length; i++) { const run = points.slice(i, i + 4).join(""); if (readings(out).some(view => view.includes(run))) shown.add(run); }
  return [...shown];
}
const inputs = [
  `body {"k": "${leaf}"} end`,
  `list ["${leaf}", 1] ok`,
  `x "k": "${leaf}" y`,
  JSON.stringify(`{"k": "${leaf}"}`),
  "say " + JSON.stringify(JSON.stringify({ k: `${leaf}` })) + " ok",
];

describe("overlapping known secrets that hold a character outside the basic plane", () => {
  test.each(inputs)("%j: no run of four code points of the shorter secret is shown", input => {
    for (const secrets of [[A, B], [B, A]]) for (const depth of [0, 1, 30]) {
      expect(shownCodePointRuns(scrubValue(input, secrets, "", depth) as string, A)).toEqual([]);
    }
    expect(shownCodePointRuns(scrubText(input, [A, B]), A)).toEqual([]);
    // An attribute that is one JSON value is read as the value it encodes, leaf by leaf, as the reference reads a value.
    if (!input.startsWith('"')) expect(shownCodePointRuns(scrubAttribute(input, [A, B]), A)).toEqual([]);
  });
});

describe("a text with an unclosed run of escaped quotes and a known secret", () => {
  const shapes: [string, (inner: string) => string][] = [
    ["object", inner => 'body {"k": ' + JSON.stringify(inner) + "} end"],
    ["pair", inner => 'x "k": ' + JSON.stringify(inner) + " y"],
    ["literal", inner => "say " + JSON.stringify(JSON.stringify({ k: inner })) + " ok"],
  ];
  test.each(shapes)("%s: linear time", (_, make) => {
    const time = (n: number) => {
      const text = make('LQ64F0FD13zz "' + '\\"'.repeat(n));
      let low = Infinity;
      for (let r = 0; r < 5; r++) { const started = process.hrtime.bigint(); scrubValue(text, ["LQ64F0FD13zz", "QQQQ0123456789"]); low = Math.min(low, Number(process.hrtime.bigint() - started) / 1e6); }
      return low;
    };
    time(4000);
    let ratio = Infinity;
    for (let attempt = 0; attempt < 3 && ratio >= 5.5; attempt++) ratio = Math.min(ratio, time(32000) / Math.max(time(8000), 1));
    expect(ratio).toBeLessThan(5.5);
  });
});
