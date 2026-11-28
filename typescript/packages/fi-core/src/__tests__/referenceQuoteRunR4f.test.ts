import { describe, expect, test } from "@jest/globals";
import { indexPrivacyHints, scrubValue } from "../privacy-runtime";

// Known secrets that take a quote of a JSON string in a text: where the string's owner is taken out to a bare placeholder, the quote
// goes with it, as the reference replaces the secrets on the text itself, so the quote is not left beside the placeholder where it reads,
// with what follows or precedes it, as a run of a known secret. A run of fewer than four characters between a secret and the quote is
// read as such a run only where the string is nested in another one.
//
// Each test asserts that the output shows no run of four characters of a known secret or of a planted credential marker that the
// reference output does not show (the reference output, from Reference.scrub in spec/auto-instrumentation/generate_privacy_fixtures.py,
// is written out beside each case, the same at every depth and site), for the leaf as a value at depths 0 and 29 as a depth parameter
// and in objects nested 0 and 31 deep, framed and in prose. A budget throw drops the span, so it hides everything; it is accepted only
// at depth 31, where the scrubber's own depth budget may run out.
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

function check(cases: [string, string, string[], string][]): void {
  for (const [title, leaf, secrets, reference] of cases) {
    for (const framed of sites) {
      for (const depth of [0, 29]) {
        test(`${title}: no run of a secret the reference hides is readable at depth ${depth} as a parameter (${site(framed)})`, () => {
          hidesAtLeast(param(leaf, secrets, depth, framed), reference, leaf, secrets);
        });
      }
      for (const depth of [0, 31]) {
        test(`${title}: no run of a secret the reference hides is readable in objects nested ${depth} deep (${site(framed)})`, () => {
          hidesAtLeast(obj(leaf, secrets, depth, framed), reference, leaf, secrets);
        });
      }
    }
  }
}

// [title, leaf, secrets, reference output for the leaf]
describe("a secret that takes the opening quote of a string beside a short run of another secret", () => {
  check([
    ["a secret over a name and the opening quote of a URL string, a longer one from that quote over the URL",
      "{x1\"http://h/p?a= \\n\\\\nsecret\\\"{\\\"a\\\":\\\"\"",
      ["secret\\\\\\\"{\\\\\\\"a\\\\\\\":\\\\\\\"", "\\\\\"a\\\\\\\":\\\\\\\"\\\"", "\"http://h/p?a", "{x1\"ht"],
      "{x1<redacted>= \\n\\\\nsecret\\\"{\\\"a\\\":\\\"\""],
  ]);
});

// A later secret of the same string owner that overlaps the first one's placeholder: the owner is read as a whole for the quote run once
// the later secret is merged into it, so a quote the merged secrets take goes with the placeholder.
describe("secrets merged into one string owner, the later one taking a quote of the string", () => {
  check([
    ["a secret inside a string, a second one up to its closing quote and a third one from inside the second over that quote",
      "k \"a\\nBCDEFGHI PQRSTUVWXYZ\" QRS1 end",
      ["nBCDEFGHI", "PQRSTUVWXYZ\"", "XYZ\" QRS1"],
      "k \"a\\<redacted> <redacted> QRS1 end"],
    ["secrets over escaped backslashes and a marker in a string, a later one over an escaped quote and the closing quote",
      "apikey\"\\\\MK45498137TAILVA53C506F0F\\\\\\\"tokenBearer \\\\\\\\\"}MK",
      ["\\\\\\\\\"tokenBearer \\\\\\\\\\\\\\\\\\", "\\MK45498", "\\\\\"}MK", "enBearer \\\\\\\\\""],
      "apikey\"\\<redacted>137TAILVA53C506F0F\\\\\\\"tok<redacted>}MK"],
    ["overlapping secrets over an escaped quote and a marker, a later one over the closing quote and the quotes after it",
      "\" \\\\TAILV\\\"]Bearer TAILV56A172\\\"\"\",:}\"\"[",
      ["\\\"]Bearer ", "r TAILV56", "\\\"\"\",:}\"\"[", "LV56A172\\\"\""],
      "\" \\\\TAILV<redacted>TAI<redacted>\",:}\"\"["],
    ["a secret over the opening quote of a string, a later one over a name and the closing quote of the string",
      "\"token\\\\\\\\=\\nMKDC4EA5FFTAILV::pwd='\"\\u0041apikey",
      ["\"token\\\\", "pwd='\"\\u0041a", "FFTAILV::pwd='\""],
      "<redacted>\\\\=\\nMKDC4EA5<redacted>\\u0041apikey"],
  ]);
});
