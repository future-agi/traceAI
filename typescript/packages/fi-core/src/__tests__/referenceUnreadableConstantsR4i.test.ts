import { describe, expect, test } from "@jest/globals";
import { indexPrivacyHints, scrubValue } from "../privacy-runtime";

// A text that is one JSON value apart from NaN, Infinity or -Infinity. The d2dab4e8 Reference (spec/auto-instrumentation/
// generate_privacy_fixtures.py sha256 4dbcc945...2c5192c6, Reference.unreadable(), version 6 round 3) reads it with those spelled as
// they are and makes it the placeholder when that reading repeats a member name or its export would change it; the export of a member
// path reads a name with percent forms once (version 5). A name of more than max_depth components in that reading makes the whole
// exported value the placeholder. Each expected value is the d2dab4e8 Reference output of
// Reference(privacy_catalog()).scrub(value, "", None, secrets, _depth=0, framed=True).
const none = indexPrivacyHints([]);
const run = (value: unknown, secrets: string[]) => scrubValue(value, secrets, "", 0, false, undefined, none, true);

describe("referenceUnreadableConstantsR4i: a JSON text that holds NaN or an infinity", () => {
  test.each([
    ["a percent-spelled token name beside NaN", "{\"x\": NaN, \"tok%65n\": \"TAILVAAAA5\"}"],
    ["a percent-spelled password name beside NaN", "{\"x\": NaN, \"%70assword\": \"TAILVAAAA5\"}"],
    ["a nested percent-spelled secret name and NaN", "{\"user\": {\"secr%65t\": \"TAILVAAAA1\"}, \"n\": NaN}"],
    ["a percent-spelled token name beside Infinity", "{\"x\": Infinity, \"tok%65n\": \"TAILVAAAA5\"}"],
  ])("%s is the placeholder", (_, value) => {
    expect(run(value, [])).toEqual("<redacted>");
  });

  test("a name of more than max_depth components in such a text makes the whole value the placeholder", () => {
    const name = Array.from({ length: 40 }, (_, i) => `a${i}`).join(".");
    const value = { x: `[NaN, {"${name}":1}]`, y: "note SECRETAAAABBBBCC end" };
    expect(run(value, ["SECRETAAAA", "AAAABBBBCC"])).toEqual("<redacted>");
  });

  test("the same text with no constant and no credential name is read as text", () => {
    expect(run("{\"x\": 1, \"note\": \"TAILVAAAA5\"} tail", [])).toEqual("{\"x\": 1, \"note\": \"TAILVAAAA5\"} tail");
  });
});

// Layers of JSON strings that each hold NaN: each layer is read once, so ten layers cost about as much per character as a few times two.
const layers = (depth: number, n: number) => { let s = "[NaN,\"" + "x".repeat(n) + "\"]"; for (let i = 0; i < depth; i++) s = JSON.stringify(["x", s, NaN]).replace("null", "NaN"); return s; };
const best = (text: string) => {
  let low = Infinity;
  for (let r = 0; r < 3; r++) { const started = process.hrtime.bigint(); scrubValue(text, []); low = Math.min(low, Number(process.hrtime.bigint() - started) / 1e6); }
  return low;
};
describe("referenceUnreadableConstantsR4i: cost of nested layers that hold NaN", () => {
  test("ten layers take at most about sixteen times as long as two layers of the same length", () => {
    const shallow = layers(2, 100_000), deep = layers(10, 100_000);
    best(shallow);
    let ratio = Infinity;
    for (let attempt = 0; attempt < 3 && ratio >= 16; attempt++) ratio = Math.min(ratio, best(deep) / Math.max(best(shallow), 1));
    expect(ratio).toBeLessThan(16);
  });
  test("eight layers: four times the text takes at most about five times as long", () => {
    const small = layers(8, 25_000), large = layers(8, 100_000);
    best(small);
    let ratio = Infinity;
    for (let attempt = 0; attempt < 3 && ratio >= 5.5; attempt++) ratio = Math.min(ratio, best(large) / Math.max(best(small), 1));
    expect(ratio).toBeLessThan(5.5);
  });
});
