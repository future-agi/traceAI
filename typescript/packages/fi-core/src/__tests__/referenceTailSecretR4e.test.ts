import { describe, expect, test } from "@jest/globals";
import { privacyFilter } from "../export-privacy";
import { indexPrivacyHints, scrubAttribute, scrubValue } from "../privacy-runtime";

// A known secret that crosses the opener of an unclosed JSON tail: the text holds a pair whose name a first secret cuts, then a bracket
// that is never closed, and a second secret starts at that bracket (or a few characters before it) and ends inside the credential the
// tail holds. The scrubber takes the tail's opening run out as one placeholder; the rest of the crossing secret, and the credential
// after it, are replaced as well, so no character run of the secret that the reference hides is readable.
//
// Each test asserts that the output shows no run of four characters of a known secret or of a planted credential marker that the
// reference output does not show (the reference output is written out beside it), at depths 0 and 28-31 as a depth parameter and as
// nested objects, and inside JSON texts up to 30 deep, framed and in prose. A budget throw drops the span, so it hides everything; it
// is accepted only at depth 31 (and in a JSON text 30 deep), where the scrubber's own depth budget may run out.
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

// [title, text, secrets, reference output (the same framed and in prose, at every depth here)]
const repros: [string, string, string[], string][] = [
  ["a secret from a bracket after an escaped quote and colon into a credential, after a cut name",
    "\"secret\" :MK573097{\\\":[\"x\":TAILVC714B3E7 end", ["\" :MK573", "[\"x\":TAILVC"],
    "\"secret<redacted>097{\\\":<redacted>714B3E7 end"],
  ["a secret from the colon before a bracket into a credential, after a secret that holds a name and its value",
    "\"token\":MK5C9D2{\\\":[\"AUTH\"\":TAILV00FB48", [":[\"AUTH\"\":TA", "token\":MK5C9D2"],
    "\"<redacted>{\\\"<redacted>ILV00FB48"],
];

// A JSON text at depth 0 is the text as one JSON string, a different input from the bare text: the reference output for that string,
// JSON encoded. For the first text the scrub hides the whole string, more than the reference.
const asString: Record<string, string> = {
  [repros[0][0]]: String.raw`"\"\\\"secret\\<redacted>097{\\\\\\\":\\\"<redacted>\\\""`,
  [repros[1][0]]: String.raw`"\"\\\"token\\\":\\\"<redacted>\\\""`,
};

describe("a known secret that crosses the opener of an unclosed JSON tail", () => {
  for (const [title, text, secrets, reference] of repros) {
    for (const framed of sites) {
      for (const depth of [0, 28, 29, 30, 31]) {
        test(`${title}: no run of the secret is readable at depth ${depth} as a parameter (${site(framed)})`, () => {
          hidesAtLeast(param(text, secrets, depth, framed), reference, text, secrets);
        });
        test(`${title}: no run of the secret is readable in objects nested ${depth} deep (${site(framed)})`, () => {
          hidesAtLeast(obj(text, secrets, depth, framed), reference, text, secrets);
        });
      }
      test(`${title}: no run of the secret is readable inside JSON texts 1 to 30 deep (${site(framed)})`, () => {
        for (let depth = 1; depth <= 30; depth++) hidesAtLeast(jsonText(text, secrets, depth, framed), reference, text, secrets);
      });
      test(`${title}: no run of the secret is readable in the text as one JSON string (${site(framed)})`, () => {
        const out = scrubValue(JSON.stringify(text), secrets, "", 0, false, undefined, none, framed) as string;
        hidesAtLeast(out, JSON.parse(asString[title]), text, secrets);
      });
    }
  }

  const [, text, secrets, reference] = repros[0];
  test("the first text through scrubAttribute shows no run of the crossing secret", () => {
    hidesAtLeast(scrubAttribute(text, secrets), reference, text, secrets);
  });
  test("the first text through the export filter's attributes shows no run of the crossing secret", () => {
    const attributes = privacyFilter({}, secrets).attributes({ message: text, "input.value": text, note: text }) as Record<string, unknown>;
    for (const key of ["message", "input.value", "note"]) hidesAtLeast(String(attributes[key]), reference, text, secrets);
  });
});

// Shapes from the tail family fuzz: different outer and inner openers ({, [, {\", [\", {\":, {[, [{, [[) and separators (" : ", "":,
// "=, """":, ": ", \":, =), with the crossing secret starting at the inner opener or up to three characters before it.
// [title, text, secrets, reference output]
const family: [string, string, string[], string][] = [
  ["{\\\":{ and a spaced colon", "\"auth\":MKA58F92{\\\":{\"AUTH\"\" : TAILV34A25F1 end", ["h\":MKA58F", ":{\"AUTH\"\" : TAILV34A"],
    "\"aut<redacted>92{\\\"<redacted>25F1 end"],
  ["[\\\"[ and a doubled quote before the colon", "\"token\":MK8A47DA[\\\"[\"password\"\":TAILV91370D01", ["[\"password\"\":TAILV9", "en\":MK8A4"],
    "\"tok<redacted>7DA[\\\"<redacted>1370D01"],
  ["[{ and an equals sign", "\"auth\":MKC3DA183[{\"x\"=TAILVD5480BB end", ["h\":MKC3D", "{\"x\"=TAILVD5"],
    "\"aut<redacted>A183[<redacted>480BB end"],
  ["[[ and a run of quotes before the colon", "\"x\" :MKDB208[[\"AUTH\"\"\"\":TAILV33CE21BC end", ["\" :MKDB2", "[\"AUTH\"\"\"\":TAILV33CE"],
    "\"x<redacted>08[<redacted>21BC end"],
  ["{\\\"{ and a doubled quote before the colon", "\"x\":MKAE9D7{\\\"{\"secret\"\":TAILV0329D5 ok", ["x\":MKAE9", "{\"secret\"\":TAILV032"],
    "\"<redacted>D7{\\\"<redacted>9D5 ok"],
  ["{[[ with the secret starting at the second bracket", "\"x\":MKAB029B{[[\"AUTH\"\":TAILV97833C]", ["[[\"AUTH\"\":T", "\":MKAB02"],
    "\"x<redacted>9B{<redacted>AILV97833C]"],
  ["[\\\"{ and a bare name with a quoted value", "\"x\" :MK855F4D[\\\"{\"x: \"TAILV13C82E\"", ["\" :MK855F", "\\\"{\"x: \"TAILV13"],
    "\"x<redacted>4D[<redacted>C82E\""],
  ["{[{ and an escaped quote before the colon", "\"auth\" :MKA435237{[{\"secret\"\\\":TAILV0BFF5E0E\"}", ["[{\"secret\"\\\":TA", "h\" :MKA4352"],
    "\"aut<redacted>37{<redacted>ILV0BFF5E0E\"}"],
  ["{\\\":[ and a quoted value after a spaced colon", "\"password\":MK351C50F{\\\":[\"\"AUTH\"\": \"TAILV0DC7016]",
    ["\":[\"\"AUTH\"\": \"TAILV0DC7", "rd\":MK351C5"], "\"passwo<redacted>0F{\\<redacted>016]"],
  ["{\\\"[ with the secret starting at the quote before the bracket", "\"api_key\":MK49F763{\\\"[x: \"TAILV534A142 ok",
    ["y\":MK49F", "\"[x: \"TAILV534A14"], "\"api_ke<redacted>763{\\<redacted>2 ok"],
];

describe("a known secret that crosses the opener of an unclosed tail, across openers and separators", () => {
  for (const [title, text, secrets, reference] of family) {
    for (const framed of sites) {
      test(`${title}: no run of the secret is readable (${site(framed)})`, () => {
        hidesAtLeast(param(text, secrets, 0, framed), reference, text, secrets);
      });
      test(`${title}: no run of the secret is readable in objects nested 30 deep (${site(framed)})`, () => {
        hidesAtLeast(obj(text, secrets, 30, framed), reference, text, secrets);
      });
    }
  }
});
