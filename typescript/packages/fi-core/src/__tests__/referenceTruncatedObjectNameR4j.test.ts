import { describe, expect, test } from "@jest/globals";
import { scrubAttribute, scrubText, scrubValue } from "../privacy-runtime";

// A text that ends in a JSON object cut off right after its first member name ('{"name":'), where two known secrets overlap in that name
// and the longer one is spelled as given only once the name's JSON string forms are read (it holds the character an escape such as é
// stands for). The reference replaces known secrets on the raw bytes, where only the shorter secret is spelled as given, so all of it is
// hidden and the escape is kept as written. The name is exported that way: no reading of the export shows a run of the shorter secret.

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
function shownRuns(out: unknown, secret: string): string[] {
  const text = typeof out === "string" ? out : JSON.stringify(out);
  const runs = new Set<string>();
  for (let i = 0; i + 4 <= secret.length; i++) runs.add(secret.slice(i, i + 4));
  const shown = new Set<string>();
  for (const view of views(text)) for (const run of runs) if (view.includes(run)) shown.add(run);
  return [...shown];
}

const A = "LQ64F0FD13";
const B = "F0FD13é pxx";
// The six characters é sit in the name; the longer secret holds the character they stand for.
const NAME = '{"LQ64F0FD13\\u00e9 pxx":';
const HIDDEN = '{"<redacted>\\u00e9 pxx":';

describe("a truncated JSON object whose only member name holds an escape and two overlapping known secrets", () => {
  // [input, secrets, depth, path, the reference's export]
  const cases: [input: string, secrets: string[], depth: number, path: string, expected: string][] = [
    [NAME, [A, B], 0, "", HIDDEN],
    [NAME, [B, A], 0, "", HIDDEN],
    [NAME, [A, B], 5, "", HIDDEN],
    [NAME, [A, B], 31, "", HIDDEN],
    [NAME, [A, B], 0, "output.value", HIDDEN],
    [NAME, [A, B], 3, "input.value", HIDDEN],
    ["note " + NAME, [A, B], 0, "", "note " + HIDDEN],
    ["note " + NAME, [A, B], 31, "", "note " + HIDDEN],
    ["x: " + NAME, [A, B], 0, "", "x: " + HIDDEN],
    ['{ "LQ64F0FD13\\u00e9 pxx" : ', [A, B], 0, "", '{ "<redacted>\\u00e9 pxx" : '],
    // A name that holds two backslashes, an escaped quote, a line break, an escaped slash or an escaped ASCII letter.
    ['{"LQ64F0FD13\\\\ pxx":', [A, "F0FD13\\ pxx"], 0, "", '{"<redacted>\\\\ pxx":'],
    ['{"LQ64F0FD13\\" pxx":', [A, 'F0FD13" pxx'], 0, "", '{"<redacted>\\" pxx":'],
    ['{"LQ64F0FD13\\n pxx":', [A, "F0FD13\n pxx"], 0, "", '{"<redacted>\\n pxx":'],
    ['{"LQ64F0FD13\\/ pxx":', [A, "F0FD13/ pxx"], 0, "", '{"<redacted>\\/ pxx":'],
    ['{"LQ64F0FD13\\u0041 pxx":', [A, "F0FD13A pxx"], 0, "", '{"<redacted>\\u0041 pxx":'],
    // A character outside the Basic Multilingual Plane, spelled as a surrogate pair of escapes.
    ['{"LQ64F0FD13\\ud83d\\ude00 pxx":', [A, "F0FD13\u{1F600} pxx"], 0, "", '{"<redacted>\\ud83d\\ude00 pxx":'],
    ['{"LQ64F0FD13\\ud83d\\ude00 pxx":', [A, "F0FD13\u{1F600} pxx"], 5, "", '{"<redacted>\\ud83d\\ude00 pxx":'],
    // The longer secret takes the head of the shorter one.
    ['{"pxx \\u00e9LQ64F0FD13":', [A, "pxx éLQ64F0"], 0, "", '{"pxx \\u00e9<redacted>":'],
    ['note {"pxx \\u00e9LQ64F0FD13":', [A, "pxx éLQ64F0"], 0, "", 'note {"pxx \\u00e9<redacted>":'],
  ];
  test.each(cases)("%j with %j at depth %i, path %j, is exported as the reference exports it", (input, secrets, depth, path, expected) => {
    const out = scrubValue(input, secrets, path, depth);
    expect(out).toBe(expected);
    expect(shownRuns(out, A)).toEqual([]);
  });
  test.each(cases.filter(([, , depth, path]) => depth === 0 && path === ""))("%j with %j through scrubText and scrubAttribute", (input, secrets, _depth, _path, expected) => {
    expect(scrubText(input, secrets)).toBe(expected);
    expect(scrubAttribute(input, secrets)).toBe(expected);
    expect(shownRuns(scrubAttribute(input, [...secrets].reverse()), A)).toEqual([]);
  });

  test("as a string leaf of a dict or a list, beside other values", () => {
    expect(scrubValue({ m: NAME, n: 2 }, [A, B])).toEqual({ m: HIDDEN, n: 2 });
    expect(scrubValue({ m: { q: NAME } }, [A, B])).toEqual({ m: { q: HIDDEN } });
    expect(scrubValue([1, NAME], [A, B])).toEqual([1, HIDDEN]);
    expect(scrubValue([NAME], [B, A], "", 5)).toEqual([HIDDEN]);
  });

  test("inside a JSON document, as a string value after prose", () => {
    for (const depth of [0, 5]) {
      expect(scrubValue(JSON.stringify({ m: "note " + NAME }), [A, B], "", depth)).toBe(JSON.stringify({ m: "note " + HIDDEN }));
      expect(scrubValue(JSON.stringify(["note " + NAME]), [A, B], "", depth)).toBe(JSON.stringify(["note " + HIDDEN]));
    }
    const doc = JSON.stringify({ m: "note " + NAME });
    expect(scrubText(doc, [A, B])).toBe(JSON.stringify({ m: "note " + HIDDEN }));
    expect(scrubAttribute(doc, [A, B])).toBe(JSON.stringify({ m: "note " + HIDDEN }));
  });

  test("every spelling, either overlap, any prefix and spacing, at param depths 0, 5 and 31: the shorter secret is hidden whole", () => {
    // The reference hides the shorter secret on the raw bytes and keeps the rest of the name as written. An export may hide more of the
    // name than that (a name that reads as a path can be the placeholder whole); no reading of it shows a run of the shorter secret.
    // [the name's JSON spelling of one character, the character itself]
    const spellings: [string, string][] = [["\\u00e9", "é"], ["\\\\", "\\"], ['\\"', '"'], ["\\n", "\n"], ["\\ud83d\\ude00", "\u{1F600}"], ["\\/", "/"], ["\\u0041", "A"], ["\\t", "\t"]];
    const failures: string[] = [];
    for (const [spelled, read] of spellings) for (const where of ["tail", "head"]) for (const lead of ["", "note ", "x: "]) for (const [open, colon] of [["{", ":"], ["{ ", " : "], ["{", ": "]]) {
      const name = where === "tail" ? A + spelled + " pxx" : "pxx " + spelled + A;
      const longer = where === "tail" ? A.slice(4) + read + " pxx" : "pxx " + read + A.slice(0, 6);
      const input = lead + open + '"' + name + '"' + colon;
      for (const depth of [0, 5, 31]) {
        const out = scrubValue(input, [A, longer], "", depth);
        if (shownRuns(out, A).length) failures.push(`${JSON.stringify(input)} depth ${depth}: ${JSON.stringify(out)}`);
      }
      for (const out of [scrubText(input, [longer, A]), scrubAttribute(input, [longer, A])]) {
        if (shownRuns(out, A).length) failures.push(`${JSON.stringify(input)} text/attribute: ${JSON.stringify(out)}`);
      }
    }
    expect(failures).toEqual([]);
  });
});
