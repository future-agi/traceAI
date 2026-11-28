import { describe, expect, test } from "@jest/globals";
import { scrubText, scrubValue } from "../privacy-runtime";

// Two known secrets that overlap, where the longer one is spelled as given only once a JSON string literal of the text is decoded. The
// reference replaces known secrets on the raw bytes, longest first, so the shorter one is hidden whole. A decoded read is exported only
// where no reading of it, at any layer, shows a run of a known secret more often than the read on the raw bytes does. These cases pin
// what is exported where those readings reach their own bounds (the decoded read, or the raw-bytes read, has more readings than the
// bound allows), where a run is shown only at an inner layer of readings, through each kind of reading, and when the caller changes the
// secrets array it passes between two calls.

function unquotePlus(text: string): string {
  return text.replace(/\+/g, " ").replace(/(?:%[0-9A-Fa-f]{2})+/g, match => new TextDecoder("utf-8").decode(Uint8Array.from(match.slice(1).split("%").map(hex => parseInt(hex, 16)))));
}
/** Every reading of an output, to 32 layers: the text, each JSON text (its member names and strings) and JSON string literal in it
 * decoded, its escapes read, and its percent forms decoded, again and again. Placeholders are kept apart as a NUL. */
function views(out: string): string[] {
  const seen = new Set<string>();
  let layer = [out];
  for (let depth = 0; depth < 32 && layer.length && seen.size < 20000; depth++) {
    const next: string[] = [];
    for (const view of layer) {
      if (seen.has(view)) continue;
      seen.add(view);
      const trimmed = view.trim();
      if (/^[[{"]/.test(trimmed)) {
        try {
          const walk = (node: unknown): void => {
            if (typeof node === "string") next.push(node);
            else if (node && typeof node === "object") for (const [key, child] of Object.entries(node)) { next.push(key); walk(child); }
          };
          walk(JSON.parse(trimmed));
        } catch { /* not JSON */ }
      }
      for (const match of view.matchAll(/"(?:[^"\\\n]|\\.)*"/g)) { try { const s = JSON.parse(match[0]); if (typeof s === "string") next.push(s); } catch { /* not a literal */ } }
      if (view.includes("\\")) next.push(view.replace(/\\(u[0-9a-fA-F]{4}|["\\/bfnrt])/g, (_, g: string) => g[0] === "u" ? String.fromCharCode(parseInt(g.slice(1), 16)) : ({ b: "\b", f: "\f", n: "\n", r: "\r", t: "\t" } as Record<string, string>)[g] ?? g));
      if (/%[0-9A-Fa-f]{2}/.test(view)) next.push(unquotePlus(view));
    }
    layer = next;
  }
  return [...seen].map(view => view.split("<redacted>").join("\0"));
}
/** The runs of four code points of the given secrets that some reading of the output shows. */
function shownRuns(out: string, secrets: readonly string[]): string[] {
  const runs = new Set<string>();
  for (const secret of secrets) { const points = Array.from(secret); for (let i = 0; i + 4 <= points.length; i++) runs.add(points.slice(i, i + 4).join("")); }
  const shown = new Set<string>();
  for (const view of views(out)) for (const run of runs) if (view.includes(run)) shown.add(run);
  return [...shown].sort();
}
/** The text with its known secrets replaced on the raw bytes, longest first, as the reference replaces them. */
function rawReplaced(input: string, secrets: readonly string[]): string {
  for (const secret of [...secrets].sort((a, b) => b.length - a.length)) input = input.split(secret).join("<redacted>");
  return input;
}

const A = "LQ64F0FD13";
const nest = (text: string, layers: number) => { for (let k = 0; k < layers; k++) text = JSON.stringify(text); return text; };
/** An object that holds the two secrets in one member and, in a sibling member, a JSON text nested `layers` JSON layers deep with a
 * filler of backslashes and percent forms, so that reading every layer of the decoded read, or of the raw-bytes read, takes more readings
 * than the bound allows; the object is JSON-encoded `encodings` times and stands alone or in prose. */
function boundCase(tail: string, layers: number, width: number, encodings: number, prose: boolean): { input: string; secrets: string[] } {
  let value = A + tail, B = A.slice(4) + tail;
  if (B.length <= A.length) { B += "x".repeat(A.length + 1 - B.length); value += "xxxx"; }
  const sibling = nest('{"q": ' + JSON.stringify("ab%41\\".repeat(Math.floor(width / 8) + 1)) + "}", layers);
  let text = '{"k": ' + JSON.stringify(value) + ', "z": ' + JSON.stringify(sibling) + "}";
  text = nest(text, encodings);
  return { input: prose ? "body: " + text + " retry" : text, secrets: [A, B] };
}

describe("a decoded read or a raw-bytes read whose readings pass their own bound", () => {
  // Tails that JSON encodes with a backslash, so the longer secret is spelled as given only once decoded.
  const cases: [string, string, number, number, number, boolean][] = [];
  for (const tail of ["\\ p", "\\u0041 p"]) for (const layers of [3, 4, 5]) for (const width of [0, 40]) for (const encodings of [1, 2]) for (const prose of [false, true])
    if (layers < 5 || encodings < 2) cases.push([`${JSON.stringify(tail)} ${layers} layers, filler ${width}, ${encodings} encodings${prose ? ", in prose" : ""}`, tail, layers, width, encodings, prose]);
  test.each(cases)("%s: the export is the text with its secrets replaced on the raw bytes", (_label, tail, layers, width, encodings, prose) => {
    const { input, secrets } = boundCase(tail, layers, width, encodings, prose);
    for (const order of [secrets, [...secrets].reverse()]) {
      const out = scrubValue(input, order) as string;
      expect(out).toBe(rawReplaced(input, secrets));
      expect(shownRuns(out, [A])).toEqual([]);
    }
  });
  test("a sibling five JSON layers deep in an object encoded twice: no run of the shorter secret is shown, in either secret order", () => {
    for (const tail of ["\\ p", "\\u0041 p"]) for (const width of [0, 40]) for (const prose of [false, true]) {
      const { input, secrets } = boundCase(tail, 5, width, 2, prose);
      const out = scrubValue(input, secrets) as string;
      expect(out).toContain("<redacted>");
      expect(shownRuns(out, [A])).toEqual([]);
      expect(scrubValue(input, [...secrets].reverse())).toBe(out);
    }
  });
  test("an object whose sibling holds a JSON text four layers deep: no run of the shorter secret", () => {
    const { input, secrets } = boundCase("\\ p", 4, 0, 1, false);
    expect(input.length).toBeGreaterThan(500);
    const out = scrubValue(input, secrets) as string;
    expect(out.startsWith('"{\\"k\\": \\"<redacted>\\\\\\\\ pxxxx\\", \\"z\\": ')).toBe(true);
    expect(out).toBe(rawReplaced(input, secrets));
    expect(scrubText(input, secrets)).toBe(out);
  });
  test("the same object with an escape spelled in the longer secret, alone and in prose: no run of the shorter secret", () => {
    for (const prose of [false, true]) {
      const { input, secrets } = boundCase("\\u0041 p", 4, 0, 1, prose);
      const out = scrubValue(input, secrets) as string;
      expect(out).toBe(rawReplaced(input, secrets));
      expect(out).toContain('<redacted>\\\\\\\\u0041 p');
      expect(shownRuns(out, [A])).toEqual([]);
    }
  });
});

describe("a member value whose percent forms take more readings than the bound allows", () => {
  // The value ends in a percent form encoded k times over ('%2525...41'); each decoding is one more reading.
  const cases: [string, string, number, string][] = [];
  for (const tail of ["\\ p", "\\\\ p", "\" p"]) for (const k of [31, 32, 33, 40]) for (const shape of ["pair", "object", "literal"]) cases.push([`tail ${JSON.stringify(tail)}, ${k} encodings, in ${shape === "object" ? "an" : "a"} ${shape}`, tail, k, shape]);
  test.each(cases)("%s: the export is the text with its secrets replaced on the raw bytes", (_label, tail, k, shape) => {
    const value = A + tail + " %" + "25".repeat(k) + "41", secrets = [A, A.slice(4) + tail];
    const input = ({ pair: 'x "k": ' + JSON.stringify(value) + " y", object: 'body {"k": ' + JSON.stringify(value) + "} end", literal: JSON.stringify('{"k": ' + JSON.stringify(value) + "}") } as Record<string, string>)[shape];
    const out = scrubValue(input, secrets) as string;
    expect(out).toBe(rawReplaced(input, secrets));
    expect(shownRuns(out, [A])).toEqual([]);
  });
  test("the pair 'x \"k\": \"LQ64F0FD13\\\\ p %2525...41\" y' with 31 encodings", () => {
    const input = 'x "k": "LQ64F0FD13\\\\ p %' + "25".repeat(31) + '41" y', secrets = [A, "F0FD13\\ p"];
    const out = scrubValue(input, secrets) as string;
    expect(out).toBe('x "k": "<redacted>\\\\ p %' + "25".repeat(31) + '41" y');
    expect(scrubText(input, secrets)).toBe(out);
  });
});

describe("a run of a known secret shown only at an inner layer of readings", () => {
  // Three overlapping secrets; the export shows only what the reference's own export shows.
  const cases: [string, string, string[], number, string][] = [
    ["an object of a note and a password in a JSON string literal", "\"{\\\"note\\\": \\\"\\\\nsxC77N9988 yk\\\\\\\\Y\\\\\\\\5 r2\\\", \\\"password\\\": \\\"pw123456789\\\"}\"", ["yk\\Y\\5 r2", "xC77N9988 yk", "\nsxC77N9988 y"], 0, "\"{\\\"note\\\": \\\"\\\\ns<redacted>\\\\\\\\Y\\\\\\\\5 r2\\\", \\\"password\\\": \\\"<redacted>\\\"}\""],
    ["an object with escaped astral and euro characters in a literal in prose", "body: \"{\\\"k\\\": \\\"\\\\ud83d\\\\ude00VKf299y5esmYU\\\\\\\\m\\\\u20ac\\\\\\\"3o\\\"}\" retry", ["😀VKf299y5esmY", "299y5esmYU", "mYU\\m€\"3o"], 30, "body: \"{\\\"k\\\": \\\"\\\\ud83d\\\\ude00VKf<redacted>\\\\\\\\m\\\\u20ac\\\\\\\"3o\\\"}\" retry"],
    ["prose with an object of a note and a password, in a JSON string literal", "\"note body: {\\\"note\\\": \\\"lsw\\\\n52bG5w25It3jlF\\\\no/\\\", \\\"password\\\": \\\"pw123456789\\\"} retry end\"", ["25It3jlF", "lsw\n52bG5w25It3", "t3jlF\no/"], 30, "\"note body: {\\\"note\\\": \\\"lsw\\\\n52bG5w<redacted>\\\\no/\\\", \\\"password\\\": \\\"<redacted>\\\"} retry end\""],
    ["an array with a percent sign and escapes in a JSON string literal", "\"[\\\"I7%\\\\ud83d\\\\ude00lxlZVy8sU3LB1\\\\\\\\\\\\u00e98\\\", 1]\"", ["y8sU3LB1\\é8", "xlZVy8sU3LB1", "I7%😀lxlZVy8sU3L"], 10, "\"[\\\"I7%\\\\ud83d\\\\ude00l<redacted>\\\\\\\\\\\\u00e98\\\", 1]\""],
  ];
  test.each(cases)("%s", (_label, input, secrets, depth, expected) => {
    const out = scrubValue(input, secrets, "", depth) as string;
    expect(out).toBe(expected);
    const allowed = new Set(shownRuns(expected, secrets));
    expect(shownRuns(out, secrets).filter(run => !allowed.has(run))).toEqual([]);
  });
});

describe("each kind of reading of the export", () => {
  // The longer secret holds an e-acute, a quote or a backslash, so it is spelled as given only once read. The shorter one is hidden whole.
  const B = "F0FD13é pxx";
  const exact: [string, string, string[], string][] = [
    ["the text itself", 'body: {"k": "LQ64F0FD13\\u00e9 pxx"} retry', [A, B], 'body: {"k": "<redacted>\\u00e9 pxx"} retry'],
    ["the string a JSON string literal encodes", JSON.stringify('{"k": "LQ64F0FD13\\u00e9 pxx"}'), [A, B], JSON.stringify('{"k": "<redacted>\\u00e9 pxx"}')],
    ["a member name of a JSON text", 'note {"LQ64F0FD13\\u00e9 pxx": [1]} end', [A, B], 'note {"<redacted>\\u00e9 pxx": [1]} end'],
    ["a member name of a JSON text in a literal", JSON.stringify('{"LQ64F0FD13\\u00e9 pxx": [1]}'), [A, B], JSON.stringify('{"<redacted>\\u00e9 pxx": [1]}')],
    ["a JSON string literal inside the string a literal encodes", JSON.stringify('say "LQ64F0FD13\\u00e9 pxx" ok'), [A, B], JSON.stringify('say "<redacted>\\u00e9 pxx" ok')],
    ["an escaped quote inside a JSON string literal of a literal", JSON.stringify('say "LQ64F0FD13\\" p" ok'), [A, 'F0FD13" p'], JSON.stringify('say "<redacted>\\" p" ok')],
    ["an escaped quote outside any literal", JSON.stringify('say LQ64F0FD13\\" p ok'), [A, 'F0FD13" p'], JSON.stringify('say <redacted>\\" p ok')],
    ["an escape outside any literal", JSON.stringify("say LQ64F0FD13\\u00e9 pxx ok"), [A, B], JSON.stringify("say <redacted>\\u00e9 pxx ok")],
    ["percent forms in a URL in prose in a literal", JSON.stringify("see https://h.io/?q=LQ64F0FD13%C3%A9+pxx ok"), [A, B], JSON.stringify("see https://h.io/?q=<redacted>%C3%A9+pxx ok")],
    ["a percent form beside a backslash in a pair", 'x "k": "LQ64F0FD13\\\\ p %41" y', [A, "F0FD13\\ p"], 'x "k": "<redacted>\\\\ p %41" y'],
  ];
  test.each(exact)("%s", (_label, input, secrets, expected) => {
    for (const order of [secrets, [...secrets].reverse()]) {
      const out = scrubValue(input, order) as string;
      expect(out).toBe(expected);
      expect(shownRuns(out, [A])).toEqual([]);
    }
  });
  // The export re-spells the JSON text inside the literal; no reading of it shows a run of the shorter secret.
  test.each([
    ["a member name two JSON layers in", JSON.stringify(JSON.stringify({ a: { "LQ64F0FD13\\ p": 1 } })), [A, "F0FD13\\ p"]],
    ["percent forms in a URL in a JSON text in a literal", JSON.stringify('{"u": "https://h.io/?q=LQ64F0FD13%C3%A9+pxx"}'), [A, B]],
  ])("%s", (_label, input, secrets) => {
    const out = scrubValue(input, secrets as string[]) as string;
    expect(out).toContain("<redacted>");
    expect(shownRuns(out, [A])).toEqual([]);
  });
});

describe("a secrets array the caller changes between two calls", () => {
  // The first call reads a text that spells the longer secret B as given, so the runs of the secrets it is given are read then; the second
  // call reads a text where A is hidden whole only where the scrub reads the runs of A from the secrets the array holds now.
  const B = "F0FD13é pxx";
  const first = "note " + JSON.stringify(JSON.stringify({ k: B + " F0FD13é" })) + " end";
  const input = JSON.stringify('{"k": "LQ64F0FD13\\u00e9 pxx"}');
  const expected = JSON.stringify('{"k": "<redacted>\\u00e9 pxx"}');
  const changes: [string, string[], (secrets: string[]) => void][] = [
    ["a secret pushed", [B], secrets => { secrets.push(A); }],
    ["a secret put in at the front", [B], secrets => { secrets.unshift(A); }],
    ["an element set", [B, "PLNTq7Zr4kXw9"], secrets => { secrets[1] = A; }],
    ["a secret spliced in", [B], secrets => { secrets.splice(0, 0, A); }],
    ["the array emptied and filled again", [B], secrets => { secrets.length = 0; secrets.push(A, B); }],
    ["an element replaced by one of the same length", ["PLNTq7Zr4k", B], secrets => { secrets[0] = A; }],
  ];
  test.each(changes)("%s: the second call hides the new secret whole", (_label, given, change) => {
    for (const scrub of [(text: string, secrets: string[]) => scrubValue(text, secrets) as string, (text: string, secrets: string[]) => scrubText(text, secrets)]) {
      for (const warm of [first, input]) {
        const secrets = [...given];
        expect(scrub(warm, secrets)).toContain("<redacted>");
        change(secrets);
        const out = scrub(input, secrets);
        expect(out).toBe(expected);
        expect(out).toBe(scrub(input, [...secrets]));
        expect(shownRuns(out, [A])).toEqual([]);
      }
    }
  });
  test("the same array used again unchanged gives the same export", () => {
    const secrets = [A, B];
    const once = scrubValue(input, secrets);
    expect(scrubValue(input, secrets)).toBe(once);
    expect(once).toBe(expected);
  });
});
