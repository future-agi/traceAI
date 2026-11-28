import { describe, expect, test } from "@jest/globals";
import { scrubAttribute, scrubValue } from "../privacy-runtime";

// An attribute that is one JSON string (a quoted root, possibly holding further JSON string layers) with two known secrets that overlap,
// where the longer one is spelled as given only once a JSON string layer is decoded. The reference replaces known secrets on the raw
// bytes, where only the shorter secret is spelled as given, so all of it is hidden. scrubAttribute reads such a text as the string it
// encodes only where that read shows no run of a known secret that the read on the escaped bytes hides, as scrubValue does.

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
/** The runs of four code points of a secret that some reading of the output shows. */
function shownRuns(out: string, secret: string): string[] {
  const points = Array.from(secret), all = views(out), shown = new Set<string>();
  for (let i = 0; i + 4 <= points.length; i++) { const run = points.slice(i, i + 4).join(""); if (all.some(view => view.includes(run))) shown.add(run); }
  return [...shown];
}

const A = "LQ64F0FD13";
const AA = "a\u{1F600}bcF0FD", AB = "F0FDa\u{1F600}bZ\u{1F600}bcé";
const astralLeaf = "a\u{1F600}bcF0FDa\u{1F600}bZ\u{1F600}bc\\u00e9";

// [label, input, secrets, hidden secret, reference output]. The reference output is what the reference (d2dab4e8) and scrubValue export.
const quotedRoots: [string, string, string[], string, string][] = [
  ["a \\u escape", '"LQ64F0FD13\\u00e9 pxx"', [A, "F0FD13é pxx"], A, '"<redacted>\\u00e9 pxx"'],
  ["a \\u escape, the secrets in the other order", '"LQ64F0FD13\\u00e9 pxx"', ["F0FD13é pxx", A], A, '"<redacted>\\u00e9 pxx"'],
  ["a \\u escape, with JSON white space around it", ' \t"LQ64F0FD13\\u00e9 pxx"\n', [A, "F0FD13é pxx"], A, ' \t"<redacted>\\u00e9 pxx"\n'],
  ["an escaped backslash", '"LQ64F0FD13\\\\ pxx"', [A, "F0FD13\\ pxx"], A, '"<redacted>\\\\ pxx"'],
  ["an escaped quote", '"LQ64F0FD13\\" pxx"', [A, 'F0FD13" pxx'], A, '"<redacted>\\" pxx"'],
  ["an escaped line break", '"LQ64F0FD13\\n pxx"', [A, "F0FD13\n pxx"], A, '"<redacted>\\n pxx"'],
  ["an escaped solidus", '"LQ64F0FD13\\/ pxx"', [A, "F0FD13/ pxx"], A, '"<redacted>\\/ pxx"'],
  ["an escaped surrogate pair", '"LQ64F0FD13\\ud83d\\ude00 pxx"', [A, "F0FD13\u{1F600} pxx"], A, '"<redacted>\\ud83d\\ude00 pxx"'],
  ["a secret that holds a character outside the basic plane", '"' + astralLeaf + '"', [AA, AB], AA, '"<redacted>a\u{1F600}bZ\u{1F600}bc\\u00e9"'],
  // Two layers: a JSON string that holds a JSON object whose member value holds the escape.
  ["two layers, a \\u escape", JSON.stringify('{"k": "LQ64F0FD13\\u00e9 pxx"}'), [A, "F0FD13é pxx"], A, JSON.stringify('{"k": "<redacted>\\u00e9 pxx"}')],
  ["two layers, a \\u escape, the secrets in the other order", JSON.stringify('{"k": "LQ64F0FD13\\u00e9 pxx"}'), ["F0FD13é pxx", A], A, JSON.stringify('{"k": "<redacted>\\u00e9 pxx"}')],
  ["two layers, an escaped backslash", JSON.stringify(JSON.stringify({ k: "LQ64F0FD13\\ pxx" })), [A, "F0FD13\\ pxx"], A, JSON.stringify(JSON.stringify({ k: "<redacted>\\ pxx" }))],
  ["two layers, an escaped quote", JSON.stringify(JSON.stringify({ k: 'LQ64F0FD13" pxx' })), [A, 'F0FD13" pxx'], A, JSON.stringify(JSON.stringify({ k: '<redacted>" pxx' }))],
  ["two layers, an escaped surrogate pair", JSON.stringify('{"k":"LQ64F0FD13\\ud83d\\ude00 pxx"}'), [A, "F0FD13\u{1F600} pxx"], A, JSON.stringify('{"k":"<redacted>\\ud83d\\ude00 pxx"}')],
  ["two layers, a secret that holds a character outside the basic plane", JSON.stringify(`{"k": "${astralLeaf}"}`), [AA, AB], AA, JSON.stringify(`{"k": "<redacted>a\u{1F600}bZ\u{1F600}bc\\u00e9"}`)],
  ["two layers, a secret outside the basic plane, the secrets in the other order", JSON.stringify(`{"k": "${astralLeaf}"}`), [AB, AA], AA, JSON.stringify(`{"k": "<redacted>a\u{1F600}bZ\u{1F600}bc\\u00e9"}`)],
];

describe("an attribute that is one JSON string with overlapping known secrets spelled apart once decoded", () => {
  test.each(quotedRoots)("%s: no run of the shorter secret is shown", (_, input, secrets, hidden) => {
    expect(shownRuns(scrubAttribute(input, secrets), hidden)).toEqual([]);
  });
  test.each(quotedRoots)("%s: exported as scrubValue and the reference export it", (_, input, secrets, __, reference) => {
    expect(scrubValue(input, secrets)).toBe(reference);
    expect(scrubAttribute(input, secrets)).toBe(reference);
  });
});

// Tails whose characters JSON encodes with a backslash, so the longer secret is not spelled as given in the escaped bytes.
const TAILS = ["\\\\ p", "\\ p", "\" p", "\n p", "\\u0041 p", "\": 1", "é pxx", "\u{1F600} pxx"];
function family(layers: number): { input: string; secrets: string[]; label: string }[] {
  const cases: { input: string; secrets: string[]; label: string }[] = [];
  for (const tail of TAILS) for (const where of ["tail", "head"]) for (const shape of ["field", "obj", "arr", "lit", "pw"]) for (const frame of ["alone", "spaced"]) {
    let v: string, B: string;
    if (where === "tail") { v = A + tail; B = A.slice(4) + tail; }
    else { v = tail.trim() + A; B = tail.trim() + A.slice(0, 7); }
    if (B.length <= A.length) { B += "x".repeat(A.length + 1 - B.length); if (where === "tail") v += "xxxx"; }
    let t = ({ field: '"":' + JSON.stringify(v), obj: JSON.stringify({ k: v }), arr: JSON.stringify([v]), lit: "e " + JSON.stringify(v), pw: JSON.stringify({ note: v, password: "pw123456789" }) } as Record<string, string>)[shape];
    // A character outside ASCII is spelled with \u escapes in the innermost JSON text, so the longer secret is spelled apart there too.
    t = t.replace(/é/g, "\\u00e9").replace(/\u{1F600}/gu, "\\ud83d\\ude00");
    for (let k = 0; k < layers; k++) t = JSON.stringify(t);
    const input = frame === "alone" ? t : " " + t + "\n";
    for (const secrets of [[A, B], [B, A]]) cases.push({ input, secrets, label: JSON.stringify([tail, where, shape, frame, secrets[0] === A ? "short first" : "long first"]) });
  }
  return cases;
}

describe("an attribute that is one JSON string holding further JSON string layers, with overlapping known secrets", () => {
  test.each([1, 2, 3])("encoding layers %i: no run of the shorter secret is shown", layers => {
    const failures: string[] = [];
    for (const { input, secrets, label } of family(layers)) {
      let out: string;
      try { out = scrubAttribute(input, secrets); } catch (error) {
        // A value the scrub cannot read within its budgets is dropped whole.
        if (error instanceof Error && /^Privacy (structure|text) budget$/.test(error.message)) continue;
        throw error;
      }
      const shown = shownRuns(out, A);
      if (shown.length) failures.push(`${label}: ${shown.join(",")}`);
    }
    expect(failures).toEqual([]);
  });
});

describe("an attribute that is one JSON string with no secret spelled apart by its escapes", () => {
  // The attribute is read as the string it encodes, as before: its escapes are read, a known secret spelled only once decoded is hidden,
  // and a JSON text it holds is read as JSON.
  const kept: [string, string, string[], string][] = [
    ["no known secret", '"hello \\u00e9 world"', [], '"hello \\u00e9 world"'],
    ["the empty string", '""', [A], '""'],
    ["a known secret spelled as given", '"x LQ64F0FD13 y"', [A], '"x <redacted> y"'],
    ["a known secret beside a \\u escape", '"x LQ64F0FD13\\u00e9 y"', [A], '"x <redacted>é y"'],
    ["a known secret beside escaped backslashes and quotes", '"a\\\\b \\"c\\" LQ64F0FD13"', [A], '"a\\\\b \\"c\\" <redacted>"'],
    ["a known secret beside an escaped surrogate pair", '"\\ud83d\\ude00 LQ64F0FD13"', [A], '"\u{1F600} <redacted>"'],
    ["a known secret spelled only once its \\u escapes are read", '"\\u004c\\u005164F0FD13 y"', [A], '"<redacted> y"'],
    ["the whole text a known secret", '"LQ64F0FD13"', [A], '"<redacted>"'],
    ["two layers, a known secret beside a \\u escape", JSON.stringify('{"k": "LQ64F0FD13\\u00e9 pxx"}'), [A], JSON.stringify('{"k":"<redacted>é pxx"}')],
    ["two layers, a credential member", JSON.stringify(JSON.stringify({ note: "ok", password: "pw123456789" })), [], JSON.stringify(JSON.stringify({ note: "ok", password: "<redacted>" }))],
    // Overlapping known secrets where the read of the decoded string hides at least what the read on the escaped bytes hides; every
    // occurrence of each is hidden, so two that cover the string hide it whole.
    ["overlapping secrets that cover the string, the decoded read hiding the shorter one", '"p\\u00e9 LQ64F0FD13"', [A, "pé LQ64F0F"], '"<redacted>"'],
    ["overlapping secrets ending at a surrogate pair", '"LQ64F0FD13\\ud83d\\ude00 pxx"', [A, "F0FD13\u{1F600}"], '"<redacted>\u{1F600} pxx"'],
  ];
  test.each(kept)("%s", (_, input, secrets, expected) => {
    expect(scrubAttribute(input, secrets)).toBe(expected);
    expect(scrubAttribute(expected, secrets)).toBe(expected);
    expect(shownRuns(scrubAttribute(input, secrets), A)).toEqual([]);
  });
  test("a JSON string that holds a JSON number and a marker is read as JSON at each layer", () => {
    const payload = { n: 14142135, marker: "__REDACTED__", nested: JSON.stringify({ n: 14142135, marker: "__REDACTED__" }) };
    const clean = scrubAttribute(JSON.stringify(JSON.stringify(payload)), ["14142135"]);
    expect(JSON.parse(JSON.parse(clean))).toEqual({ n: "<redacted>", marker: "__REDACTED__", nested: JSON.stringify({ n: "<redacted>", marker: "__REDACTED__" }) });
  });
});
