import { describe, expect, test } from "@jest/globals";
import { indexPrivacyHints, scrubValue } from "../privacy-runtime";

// A text that starts with a JSON object, then a gap of a thousand or more characters, then a pair whose value holds a credential.
// Next to the depth limit (30 and 31 levels) the export returns what the reference returns and keeps no run of the credential
// readable. Each expected value below is the output of the reference (spec/auto-instrumentation/generate_privacy_fixtures.py),
// which is the same at depth 0 and 28-31, framed and in prose.
const none = indexPrivacyHints([]);
const nest = (depth: number, leaf: unknown) => { let value = leaf; for (let i = 0; i < depth; i++) value = { k: value }; return value; };
const jsonText = (depth: number, leaf: string) => "{\"k\": ".repeat(depth) + JSON.stringify(leaf) + "}".repeat(depth);

// clean() gives the reference output for a filler: the text up to the gap followed by the given tail, or the placeholder
// "<redacted>" in place of the whole text.
type Form = { name: string; before: string; after: string; secrets: string[]; clean: (filler: string) => { tail: string } | { whole: string } };
const forms: Form[] = [
  {
    name: "an empty object, then a session pair behind escaped quotes",
    before: "{\"\":\"\"}",
    after: "\\\":\\\"TAsession\":TAILVF",
    secrets: ["}\\\":\\\"TA"],
    // d2dab4e8 (version 6, round 3): a text that begins as a JSON object, is not one JSON value and holds a credential name
    // (the quoted member name ':\\"TAsession', read as JSON reads names) is the placeholder as a whole.
    clean: () => ({ whole: "<redacted>" }),
  },
  {
    name: "a quoted secret name, then the rest of its object and a pair after it",
    before: "{\"secret\"",
    after: ":[true]}\":TAILV5",
    secrets: ["\":[true]"],
    // d2dab4e8 (version 6, round 3): with a blank gap between the quoted name and ':[true]}' the text begins as a JSON object,
    // is not one JSON value and holds a quoted credential member name, so it is the placeholder as a whole.
    clean: (filler) => (filler === "a b," ? { tail: ":[true]}\":\"<redacted>\"" } : { whole: "<redacted>" }),
  },
];
const fillers: Array<[string, string]> = [[" ", "spaces"], ["\n", "line breaks"], ["a b,", "'a b,'"]];
const gaps = [1000, 1024, 1030, 2100];
const depths = [0, 28, 29, 30, 31];

for (const form of forms) {
  describe(`a gap after the first JSON object: ${form.name}`, () => {
    for (const [filler, fillerName] of fillers) {
      for (const framed of [true, false]) {
        const site = framed ? "framed" : "prose";
        const cases = gaps.map((size) => {
          const gap = filler.repeat(size);
          const expected = form.clean(filler);
          return { size, value: form.before + gap + form.after, clean: "whole" in expected ? expected.whole : form.before + gap + expected.tail };
        });
        test(`is returned as the reference returns it, or hidden whole at depth 0 and 28-31, passed as the depth (${fillerName}, ${site})`, () => {
          for (const { value, clean } of cases) {
            for (const depth of depths) {
              const out = scrubValue(value, form.secrets, "message", depth, false, undefined, none, framed);
              expect(out).not.toContain("TAILV");
              expect(out).toBe(clean);
            }
          }
        });
        test(`is returned as the reference returns it, or hidden whole in objects nested 0 and 28-31 deep (${fillerName}, ${site})`, () => {
          for (const { value, clean } of cases) {
            for (const depth of depths) {
              const out = scrubValue(nest(depth, value), form.secrets, "message", 0, false, undefined, none, framed);
              expect(JSON.stringify(out)).not.toContain("TAILV");
              expect(out).toEqual(nest(depth, clean));
            }
          }
        });
        test(`is returned as the reference returns it, or hidden whole inside JSON texts 28-31 deep (${fillerName}, ${site})`, () => {
          for (const { value, clean } of cases) {
            for (const depth of depths.filter((d) => d > 0)) {
              const out = scrubValue(jsonText(depth, value), form.secrets, "message", 0, false, undefined, none, framed);
              expect(out).not.toContain("TAILV");
              expect(out).toBe(JSON.stringify(nest(depth, clean)));
            }
          }
        });
      }
    }
  });
}
