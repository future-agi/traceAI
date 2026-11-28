import { describe, expect, test } from "@jest/globals";
import { indexPrivacyHints, scrubValue } from "../privacy-runtime";

// Texts that the reference (spec/auto-instrumentation/generate_privacy_fixtures.py) exports at every depth listed here, without a
// structure budget error. The reference charges one node per value it walks and one depth level per container of a value that is one
// JSON document as a whole; a text that is not one JSON document is read as text, whatever brackets it holds or a known secret leaves
// in it. Each text is read three ways: as a value at a given depth (the depth argument), as the leaf of nested objects, and as the leaf
// of a JSON text of nested objects. Each expected value below is the output of the reference, or, where the export redacts more than
// the reference, the export's own output at depth 0; every character the reference hides stays hidden.
const none = indexPrivacyHints([]);
type Mode = "param" | "obj" | "json";
const nest = (depth: number, leaf: unknown) => { let value = leaf; for (let i = 0; i < depth; i++) value = { k: value }; return value; };
const unnest = (depth: number, value: unknown) => { for (let i = 0; i < depth; i++) value = (value as { k: unknown }).k; return value; };
/** Scrubs `leaf` read one of three ways at `depth` and gives back the scrubbed leaf. */
function scrubLeaf(leaf: string, secrets: string[], mode: Mode, depth: number, framed: boolean): unknown {
  if (mode === "param") return scrubValue(leaf, secrets, "", depth, false, undefined, none, framed);
  if (mode === "obj") return unnest(depth, scrubValue(nest(depth, leaf), secrets, "", 0, false, undefined, none, framed));
  const out = scrubValue("{\"k\": ".repeat(depth) + JSON.stringify(leaf) + "}".repeat(depth), secrets, "", 0, false, undefined, none, framed);
  return unnest(depth, JSON.parse(out as string));
}
/** Every marker that the reference's output does not show is not shown by the export either. */
function hidesWhatTheReferenceHides(out: unknown, reference: string, markers: string[]): void {
  expect(typeof out).toBe("string");
  for (const marker of markers) if (!reference.includes(marker)) expect(out as string).not.toContain(marker);
}
const label = (mode: Mode, depth: number, framed: boolean) =>
  `${mode === "param" ? "a value at depth" : mode === "obj" ? "the leaf of objects nested" : "the leaf of a JSON text nested"} ${depth}, ${framed ? "framed" : "prose"}`;

// F1: a secret that runs from inside a quoted credential name over its '=' and the line break. With the secret replaced the text
// reads as one JSON array, but the value is not one JSON document, so the reference reads it as text at the value's own depth.
describe("a text that reads as one JSON array once a known secret is replaced", () => {
  const leaf = "[\"Authorization\"=\nTAILVB47C61\"]", secrets = ["thorization\"=\n"];
  const reference = "[\"Au<redacted>TAILVB47C61\"]";
  const red: [Mode, number][] = [["param", 31], ["param", 32], ["obj", 31], ["obj", 32], ["json", 30], ["json", 31]];
  const guard: [Mode, number][] = [["param", 0], ["param", 28], ["param", 29], ["param", 30], ["obj", 0], ["obj", 28], ["obj", 29], ["obj", 30], ["json", 28], ["json", 29]];
  for (const framed of [true, false]) {
    for (const [mode, depth] of red) {
      test(`is exported as the reference exports it at the last depths the reference reads (${label(mode, depth, framed)})`, () => {
        const out = scrubLeaf(leaf, secrets, mode, depth, framed);
        hidesWhatTheReferenceHides(out, reference, [secrets[0], "Authorization"]);
        expect(out).toBe(reference);
      });
    }
  }
  describe("guards: depths below the last ones", () => {
    for (const framed of [true, false]) {
      for (const [mode, depth] of guard) {
        test(`is exported as the reference exports it (${label(mode, depth, framed)})`, () => {
          expect(scrubLeaf(leaf, secrets, mode, depth, framed)).toBe(reference);
        });
      }
    }
  });
});

// F2: a known secret that crosses a JSON object takes it out whole; brackets that close no JSON follow after a gap of line breaks.
// The reference reads all of it as text at the value's own depth.
describe("brackets that close no JSON after a gap, past a JSON object that a known secret crosses", () => {
  const leaf = (gap: number) => "{\"wd\": [\"MK64EDB\"]}} " + "\n".repeat(gap) + "\"secret:M\",x:\"[\"{{{}";
  const reference = (gap: number) => "{\"<redacted>\"]}} " + "\n".repeat(gap) + "\"secret:<redacted>,x:\"[\"{{{}";
  // The export at depth 0, which takes the whole crossed object and the whole quoted "secret:M" pair out.
  const shallow = (gap: number) => "\"<redacted>\"} " + "\n".repeat(gap) + "\"<redacted>\",x:\"[\"{{{}";
  const secrets = ["wd\": [\"MK64EDB"];
  const markers = ["MK64EDB", secrets[0], "secret:M", "\"M\""];
  const check = (gap: number, mode: Mode, depth: number, framed: boolean) => {
    const out = scrubLeaf(leaf(gap), secrets, mode, depth, framed);
    hidesWhatTheReferenceHides(out, reference(gap), markers);
    expect([reference(gap), shallow(gap)]).toContain(out);
  };
  const cells: [Mode, number][] = [["param", 0], ["param", 28], ["param", 29], ["param", 30], ["param", 31], ["obj", 0], ["obj", 28], ["obj", 29], ["obj", 30], ["obj", 31], ["json", 28], ["json", 29], ["json", 30], ["json", 31]];
  const deepest = (mode: Mode, depth: number) => (mode === "json" ? depth === 29 : depth === 30);
  for (const gap of [1024, 1030, 2100, 5000]) {
    for (const framed of [true, false]) {
      for (const [mode, depth] of cells.filter(([m, d]) => deepest(m, d))) {
        test(`is exported with a ${gap}-character gap one level before the brackets reach the depth limit (${label(mode, depth, framed)})`, () => check(gap, mode, depth, framed));
      }
    }
  }
  describe("a 1000-character gap, which 9e15d8e9 also refuses at these depths", () => {
    for (const framed of [true, false]) {
      for (const [mode, depth] of cells.filter(([m, d]) => deepest(m, d))) {
        test(`is exported one level before the brackets reach the depth limit (${label(mode, depth, framed)})`, () => check(1000, mode, depth, framed));
      }
    }
  });
  describe("guards: the other depths of the gap sweep", () => {
    for (const gap of [1000, 1024, 1030, 2100, 5000]) {
      for (const framed of [true, false]) {
        for (const [mode, depth] of cells.filter(([m, d]) => !deepest(m, d))) {
          test(`is exported with a ${gap}-character gap (${label(mode, depth, framed)})`, () => check(gap, mode, depth, framed));
        }
      }
    }
  });
});

// L2-1: one known secret crosses the JSON object at the start; thousands of credential pairs follow, each with a quoted value. The
// reference charges the text as one node, however many pairs it reads in it.
describe("thousands of credential pairs after a JSON object that a known secret crosses", () => {
  const value = (n: number) => "abcdefg {\"k\":\"v\"}" + Array.from({ length: n }, (_, i) => " pwd=ab\"TAILV" + i + "\"").join("");
  const secrets = ["defg {\"k"];
  // The reference's output keeps '":"v"}' after the secret's placeholder; the export takes the crossed object out whole.
  const check = (n: number, framed: boolean) => {
    const out = scrubValue(value(n), secrets, "", 0, false, undefined, none, framed);
    expect(typeof out).toBe("string");
    expect(out).not.toContain("TAILV");
    expect(out).not.toContain("defg");
    expect([`abc<redacted>":"v"}`, "abc<redacted>"].map(head => head + " pwd=<redacted>".repeat(n))).toContain(out);
  };
  for (const n of [8191, 9000]) {
    for (const framed of [true, false]) {
      test(`takes every pair's value out with ${n} pairs (${framed ? "framed" : "prose"})`, () => check(n, framed), 20_000);
    }
  }
  describe("guards: fewer pairs", () => {
    for (const framed of [true, false]) {
      test(`takes every pair's value out with 8190 pairs (${framed ? "framed" : "prose"})`, () => check(8190, framed), 20_000);
    }
  });
});
