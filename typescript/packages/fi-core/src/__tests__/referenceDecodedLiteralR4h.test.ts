import { describe, expect, test } from "@jest/globals";
import { scrubText, scrubValue } from "../privacy-runtime";

// Two known secrets that overlap, where the longer one is spelled as given only once a JSON string literal of the text is decoded (it
// holds a quote, a backslash, a line break or the text of an escape). The reference replaces known secrets on the raw bytes, where only
// the shorter secret is spelled as given, so all of it is hidden. A JSON string literal that holds a JSON text is read as the string it
// encodes only where that read shows no run of a known secret that its read on the escaped bytes hides, at every layer of decoding.

/** Every reading of an output: the text, each JSON text and JSON string literal in it decoded, and its escapes read, again and again. */
function views(out: string): string[] {
  const seen = new Set<string>();
  let layer = [out];
  for (let depth = 0; depth < 8 && layer.length; depth++) {
    const next: string[] = [];
    for (const view of layer) {
      if (seen.has(view)) continue;
      seen.add(view);
      const trimmed = view.trim();
      if (/^[[{"]/.test(trimmed)) {
        try {
          const strings: string[] = [];
          const walk = (node: unknown): void => {
            if (typeof node === "string") strings.push(node);
            else if (node && typeof node === "object") for (const [key, child] of Object.entries(node)) { strings.push(key); walk(child); }
          };
          walk(JSON.parse(trimmed));
          next.push(...strings);
        } catch { /* not JSON */ }
      }
      for (const match of view.matchAll(/"(?:[^"\\\n]|\\.)*"/g)) { try { const s = JSON.parse(match[0]); if (typeof s === "string") next.push(s); } catch { /* not a literal */ } }
      if (view.includes("\\")) next.push(view.replace(/\\(u[0-9a-fA-F]{4}|["\\/bfnrt])/g, (_, g: string) => g[0] === "u" ? String.fromCharCode(parseInt(g.slice(1), 16)) : ({ b: "\b", f: "\f", n: "\n", r: "\r", t: "\t" } as Record<string, string>)[g] ?? g));
    }
    layer = next;
  }
  return [...seen].map(view => view.split("<redacted>").join("\0"));
}
/** The four-character runs of a secret that some reading of the output shows. */
function shownRuns(out: string, secret: string): string[] {
  const runs = new Set<string>();
  for (let i = 0; i + 4 <= secret.length; i++) runs.add(secret.slice(i, i + 4));
  const shown = new Set<string>();
  for (const view of views(out)) for (const run of runs) if (view.includes(run)) shown.add(run);
  return [...shown];
}

const A = "LQ64F0FD13";
// Tails whose characters JSON encodes with a backslash, so the longer secret is not spelled as given in the escaped bytes.
const TAILS = ["\\\\ p", "\\ p", "\" p", "\n p", "\\u0041 p", "\": 1"];
function family(layers: number): { input: string; secrets: string[]; label: string }[] {
  const cases: { input: string; secrets: string[]; label: string }[] = [];
  for (const tail of TAILS) for (const where of ["tail", "head"]) for (const shape of ["field", "obj", "arr", "lit", "pw"]) for (const frame of ["alone", "prose", "jsonval"]) {
    let v: string, B: string;
    if (where === "tail") { v = A + tail; B = A.slice(4) + tail; }
    else { v = tail.trim() + A; B = tail.trim() + A.slice(0, 7); }
    if (B.length <= A.length) { B += "x".repeat(A.length + 1 - B.length); if (where === "tail") v += "xxxx"; }
    let t = ({ field: '"":' + JSON.stringify(v), obj: JSON.stringify({ k: v }), arr: JSON.stringify([v]), lit: "e " + JSON.stringify(v), pw: JSON.stringify({ note: v, password: "pw123456789" }) } as Record<string, string>)[shape];
    for (let k = 0; k < layers; k++) t = JSON.stringify(t);
    const input = ({ alone: t, prose: "body: " + t + " retry", jsonval: JSON.stringify({ k: t }) } as Record<string, string>)[frame];
    cases.push({ input, secrets: [A, B], label: JSON.stringify([tail, where, shape, frame]) });
  }
  return cases;
}

describe("overlapping known secrets spelled apart at an inner layer of a JSON string literal", () => {
  test("the review's case: the shorter secret is hidden whole", () => {
    const input = JSON.stringify('{"k": "LQ64F0FD13\\u00e9 pxx"}');
    const out = scrubValue(input, [A, "F0FD13é pxx"]) as string;
    expect(out).toBe(JSON.stringify('{"k": "<redacted>\\u00e9 pxx"}'));
    expect(shownRuns(out, A)).toEqual([]);
    expect(scrubText(input, [A, "F0FD13é pxx"])).toBe(out);
  });
  test("a longer secret that holds two backslashes", () => {
    const input = JSON.stringify(JSON.stringify({ k: "LQ64F0FD13\\\\ p" }));
    const out = scrubValue(input, [A, "F0FD13\\\\ p"]) as string;
    expect(shownRuns(out, A)).toEqual([]);
  });
  test("a secret that holds a quote, beside a JSON number", () => {
    const input = '"\\"\\":\\"n\\\\\\": 314159265\\""';
    const out = scrubValue(input, ['n": 31415', "14159265"]) as string;
    expect(shownRuns(out, "14159265")).toEqual([]);
  });
  test.each([1, 2, 3, 4])("encoding layers %i, param depths 0 to 31: no run of the shorter secret is shown", layers => {
    const failures: string[] = [];
    for (const { input, secrets, label } of family(layers)) for (let depth = 0; depth <= 31; depth += layers === 1 ? 1 : 5) {
      let out: unknown;
      try { out = scrubValue(input, secrets, "", depth); } catch (error) {
        if (!(error instanceof Error) || !/^Privacy (structure|text) budget$/.test(error.message)) throw error;
        // A value the scrub cannot read within its depth limit or budgets is dropped whole; that does not depend on its known secrets, so
        // the same value read with none is dropped too. Comparing the reads of a literal is not charged to the value's budgets.
        let plain = true;
        try { scrubValue(input, [], "", depth); plain = false; } catch (other) {
          if (!(other instanceof Error) || !/^Privacy (structure|text) budget$/.test(other.message)) throw other;
        }
        if (!plain) failures.push(`${label} depth ${depth}: ${error.message} only with its known secrets`);
        continue;
      }
      const shown = shownRuns(String(out), A);
      if (shown.length) failures.push(`${label} depth ${depth}: ${shown.join(",")}`);
    }
    expect(failures).toEqual([]);
  });
});

describe("overlapping known secrets spelled apart in a JSON object or a name and value pair of a text", () => {
  // The text is not one JSON document: the reference replaces its known secrets on the raw bytes, where only the shorter secret is spelled
  // as given, so all of it is hidden. No reading of the export shows a run of it.
  const cases: [input: string, secrets: string[], hidden: string][] = [
    ['body: {"k": "LQ64F0FD13\\u00e9 pxx"} retry', [A, "F0FD13é pxx"], A],
    ['body: [1, "LQ64F0FD13\\" p"] retry', [A, 'F0FD13" p'], A],
    ['note {"a": {"b": "x LQ64F0FD13\\\\ p"}} end', [A, "F0FD13\\ p"], A],
    ['"":"n\\": 314159265"', ['n": 31415', "14159265"], "14159265"],
    ['body: "":"n\\": 314159265" retry', ['n": 31415', "14159265"], "14159265"],
    ['note "k":"M3€pubD7z\\n/Q10h" end', ["7z\n/Q10h", "M3€pubD7z"], "M3€pubD7z"],
  ];
  test.each(cases)("%j with %j", (input, secrets, hidden) => {
    for (const depth of [0, 1, 15, 29]) {
      expect(shownRuns(scrubValue(input, secrets, "", depth) as string, hidden)).toEqual([]);
      expect(shownRuns(scrubValue(input, [...secrets].reverse(), "", depth) as string, hidden)).toEqual([]);
    }
    expect(shownRuns(scrubText(input, secrets), hidden)).toEqual([]);
  });
});

describe("a run of the shorter secret beside a copy of it that only the decoded read redacts", () => {
  // The escaped '\/' of a URL is read as a URL with userinfo only once decoded, so the decoded read hides a copy of LQ64 there and would
  // show the one the shorter secret holds; the read on the escaped bytes hides the secret whole, as the reference does.
  const S = [A, "F0FD13é pxx"];
  const object = '{"u": "https:\\/\\/bob:LQ64zzzz@h.io/x", "k": "LQ64F0FD13\\u00e9 pxx"}';
  test.each([
    "note " + object + " end",
    'note "k": "https:\\/\\/bob:LQ64zzzz@h.io/x LQ64F0FD13\\u00e9 pxx" end',
    JSON.stringify(object),
    "say " + JSON.stringify(object) + " ok",
  ])("%j", input => {
    for (const secrets of [S, [...S].reverse()]) {
      const out = scrubValue(input, secrets) as string;
      expect(out).not.toMatch(/LQ64<redacted>/);
      expect(views(out).some(view => view.includes("LQ64\0"))).toBe(false);
    }
  });
});
