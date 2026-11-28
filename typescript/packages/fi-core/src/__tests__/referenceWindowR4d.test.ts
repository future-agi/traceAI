import { describe, expect, test } from "@jest/globals";
import { indexPrivacyHints, scrubText, scrubValue } from "../privacy-runtime";

// A secret that takes a JSON owner out whole leaves the pair rule to read the text with that owner gone. A pair reads its name across any
// run of white space or any other characters the reference reads before its value, so what the reference redacts is redacted wherever it
// starts after the owner, at any distance: 1000, 1024 or 5000 characters away reads as a pair next to the owner reads.
//
// The reference keeps the quotes and the parts of an owner that a secret crosses, and quotes a pair's value; the scrubber takes the owner
// out whole. Where the two outputs differ in that way, each test asserts the scrubber's exact output and that it shows no credential
// marker and no secret that the reference output does not show (the reference output is written out beside it).
const none = indexPrivacyHints([]);
const run = (value: any, secrets: string[], framed: boolean, path = "message") => scrubValue(value, secrets, path, 0, false, undefined, none, framed);
const nest = (depth: number, leaf: any) => { let value = leaf; for (let i = 0; i < depth; i++) value = { k: value }; return value; };
const leafOf = (value: any, depth: number) => { let out: any = value; for (let i = 0; i < depth; i++) out = out.k; return out; };
/** Milliseconds, for the time a scrub takes. */
const clock = () => { const [s, ns] = process.hrtime(); return s * 1e3 + ns / 1e6; };
const MARKER = /TAIL[A-Z0-9]{2,}|MK[0-9A-F]{4,}|hunter2\w*/g;
/** The output shows no credential marker of the input and no secret that the reference output does not show. */
function redactsAtLeast(out: string, reference: string, input: string, secrets: string[]): void {
  for (const marker of new Set(input.match(MARKER) ?? [])) if (!reference.includes(marker)) expect(out).not.toContain(marker);
  for (const secret of secrets) if (!reference.includes(secret)) expect(out).not.toContain(secret);
}

describe("a quoted key taken out before a run of keys, with a long value before the last pair", () => {
  const text = (n: number) => "\"AAAAAAAA\":\"\"\"auth\":\"" + "x".repeat(n) + "\"{token\":\"TAILV111111";
  const reference = "\"<redacted>\":\"\"\"auth\":\"<redacted>\"{token\":\"<redacted>\"";
  for (const framed of [true, false]) {
    test(`the token value is redacted after 1030 characters as after 1000 (${framed ? "framed" : "prose"})`, () => {
      for (const n of [1000, 1030]) {
        const out = scrubValue(text(n), ["AAAAAAAA"], "", 0, false, undefined, indexPrivacyHints([]), framed);
        // Each read hides part of what the other shows; the text is written once with what either hides replaced.
        expect(out).toBe("<redacted>");
        redactsAtLeast(out, reference, text(n), ["AAAAAAAA"]);
      }
    });
  }
});

describe("a pair value after a long run of spaces, beside an owner a secret takes out", () => {
  test("a token value 1024 spaces after its name is redacted as one 1023 spaces after it", () => {
    for (const n of [1023, 1024]) {
      const gap = " ".repeat(n), input = "\"869TAILV\":\"token\":" + gap + "MKAAAA0001";
      const out = run(input, ["869TAILV"], true);
      expect(out).toBe("<redacted>:{gap}<redacted>".replace("{gap}", gap));
      redactsAtLeast(out, "\"<redacted>\":\"token\":{gap}\"<redacted>\"".replace("{gap}", gap), input, ["869TAILV"]);
    }
  });
  for (const depth of [0, 5, 30]) {
    test(`a password value 1014 spaces after a name that holds part of a secret is redacted (depth ${depth})`, () => {
      for (const n of [1013, 1014]) {
        const gap = " ".repeat(n), input = "\"aa SECR\"ETxy_password\":" + gap + "hunter2xyz";
        const out = leafOf(run(nest(depth, input), ["SECR\"ETxy"], true), depth);
        expect(out).toBe("<redacted>_password\":{gap}<redacted>".replace("{gap}", gap));
        redactsAtLeast(out, "\"aa <redacted>_password\":{gap}\"<redacted>\"".replace("{gap}", gap), input, ["SECR\"ETxy"]);
      }
    });
  }
  test("a value after an escaped quote and a name, 1010 characters after a secret in brackets, is redacted", () => {
    const input = "PASS%3D[TAILF86]" + "x".repeat(1010) + " t,jwt=J secret\\\":TAILCFBC613F";
    const out = run(input, ["[TAILF86"], true);
    expect(out).toBe("PASS%3D<redacted>,jwt=J secret\\\":<redacted>");
    redactsAtLeast(out, "PASS%3D<redacted>,jwt=J secret\\\":\\\"<redacted>\\\"", input, ["[TAILF86"]);
  });
});

// The shapes of referenceOrderR4c.test.ts whose secret touches a JSON-like span before a quoted pair name, with a long gap inserted just
// before the value the pair rule redacts.
describe("a secret that touches a JSON-like span, before a quoted pair name, with a long gap before the value", () => {
  // [title, text, secrets, where the gap goes, output with a gap of spaces, output with a gap of newlines, reference output]
  const cases: [string, string, string[], number, string, string, string][] = [
    ["ends inside a quoted key", "\"869TAILV\":\"token\":TAILVF", ["869TAILV"], 19, "<redacted>:{gap}<redacted>", "<redacted>:{gap}<redacted>", "\"<redacted>\":\"token\":{gap}\"<redacted>\""],
    // d2dab4e8 (version 6, round 3): a text that begins as a JSON object, is not one JSON value and holds a credential name as a quoted
    // member name is the placeholder as a whole.
    ["crosses a quote and a brace", "{\"\":\"\"}\\\":\\\"TAsession\":TAILVF", ["}\\\":\\\"TA"], 23, "<redacted>", "<redacted>", "<redacted>"],
    ["ends at a closing bracket", "{\"secret\":[true]}\":TAILV5", ["\":[true]"], 19, "<redacted>", "<redacted>", "<redacted>"],
    ["starts inside a quoted value", "\"B\":\"TAILpass\":TAILVB", ["B\":\"TAIL"], 15, "<redacted>:{gap}<redacted>", "<redacted>:{gap}<redacted>", "\"<redacted>pass\":{gap}\"<redacted>\""],
    ["starts a string that holds a backslash and a quote and a name", "[\"TAILVE\\\"pwd\"}\\\":TAILVB\"", ["[\"TAILVE"], 18, "<redacted>\\\"<redacted>\"", "<redacted>\\\":{gap}<redacted>", "<redacted>\\\"pwd\"}\\\":{gap}\\\"<redacted>\\\""],
    ["starts an empty string and is followed by an unclosed object", "\"\"TAILV40{pass\":TAILV0", ["\"TAILV40"], 16, "<redacted>{pass\":{gap}<redacted>", "<redacted>{pass\":{gap}<redacted>", "\"<redacted>{pass\":{gap}\"<redacted>\""],
    ["takes a quoted key out before an unclosed object", "\"AAAAAAAA\":\"\"{token\":\"TAILV111111", ["AAAAAAAA"], 21, "<redacted>{token\":{gap}<redacted>", "<redacted>{token\":{gap}<redacted>", "\"<redacted>\":\"\"{token\":{gap}\"<redacted>\""],
    ["takes a quoted key out before a longer run of keys", "\"AAAAAAAA\":\"\"\"auth\":\"\"{token\":\"TAILV111111", ["AAAAAAAA"], 30, "<redacted>", "<redacted>", "\"<redacted>\":\"\"\"auth\":\"<redacted>\"{token\":{gap}\"<redacted>\""],
    ["overlaps another secret in a literal that starts with a brace", "\"{x: TAILV8C0819,pass:\"", ["{x: TAILV8C08", "819,pass"], 16, "\"<redacted>\"", "\"<redacted>19{gap},pass:<redacted>", "\"<redacted>19{gap},pass:<redacted>"],
    ["ends a string that is followed by a closing brace", "\"pass\\\": 1.5}\"\":TAILVF\"", ["\": 1.5}\""], 16, "\"<redacted>\"\"<redacted>\"", "\"<redacted>\"\":{gap}<redacted>", "\"pass\\<redacted>\":{gap}\"<redacted>\""],
  ];
  for (const [title, text, secrets, at, spaces, newlines, reference] of cases) {
    for (const n of [1030, 2100]) {
      for (const [name, c, expected] of [["spaces", " ", spaces], ["newlines", "\n", newlines]] as const) {
        for (const depth of [0, 30, 31]) {
          test(`${title}, ${n} ${name} before the value (depth ${depth})`, () => {
            const gap = c.repeat(n), input = text.slice(0, at) + gap + text.slice(at);
            const out = leafOf(run(nest(depth, input), secrets, true), depth);
            expect(out).toBe(expected.replace("{gap}", gap));
            if (expected === reference) expect(out).toBe(reference.replace("{gap}", gap));
            redactsAtLeast(out, reference.replace("{gap}", gap), input, secrets);
          });
        }
      }
    }
  }
});

// Every pair after the owner is a range the reference redacts; each is replaced where it stands, so the second scan for secrets reads the
// placeholders the scrub wrote. It is charged only for the placeholders the text brought and reads the text once per secret.
describe("a secret that crosses an owner stays linear in the number of pairs after long gaps", () => {
  test.each([[30_000, 32], [1_900, 1]])("%i credential pairs, one in %i after 2000 spaces", (pairs, every) => {
    let text = 'abcdefg {"k":"v"}';
    for (let i = 0; i < pairs; i++) text += (i % every === 0 ? " ".repeat(2000) : " ") + `pwd=TAILV${i}`;
    const start = clock();
    const out = scrubText(text, ['defg {"k']);
    expect(clock() - start).toBeLessThan(5_000);
    // The reference keeps '":"v"}' of the owner the secret crosses; the scrubber takes the owner out whole. The pairs read the same.
    expect(out).toBe("abc<redacted>" + " ".repeat(2000) + "pwd=<redacted>" + " pwd=<redacted>".repeat(pairs - 1));
  });
});
