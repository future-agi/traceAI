import { describe, expect, test } from "@jest/globals";
import { privacyFilter } from "../export-privacy";
import { indexPrivacyHints, scrubValue } from "../privacy-runtime";

// Overlapping known secrets that hold a quote: the reference replaces the text's known secrets longest first, one after the other, so
// the longer secret takes the quote that a JSON string of the text opens or closes with, and the shorter secret that overlaps it is no
// longer there. The quote must not be left beside the placeholder, where it reads, with what follows or precedes it, as a run of the
// shorter secret. In a JSON text, a secret that takes the opening quote of a string is read on the text as the reference reads it once
// the leaves are replaced, wherever else the decoded string holds the same spelling.
//
// Each test asserts that the output shows no run of four characters of a known secret or of a planted credential marker that the
// reference output does not show (the reference output, from Reference.scrub in spec/auto-instrumentation/generate_privacy_fixtures.py,
// is written out beside each case), for the leaf as a value at depths 0, 29 and 31 as a depth parameter and in objects nested 29 and 31
// deep, and for the leaf in a JSON text nested 1, 28 and 31 deep, framed and in prose, and through the export filter. A budget throw
// drops the span, so it hides everything; it is accepted only at depth 31, where the scrubber's own depth budget may run out.
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

/** A JSON text read as the leaf it nests, or as it stands when it does not parse. */
function jsonLeaf(text: string): string {
  let parsed: unknown;
  try { parsed = JSON.parse(text); } catch { return text; }
  const leaf = leafOf(parsed);
  return typeof leaf === "string" ? leaf : JSON.stringify(leaf);
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

// [title, leaf, secrets, reference output for the leaf (the same at every depth and site), reference output for the leaf's part of the
// JSON text when the reference does not return that text as JSON (the text is then '{"k":' d times, this, and '}' d times)]
const cases: [string, string, string[], string, string?][] = [
  ["a longer secret that ends with the closing quote of a string, a shorter one from its last character over the quote",
    "\"TAILV2C0F78\"=\"MKD212F5C2\"}[\"TAILV9A492B441\"]{wd: :}}\\\"pwd\\\"[\"MK974C\"]", ["74C\"]", "8\"=\"MKD212", "TAILV2C0F78\""],
    "\"<redacted>=\"MKD212F5C2\"}[\"TAILV9A492B441\"]{wd: :}}\\\"pwd\\\"[\"MK974C\"]"],
  ["a longer secret that starts with the opening quote of a string, a shorter one over a name and that quote",
    "{{'access_token'\"TAILV7DB88243D\"}1: a b,[\"TAILV0C7A9\"]{\\\":[{\"pwd\"\":\"TAILVB5A7BD8\"}", ["s_token'\"T", "1: a b,[\"TA", "\"TAILV7DB88243D"],
    "{{'access_token'<redacted>\"}<redacted>ILV0C7A9\"]{\\\":[{\"pwd\"\":\"TAILVB5A7BD8\"}"],
  ["a longer secret that ends with the closing quote of a string, a shorter one over that quote and an escaped quote after it",
    " end,\"MK7E33DF7AC\"}\\\"", ["7AC\"}\\\"", "E33DF7AC\"", "C\"}\\\""],
    " end,\"MK7<redacted>}\\\""],
  ["a longer secret that starts with the opening quote of a string, a shorter one from a marker over that quote",
    "\"TAILV711D\"MK4B2F91\"TAILVCE97681F72\" note note MKCB61E6931\"]\":\"{Authorizationa b,", ["B2F91\"TAI", "\"TAILVCE9"],
    "\"TAILV711D\"MK4B2F91<redacted>7681F72\" note note MKCB61E6931\"]\":\"{Authorizationa b,"],
  ["a longer secret that ends with the closing quote of a string after a name, a shorter one over the name and that quote",
    "                       ''api_key'\"MKBED67C1B\"}[\"TAILV584AF376\"]", ["key'\"MKBED6", "\"MKBED67C1B"],
    "                       ''api_key'<redacted>\"}[\"TAILV584AF376\"]"],
  ["a secret that takes the opening quote of a string in a JSON text, whose decoded string holds the same spelling further on",
    "api_key\"MKB5FDCA1F\"}\\\"api_key\\\"{access_token hello world ,MK2E56\"x\"", ["\"api_key\\", "FDCA1F\"}\\\"api_key\\\""],
    "api_key\"MKB5<redacted>{access_token hello world ,MK2E56\"x\"",
    "<redacted>\"MKB5<redacted>{access_token hello world ,MK2E56\\\"x\\\"\""],
];

describe("overlapping known secrets that hold a quote, replaced longest first", () => {
  for (const [title, leaf, secrets, reference, rawJson] of cases) {
    for (const framed of sites) {
      for (const depth of [0, 29, 31]) {
        test(`${title}: no run of a secret the reference hides is readable at depth ${depth} as a parameter (${site(framed)})`, () => {
          hidesAtLeast(param(leaf, secrets, depth, framed), reference, leaf, secrets);
        });
      }
      for (const depth of [29, 31]) {
        test(`${title}: no run of a secret the reference hides is readable in objects nested ${depth} deep (${site(framed)})`, () => {
          hidesAtLeast(obj(leaf, secrets, depth, framed), reference, leaf, secrets);
        });
      }
      for (const depth of [1, 28, 31]) {
        test(`${title}: no run of a secret the reference hides is readable in a JSON text nested ${depth} deep (${site(framed)})`, () => {
          const value = JSON.stringify(nest(depth, leaf));
          const text = rawJson === undefined ? JSON.stringify(nest(depth, reference)) : "{\"k\":".repeat(depth) + rawJson + "}".repeat(depth);
          const out = param(value, secrets, 0, framed);
          hidesAtLeast(out, text, value, secrets);
          hidesAtLeast(out === THROWN ? out : jsonLeaf(out), jsonLeaf(text), leaf, secrets);
        });
      }
    }
    // The export filter keeps only the configured secrets of eight characters or more, as the reference reads only those: a shorter one
    // is not a secret there, so only the runs of the longer ones are read.
    test(`${title}: through the export filter's attributes no run of a secret the reference hides is readable`, () => {
      const attributes = privacyFilter({}, secrets).attributes({ message: leaf, "input.value": leaf, note: leaf }) as Record<string, unknown>;
      for (const key of ["message", "input.value", "note"]) hidesAtLeast(String(attributes[key]), reference, leaf, secrets.filter(secret => secret.length >= 8));
    });
  }
});
