import { describe, expect, test } from "@jest/globals";
import { indexPrivacyHints, scrubValue } from "../privacy-runtime";

// A known secret that crosses the opener of an unclosed JSON tail takes the tail out with it, to the end of the text, as one
// placeholder. A second known secret that lies inside that tail is inside what the first one took out: it adds no placeholder of its
// own and gives back none of the tail. The edits stay sorted and apart, so no part of the tail after the second secret is read again.
//
// Each test asserts the scrubbed leaf, and that it shows no run of four characters of a known secret or of a planted credential marker
// that the reference output does not show (the reference output is written out beside it; it is the same framed and in prose, at every
// depth here). A budget throw drops the span, so it hides everything; it is accepted only at depth 31 (and in a JSON text 30 deep).
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
  if (typeof out === "string" && out.startsWith("{")) { try { out = JSON.parse(out); } catch { /* not a JSON text: read it as it is */ } }
  const leaf = leafOf(out);
  return typeof leaf === "string" ? leaf : JSON.stringify(leaf);
}
const param = (leaf: string, secrets: string[], depth: number, framed: boolean) =>
  scrubbed(() => scrubValue(leaf, secrets, "", depth, false, undefined, none, framed), depth >= 31);
const obj = (leaf: string, secrets: string[], depth: number, framed: boolean) =>
  scrubbed(() => scrubValue(nest(depth, leaf), secrets, "", 0, false, undefined, none, framed), depth >= 31);
const jsonText = (leaf: string, secrets: string[], depth: number, framed: boolean) =>
  scrubbed(() => scrubValue("{\"k\": ".repeat(depth) + JSON.stringify(leaf) + "}".repeat(depth), secrets, "", 0, false, undefined, none, framed), depth >= 30);

// [title, text, secrets, scrubbed leaf, reference output]
const cases: [string, string, string[], string, string][] = [
  ["a secret that ends with the opener of an unclosed object, then a second secret inside the object's string before a credential",
    "ab SECRET1{{\"k\": \"SECRET22 TAILVABC123\"", ["SECRET1{", "SECRET22"],
    "ab <redacted>", "ab <redacted>{\"k\": \"<redacted> TAILVABC123\""],
  ["a secret that ends with the opener of an unclosed array inside an object, then a second secret inside a string of the array",
    "ab SECRET1{[\"x SECRET22 TAILVABC123\", 1", ["SECRET1{", "SECRET22"],
    "ab <redacted>", "ab <redacted>[\"x <redacted> TAILVABC123\", 1"],
  ["a secret that ends with the opener of unclosed brackets that hold prose, then a second secret in that prose",
    "ab SECRET1{{ x SECRET22 TAILVABC123", ["SECRET1{", "SECRET22"],
    "ab <redacted>", "ab <redacted>{ x <redacted> TAILVABC123"],
  ["a secret that runs from prose across the openers of unclosed brackets, then a second secret that cuts a bearer name in them",
    "xa b,'{{ x  'MK0EBB' endBearer \"apikey\" TAILVABC123", [" b,'{{ x  'MK0", "ndBearer"],
    "xa<redacted>", "xa<redacted>EBB' e<redacted> \"apikey\" TAILVABC123"],
];

/** The leaf is the expected one (or the span was dropped), and hides at least what the reference hides. */
function pins(out: string, expected: string, reference: string, text: string, secrets: string[]): void {
  if (out !== THROWN) expect(out).toBe(expected);
  hidesAtLeast(out, reference, text, secrets);
}

describe("a second known secret inside an unclosed JSON tail that a first secret takes out", () => {
  for (const [title, text, secrets, expected, reference] of cases) {
    for (const framed of sites) {
      for (const depth of [0, 29, 31]) {
        test(`${title}, ${site(framed)}, depth parameter ${depth}`, () => pins(param(text, secrets, depth, framed), expected, reference, text, secrets));
      }
      for (const depth of [1, 29, 30, 31]) {
        test(`${title}, ${site(framed)}, ${depth} objects deep`, () => pins(obj(text, secrets, depth, framed), expected, reference, text, secrets));
      }
      for (const depth of [1, 29, 30]) {
        test(`${title}, ${site(framed)}, in a JSON text ${depth} deep`, () => pins(jsonText(text, secrets, depth, framed), expected, reference, text, secrets));
      }
    }
  }
});
