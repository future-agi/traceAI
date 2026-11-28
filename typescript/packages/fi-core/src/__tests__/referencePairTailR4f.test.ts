import { describe, expect, test } from "@jest/globals";
import { privacyFilter } from "../export-privacy";
import { indexPrivacyHints, scrubAttribute, scrubValue } from "../privacy-runtime";

// A known secret that crosses the opener of an unclosed JSON tail, after a credential pair that the whole-text pair read replaces: the
// text holds a quoted value that a pair in prose (pwd=..., token=..., password:...) cuts, then a bracket that is never closed, and the
// secret starts at that bracket (or a few characters before or after it) and ends inside the credential the tail holds. Once the pair
// read has changed the text, the brackets are read again as an unclosed tail; the secret is replaced across the tail's opener, so no
// character run of the secret that the reference hides is readable.
//
// Each test asserts that the output shows no run of four characters of a known secret or of a planted credential marker that the
// reference output does not show (the reference output, from Reference.scrub in spec/auto-instrumentation/generate_privacy_fixtures.py,
// is written out beside it), at depths 0, 29 and 31 as a depth parameter, in nested objects 0 to 31 deep and inside JSON texts up to
// 30 deep, framed and in prose. A budget throw drops the span, so it hides everything; it is accepted only at depth 31 (and in a JSON
// text 30 deep), where the scrubber's own depth budget may run out.
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
  scrubbed(() => scrubValue(leaf, secrets, "message", depth, false, undefined, none, framed), depth >= 31);
const obj = (leaf: string, secrets: string[], depth: number, framed: boolean) =>
  scrubbed(() => scrubValue(nest(depth, leaf), secrets, "message", 0, false, undefined, none, framed), depth >= 31);
const jsonText = (leaf: string, secrets: string[], depth: number, framed: boolean) =>
  scrubbed(() => scrubValue("{\"k\": ".repeat(depth) + JSON.stringify(leaf) + "}".repeat(depth), secrets, "message", 0, false, undefined, none,
    framed), depth >= 30);

// [title, text, secrets, reference output (the same framed and in prose, at every depth here)]
const repros: [string, string, string[], string][] = [
  ["the opener {{ after a pair in prose that cuts a quoted value",
    "\\\" \"auth\":\"MK1\\\" pwd=MK2\"{{\\\"pwd\\\"\"\":TAILV3DA4CBA", ["\"pwd\\\"\"\":TAILV3DA4C"],
    "\\\" \"auth\":\"<redacted>\"{{\\<redacted>BA"],
  ["the opener [{ after a pair in prose that cuts a quoted value",
    "\\\" \"auth\":\"MK1\\\" pwd=MK2\"[{\\\"pwd\\\"\"\":TAILV3DA4CBA", ["\"pwd\\\"\"\":TAILV3DA4C"],
    "\\\" \"auth\":\"<redacted>\"[{\\<redacted>BA"],
];

// The text as one JSON string is a different input from the bare text: the reference output for that string. The reference shows the
// tail's credential there, so these tests guard the runs of the secret that it hides.
const asString: Record<string, string> = {
  [repros[0][0]]: String.raw`"\\\" \"auth\":\"<redacted>\"{{\\\"pwd\\\"\"\":TAILV3DA4CBA"`,
  [repros[1][0]]: String.raw`"\\\" \"auth\":\"<redacted>\"[{\\\"pwd\\\"\"\":TAILV3DA4CBA"`,
};

describe("a known secret that crosses the opener of an unclosed JSON tail after a pair read", () => {
  for (const [title, text, secrets, reference] of repros) {
    for (const framed of sites) {
      for (const depth of [0, 29, 31]) {
        test(`${title}: no run of the secret is readable at depth ${depth} as a parameter (${site(framed)})`, () => {
          hidesAtLeast(param(text, secrets, depth, framed), reference, text, secrets);
        });
      }
      for (const depth of [0, 29, 30, 31]) {
        test(`${title}: no run of the secret is readable in objects nested ${depth} deep (${site(framed)})`, () => {
          hidesAtLeast(obj(text, secrets, depth, framed), reference, text, secrets);
        });
      }
      test(`${title}: no run of the secret is readable inside JSON texts 1 to 30 deep (${site(framed)})`, () => {
        for (let depth = 1; depth <= 30; depth++) hidesAtLeast(jsonText(text, secrets, depth, framed), reference, text, secrets);
      });
      test(`${title}: the text as one JSON string hides the runs of the secret that the reference hides (${site(framed)})`, () => {
        const out = scrubValue(JSON.stringify(text), secrets, "message", 0, false, undefined, none, framed) as string;
        hidesAtLeast(out, asString[title], text, secrets);
      });
    }
  }

  for (const [title, text, secrets, reference] of repros) {
    test(`${title}: through scrubAttribute no run of the secret is readable`, () => {
      hidesAtLeast(scrubAttribute(text, secrets), reference, text, secrets);
    });
    test(`${title}: through the export filter's attributes no run of the secret is readable`, () => {
      const attributes = privacyFilter({}, secrets).attributes({ message: text, "input.value": text, note: text }) as Record<string, unknown>;
      for (const key of ["message", "input.value", "note"]) hidesAtLeast(String(attributes[key]), reference, text, secrets);
    });
  }
});

// Texts from a seeded generator of the same shape: a JSON pair whose quoted value a pair in prose cuts (an equals sign or a colon,
// spaced or not), then an unclosed opener ({, [, {{, [{, {[, [[, {\", [\"), and a secret that starts at the inner opener, up to three
// characters before it or just after it, and ends inside the tail's credential; some cases also hold a second secret over the first
// value. [title, text, secrets, reference output]
const family: [string, string, string[], string][] = [
  ["[{\\\" and a run of quotes before the colon, after an equals pair (seed 1, case 2)",
    "\\\"\"secret\":\"MKCD\\\", api_key=MKBB\"[{\\\"AUTH\"\"\"\"\":TAILVCBF0FBA", ["{\\\"AUTH\"\"\"\"\":TAILVCBF"],
    "\\\"\"secret\":\"<redacted>\"[<redacted>0FBA"],
  ["{\\\" and a spaced colon, with a second secret over the first value (seed 1, case 42)",
    "\" \"auth\":\"MK3B4\\\" token=MKC2F\"{\\\"\"\" : TAILV94135 end", ["\"\"\" : TAILV941", "\"MK3B"],
    "\" \"auth\":\"<redacted>\"{\\<redacted>35 end"],
  ["[\\\"\\\" and a doubled quote before the colon, after a colon pair (seed 1, case 162)",
    "\\\" \"api_key\":\"MK428\\\", password:MK85\"[\\\"\\\"x\"\":TAILV44D1EF\"}", ["\\\"x\"\":TAILV44D", "\":\"MK42"],
    "\\\" \"api_key\":\"<redacted>\"[\\\"<redacted>1EF\"}"],
  ["{[ with the secret starting after the brackets, after a colon pair (seed 1, case 258)",
    "\\\"\"pwd\":\"MKBA56\\\" password:MK26\"{[\\\"api_key\\\"\"\":TAILV42E8B end", ["\\\"api_key\\\"\"\":TAIL"],
    "\\\"\"pwd\":\"<redacted>\"{[<redacted>V42E8B end"],
  ["{{ with the secret starting at the second brace (seed 1, case 332)",
    "\\\" \"token\":\"MKAFA5\\\" token=MK19D\"{{\\\"\"\":TAILVE8A3BA", ["{\\\"\"\":TAILVE8A"],
    "\\\" \"token\":\"<redacted>\"{<redacted>3BA"],
  ["[[ with the secret starting at the second bracket, with a second secret over the first value (seed 1, case 336)",
    "\\\"\"pwd\":\"MK2F\\\", token=MK5B\"[[\\\"api_key\"\"\"\"\":TAILVEBE6CBA", ["[\\\"api_key\"\"\"\"\":TAILVE", "\":\"MK2"],
    "\\\"\"pwd\":\"<redacted>\"[<redacted>BE6CBA"],
  ["{\\\"\" and an escaped quote before the colon, after an equals pair (seed 1, case 338)",
    "\" \"pwd\":\"MKA91\\\", api_key=MK8EC\"{\\\"\"auth\\\"\"\":TAILV781C6AE8]", ["\\\"\"auth\\\"\"\":TAI"],
    "\" \"pwd\":\"<redacted>\"{<redacted>LV781C6AE8]"],
];

describe("a known secret that crosses the opener of an unclosed tail after a pair read, across openers and separators", () => {
  for (const [title, text, secrets, reference] of family) {
    for (const framed of sites) {
      test(`${title}: no run of the secret is readable (${site(framed)})`, () => {
        hidesAtLeast(param(text, secrets, 0, framed), reference, text, secrets);
      });
      test(`${title}: no run of the secret is readable at depth 31 as a parameter (${site(framed)})`, () => {
        hidesAtLeast(param(text, secrets, 31, framed), reference, text, secrets);
      });
      test(`${title}: no run of the secret is readable in objects nested 30 deep (${site(framed)})`, () => {
        hidesAtLeast(obj(text, secrets, 30, framed), reference, text, secrets);
      });
      test(`${title}: no run of the secret is readable inside JSON texts 1 to 30 deep (${site(framed)})`, () => {
        for (let depth = 1; depth <= 30; depth++) hidesAtLeast(jsonText(text, secrets, depth, framed), reference, text, secrets);
      });
    }
  }
});
