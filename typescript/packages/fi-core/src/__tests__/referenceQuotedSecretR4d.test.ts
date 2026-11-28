import { describe, expect, test } from "@jest/globals";
import { indexPrivacyHints, scrubValue } from "../privacy-runtime";

// A given secret that is a pair value with its double quotes is replaced by the placeholder alone in the text the reference reads, so the pair
// rule reads the value on past the closing quote, up to the end of the value-like text that follows it. The expected outputs are the reference's
// outputs for the same inputs (spec/auto-instrumentation/generate_privacy_fixtures.py, Reference.scrub).
const none = indexPrivacyHints([]);
const run = (value: unknown, secrets: string[], framed: boolean) => scrubValue(value, secrets, "message", 0, false, undefined, none, framed);
const nest = (depth: number, leaf: unknown) => { let value = leaf; for (let i = 0; i < depth; i++) value = { k: value }; return value; };

const cases: { name: string; value: string; secret: string; expected: string; marker: string }[] = [
  { name: "after a quoted name and a colon", value: "log 'password':\"hunter2hunter2\"TAILVSECRET123", secret: "\"hunter2hunter2\"", expected: "log 'password':'<redacted>'", marker: "TAILVSECRET123" },
  { name: "with a percent sign after the closing quote", value: "'secret': \"pa ss word\"%MK48170922 done", secret: "\"pa ss word\"", expected: "'secret': '<redacted>'", marker: "MK48170922" },
  { name: "with two tokens and a comma after the closing quote", value: "note: 'api_key': \"xxxxxxxx\"MK0E5771E1TAILVBF95B3, ok", secret: "\"xxxxxxxx\"", expected: "note: 'api_key': '<redacted>'", marker: "TAILVBF95B3" },
  { name: "with a slash after the closing quote and a pair after it", value: "\"msg\": 'auth':\"pa ss word\"/TAILV9184BD x=1", secret: "\"pa ss word\"", expected: "\"msg\": 'auth':'<redacted>' x=1", marker: "TAILV9184BD" },
  { name: "after a newline, with an equals sign after the closing quote", value: "a=1 'PASS':\n\"hunter2hunter2\"=TAILV5DC73F end", secret: "\"hunter2hunter2\"", expected: "a=1 'PASS':\n'<redacted>'", marker: "TAILV5DC73F" },
];

describe("a given secret that is a pair value with its quotes, followed directly by more of the value", () => {
  for (const { name, value, secret, expected, marker } of cases) {
    for (const framed of [true, false]) {
      const site = framed ? "framed" : "prose";
      for (const depth of [0, 30, 31]) {
        test(`${name}: the text after the closing quote is redacted (${site}, depth ${depth})`, () => {
          const out = run(nest(depth, value), [secret], framed);
          expect(JSON.stringify(out)).not.toContain(marker);
          expect(out).toEqual(nest(depth, expected));
        });
      }
      test(`${name}: the whole value is the placeholder past the depth limit (${site}, depth 32)`, () => {
        expect(run(nest(32, value), [secret], framed)).toBe("<redacted>");
      });
    }
    for (const depth of [1, 30]) {
      test(`${name}: inside JSON text the text after the closing quote is redacted (depth ${depth})`, () => {
        const out = run(JSON.stringify(nest(depth, value)), [secret], true);
        expect(out).not.toContain(marker);
        expect(out).toBe(JSON.stringify(nest(depth, expected)));
      });
    }
  }
});

describe("a given secret that is a whole quoted pair value", () => {
  test("is redacted with the text around it kept as it stands", () => {
    expect(run("log 'password':\"hunter2hunter2\"", ["\"hunter2hunter2\""], true)).toBe("log 'password':'<redacted>'");
  });
  test("leaves a JSON object after the pair to its own read", () => {
    expect(run("user said pass=\"xxxxxxxx\"TAILV8FF09F x=1{\"pwd\": \"MKE81FB713\"}", ["\"xxxxxxxx\""], true)).toBe("user said pass=<redacted> x=1{\"pwd\": \"<redacted>\"}");
  });
});

// A given secret that is one whole JSON object or array is replaced by the placeholder in its own quotes; the pair rule reads the value on
// past its closing bracket as the reference reads it with the placeholder alone.
describe("a given secret that is one whole JSON object or array as a pair value, followed directly by more of the value", () => {
  const shapes: [string, string, string, string][] = [
    ["an object", "log 'password':{\"a\": \"b\"}TAILVSECRET123", "{\"a\": \"b\"}", "TAILVSECRET123"],
    ["an array", "log 'password':[\"x1\", 2]TAILVSECRET456", "[\"x1\", 2]", "TAILVSECRET456"],
  ];
  for (const [name, value, secret, marker] of shapes) {
    for (const framed of [true, false]) {
      for (const depth of [0, 30, 31]) {
        test(`${name}: the text after the closing bracket is redacted (${framed ? "framed" : "prose"}, depth ${depth})`, () => {
          const out = run(nest(depth, value), [secret], framed);
          expect(JSON.stringify(out)).not.toContain(marker);
          expect(out).toEqual(nest(depth, "log 'password':'<redacted>'"));
        });
      }
    }
  }
});
