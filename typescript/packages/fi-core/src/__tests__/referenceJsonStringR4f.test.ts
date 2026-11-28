import { describe, expect, test } from "@jest/globals";
import { privacyFilter } from "../export-privacy";
import { indexPrivacyHints, scrubAttribute, scrubValue } from "../privacy-runtime";

// Known secrets inside a JSON string literal of the text, where one secret only matches the decoded string: the reference reads the
// literal as raw text and replaces the text's own known secrets there, longest first, so a longer secret that holds a quote or a
// backslash (which the literal holds escaped) does not take part of a shorter one first and leave the rest of it readable.
//
// Each test asserts that the output shows no run of four characters of a known secret or of a planted credential marker that the
// reference output does not show (the reference output, from Reference.scrub in spec/auto-instrumentation/generate_privacy_fixtures.py,
// is written out beside it), for the leaf as one JSON string and as one JSON string inside prose, at depths 0, 29 and 31 as a depth
// parameter and in objects nested 29 and 31 deep, framed and in prose. A budget throw drops the span, so it hides everything; it is
// accepted only at depth 31, where the scrubber's own depth budget may run out.
const none = indexPrivacyHints([]);
const nest = (depth: number, leaf: unknown) => { let value = leaf; for (let i = 0; i < depth; i++) value = { k: value }; return value; };
const leafOf = (value: unknown): unknown => { let out = value; while (out && typeof out === "object" && "k" in (out as object)) out = (out as { k: unknown }).k; return out; };
const sites = [true, false] as const;
const site = (framed: boolean) => framed ? "framed" : "prose";
const MARKER = /TAIL[A-Z0-9]{2,}|MK[0-9A-F]{4,}/g;
const PLACEHOLDER = /<redacted>|<omitted: [^<>]*>/g;
const THROWN = "<thrown>";
/** The text with each placeholder as one character that no secret holds, so no run is read across a placeholder. */
const readable = (text: string) => text.replace(PLACEHOLDER, "\u0000");
const runs = (text: string) => Array.from({ length: Math.max(0, text.length - 3) }, (_, i) => text.slice(i, i + 4));

/** The output shows no four-character run of a known secret or of a credential marker of the input that the reference does not show. */
function hidesAtLeast(out: string, reference: string, input: string, secrets: string[]): void {
  if (out === THROWN) return;
  const shown = readable(out), referenceShown = readable(reference);
  for (const text of [...secrets, ...new Set(input.match(MARKER) ?? [])]) {
    for (const run of runs(text)) if (!referenceShown.includes(run)) expect([text, run, shown.includes(run)]).toEqual([text, run, false]);
  }
}

/** The scrubbed leaf as text; a throw is the THROWN mark where `mayThrow`, and fails the test anywhere else. */
function scrubbed(scrub: () => unknown, mayThrow: boolean): string {
  let out: unknown;
  try { out = scrub(); } catch (error) { if (mayThrow) return THROWN; throw error; }
  const leaf = leafOf(out);
  return typeof leaf === "string" ? leaf : JSON.stringify(leaf);
}
const param = (value: string, secrets: string[], depth: number, framed: boolean) =>
  scrubbed(() => scrubValue(value, secrets, "message", depth, false, undefined, none, framed), depth >= 31);
const obj = (value: string, secrets: string[], depth: number, framed: boolean) =>
  scrubbed(() => scrubValue(nest(depth, value), secrets, "message", 0, false, undefined, none, framed), depth >= 31);

// [title, leaf, secrets, reference output for JSON.stringify(leaf), reference output for 'note ' + JSON.stringify(leaf) + ' end',
// reference output for the leaf itself]
const cases: [string, string, string[], string, string, string][] = [
  ["a longer secret with escaped quotes before a credential that a shorter secret ends",
    "[1,{\\\"\"secret\"\\\":TAILV2E138F54]", ["AILV2E138F54", "{\\\"\"secret\"\\\":TAILV2E1"],
    "\"[1,{\\\\\\\"\\\"secret\\\"\\\\\\\":T<redacted>]\"",
    "note \"[1,{\\\\\\\"\\\"secret\\\"\\\\\\\":T<redacted>]\" end", "[1,<redacted>38F54]"],
  ["a longer secret with escaped quotes around a token name before a credential that a shorter secret ends",
    "[1,{\\\"\"token\"\\\":TAILV9A1B2C3D4]", ["AILV9A1B2C3D4", "{\\\"\"token\"\\\":TAILV9A"],
    "\"[1,{\\\\\\\"\\\"token\\\"\\\\\\\":T<redacted>]\"",
    "note \"[1,{\\\\\\\"\\\"token\\\"\\\\\\\":T<redacted>]\" end", "[1,<redacted>1B2C3D4]"],
  ["two overlapping secrets, the later one ending in two quotes",
    "[authTAILEB35EBauth\"\"", ["uthTAILEB3", "LEB35EBauth\"\""],
    "\"[a<redacted>5EBauth\\\"\\\"\"",
    "note \"[a<redacted>5EBauth\\\"\\\"\" end", "[authTAI<redacted>"],
  ["overlapping secrets around line breaks, one ending in two quotes",
    "\"\",MKFFE3pwd=\nauth]\nTAIL4CA0FDtoken1,\"\"", ["4CA0FDtoken1,\"\"", "L4CA0FDtoke", "wd=\nauth]\n"],
    "\"\\\"\\\",MKFFE3pwd=\\nauth]\\nTAI<redacted>n1,\\\"\\\"\"",
    "note \"\\\"\\\",MKFFE3pwd=\\nauth]\\nTAI<redacted>n1,\\\"\\\"\" end", "\"\",MKFFE3p<redacted>TAIL<redacted>"],
];

describe("known secrets inside a JSON string literal of the text, read in the text's own order", () => {
  for (const [title, leaf, secrets, asString, inProse] of cases) {
    const forms: [string, string, string][] = [["as one JSON string", JSON.stringify(leaf), asString], ["as one JSON string in prose", "note " + JSON.stringify(leaf) + " end", inProse]];
    for (const [form, value, reference] of forms) {
      for (const framed of sites) {
        for (const depth of [0, 29, 31]) {
          test(`${title}, ${form}: no run of a secret the reference hides is readable at depth ${depth} as a parameter (${site(framed)})`, () => {
            hidesAtLeast(param(value, secrets, depth, framed), reference, value, secrets);
          });
        }
        for (const depth of [29, 31]) {
          test(`${title}, ${form}: no run of a secret the reference hides is readable in objects nested ${depth} deep (${site(framed)})`, () => {
            hidesAtLeast(obj(value, secrets, depth, framed), reference, value, secrets);
          });
        }
      }
      test(`${title}, ${form}: through the export filter's attributes no run of a secret the reference hides is readable`, () => {
        const attributes = privacyFilter({}, secrets).attributes({ message: value, "input.value": value, note: value }) as Record<string, unknown>;
        for (const key of ["message", "input.value", "note"]) hidesAtLeast(String(attributes[key]), reference, value, secrets);
      });
    }
  }

  // scrubAttribute reads an attribute that is one JSON document as that document, so the JSON string is read as the leaf it encodes, or,
  // where that read shows a run of a known secret that the read on its escaped bytes hides, as the reference reads the JSON string itself.
  // Its output shows no run that both the reference output for the leaf and the reference output for the JSON string hide; the string
  // in prose is read as text.
  for (const [title, leaf, secrets, asString, inProse, bare] of cases) {
    test(`${title}, as one JSON string: through scrubAttribute no run of a secret the reference hides both in the leaf and in the JSON string is readable`, () => {
      hidesAtLeast(JSON.parse(scrubAttribute(JSON.stringify(leaf), secrets)), bare + "\u0000" + JSON.parse(asString), leaf, secrets);
    });
    test(`${title}, as one JSON string in prose: through scrubAttribute no run of a secret the reference hides is readable`, () => {
      const value = "note " + JSON.stringify(leaf) + " end";
      hidesAtLeast(scrubAttribute(value, secrets), inProse, value, secrets);
    });
  }

  // The two overlapping secrets, the later one ending in two quotes, are replaced as the reference replaces them.
  const [title, leaf, secrets, asString, inProse] = cases[2];
  for (const framed of sites) {
    test(`${title}: the JSON string is returned as the reference returns it (${site(framed)})`, () => {
      expect(param(JSON.stringify(leaf), secrets, 0, framed)).toBe(asString);
      expect(param("note " + JSON.stringify(leaf) + " end", secrets, 0, framed)).toBe(inProse);
    });
  }

  // The bare leaf is a different input from the JSON string: the reference reads it with the longer secret matching first and shows
  // the end of the shorter one ("[1,<redacted>38F54]"). The read with the pair rule's spans joined hides the whole text, and it hides
  // every character the reference's order hides, so it is the one exported.
  test("the bare leaf with a longer secret holding quotes hides at least what the reference hides", () => {
    expect(param(cases[0][1], cases[0][2], 0, true)).toBe("<omitted: incomplete JSON>");
    expect(param(cases[0][1], cases[0][2], 0, false)).toBe("<omitted: incomplete JSON>");
  });
});
