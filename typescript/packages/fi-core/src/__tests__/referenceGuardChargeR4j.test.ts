import { describe, expect, test } from "@jest/globals";
import { scrubText, scrubValue, type PrivacyBudget } from "../privacy-runtime";

// A JSON object or array in prose, or a name:value pair in prose, whose raw bytes hold a known secret is read two ways: leaf by leaf, and on
// its raw bytes with the known secrets replaced there. Where a string inside it is a JSON text nested a few layers deep, the readings of the
// leaf read are too many to weigh, and the raw-bytes read is exported. That read is charged to the call's node and text budget as any read
// of the object is: the charge is the same as for the same object with no known secret in it, so a text of many such objects meets the
// node budget at the same count of objects whether or not they hold a known secret.

const S = "MRKv8Qz3Hh2Lp"; // planted marker, the known secret
const C = "plainvalue123"; // a plain value of the same length, no secret
const nest = (s: string, n: number): string => { for (let i = 0; i < n; i++) s = JSON.stringify(s); return s; };
const deepShort = nest(JSON.stringify({ y: "cd".repeat(300), z: [1, 2, 3] }), 3);
const deepLong = nest(JSON.stringify({ y: "cd".repeat(2000), z: [1, 2, 3] }), 3);
const spaced = (n: number): string => nest(JSON.stringify({ x: "ab".repeat(400) }), n);

const fresh = (): PrivacyBudget => ({ nodes: 16384, text: 4194304, seen: new WeakSet() });
/** The output and the nodes and text characters one scrubValue call charges to a fresh budget. */
function charged(input: string): { out: unknown; nodes: number; text: number } {
  const budget = fresh();
  const out = scrubValue(input, [S], "", 0, false, budget);
  return { out, nodes: 16384 - budget.nodes, text: 4194304 - budget.text };
}
/** The four-character runs of the known secret the output shows as it is, or in any JSON string it holds, decoded again and again. */
function shown(out: string): string[] {
  const seen = new Set<string>();
  let layer = [out];
  for (let depth = 0; depth < 8 && layer.length; depth++) {
    const next: string[] = [];
    for (const view of layer) {
      if (seen.has(view)) continue;
      seen.add(view);
      for (const match of view.matchAll(/"(?:[^"\\\n]|\\.)*"/g)) { try { const s = JSON.parse(match[0]); if (typeof s === "string") next.push(s); } catch { /* not a literal */ } }
    }
    layer = next;
  }
  const runs: string[] = [];
  for (let i = 0; i + 4 <= S.length; i++) if ([...seen].some(view => view.split("<redacted>").join("\0").includes(S.slice(i, i + 4)))) runs.push(S.slice(i, i + 4));
  return runs;
}

// One object (or pair) in prose; `m` is the marker or the plain value. The nodes and text characters are those f0d94678 charges with the
// marker in place; the exported text is the Reference's (d2dab4e8): the input with the marker replaced.
const single: [string, (m: string) => string, number, number][] = [
  ["an object with a string nested three JSON layers deep", m => `pre ${JSON.stringify({ k: m, v: deepShort })} post`, 13, 3957],
  ["an array with a string nested three JSON layers deep", m => `pre ${JSON.stringify([m, deepShort, [4, 5, 6]])} post`, 17, 3955],
  ["a name:value pair whose object value holds 200 zeros and a string nested three JSON layers deep", m => `pre "k": ${JSON.stringify({ s: m, a: Array(200).fill(0), v: deepShort })} post`, 214, 4369],
  ["an object with 1000 zeros and a string nested three JSON layers deep", m => `pre ${JSON.stringify({ k: m, a: Array(1000).fill(0), v: deepLong })} post`, 1014, 26364],
  ["a name:value pair whose object value holds 1000 zeros and a string nested three JSON layers deep", m => `pre "k": ${JSON.stringify({ s: m, a: Array(1000).fill(0), v: deepLong })} post`, 1014, 26369],
  ["a spaced object with a string nested three JSON layers deep", m => `note {"k": "${m}", "v": ${JSON.stringify(spaced(3))}} end`, 9, 5047],
  ["a spaced object with a string nested four JSON layers deep", m => `note {"k": "${m}", "v": ${JSON.stringify(spaced(4))}} end`, 10, 6041],
];

describe("an object or pair in prose whose leaf read has too many readings to weigh is charged for the read it exports", () => {
  test.each(single)("%s: the nodes and text characters charged are those of the read that is exported", (_label, make, nodes, text) => {
    const r = charged(make(S));
    expect(r.out).toBe(make(S).split(S).join("<redacted>"));
    expect(shown(r.out as string)).toEqual([]);
    expect({ nodes: r.nodes, text: r.text }).toEqual({ nodes, text });
  });
  test.each(single)("%s: the charge is the same with the known secret as with a plain value in its place", (_label, make) => {
    const withSecret = charged(make(S)), control = charged(make(C));
    expect(control.out).toBe(make(C));
    expect(withSecret.nodes).toBe(control.nodes);
    // The exported text is shorter by what the placeholder saves on the secret, and so is the text charged for it.
    expect(withSecret.text).toBe(control.text - (S.length - "<redacted>".length));
  });
});

// k copies in prose of an object (or pair) of 1000 zeros and a string nested three JSON layers deep: about 1014 nodes each, so 16 fit in
// the node budget of 16384 and 17 do not (f0d94678 throws 'Privacy structure budget' from 17 copies, with the marker and without it,
// through scrubValue and scrubText alike).
const objects = (m: string, k: number): string => "pre " + `${JSON.stringify({ k: m, a: Array(1000).fill(0), v: deepLong })} and `.repeat(k) + "post";
const pairs = (m: string, k: number): string => "pre " + Array(k).fill(`"k": ${JSON.stringify({ s: m, a: Array(1000).fill(0), v: deepLong })}`).join(" and ") + " post";
const budgetError = /^Privacy (structure|text) budget$/;
const counts: [string, (m: string, k: number) => string][] = [["objects", objects], ["name:value pairs", pairs]];

describe("a text of many such objects or pairs meets the node budget at the same count whether or not they hold a known secret", () => {
  test.each(counts)("16 %s in prose are exported with the known secret replaced", (_label, make) => {
    const input = make(S, 16);
    expect(scrubValue(input, [S])).toBe(input.split(S).join("<redacted>"));
  });
  test.each(counts)("17 %s in prose pass the node budget, with a known secret in each", (_label, make) => {
    expect(() => scrubValue(make(S, 17), [S])).toThrow(budgetError);
  });
  test("17 objects in prose read as free text pass the node budget, with a known secret in each", () => {
    expect(() => scrubText(objects(S, 17), [S])).toThrow(budgetError);
  });
  test.each(counts)("17 %s in prose pass the node budget, with a plain value in place of the secret", (_label, make) => {
    expect(() => scrubValue(make(C, 17), [S])).toThrow(budgetError);
  });
  test("20 objects in prose, about 20000 JSON nodes, pass the node budget, with a known secret in each", () => {
    expect(() => scrubValue(objects(S, 20), [S])).toThrow(budgetError);
  });
});
