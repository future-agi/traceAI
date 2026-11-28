import { describe, expect, test } from "@jest/globals";
import { indexPrivacyHints, scrubValue } from "../privacy-runtime";

// A text that opens with a bracket and is not one complete JSON document: a known secret that takes out part of a credential name (or
// the whole name with the delimiters after it) leaves nothing for the pair rule to read once the secret is replaced, so the value the
// name stands for is not redacted where it stands. The whole text is the placeholder instead, at every depth and through a JSON text.
const none = indexPrivacyHints([]);
const run = (value: unknown, secrets: string[], framed: boolean) => scrubValue(value, secrets, "message", 0, false, undefined, none, framed);
const nest = (depth: number, leaf: unknown) => { let value = leaf; for (let i = 0; i < depth; i++) value = { k: value }; return value; };
const sites = [true, false] as const;
const site = (framed: boolean) => framed ? "framed" : "prose";

const cutNames: [title: string, leaf: string, secrets: string[], depths: number[]][] = [
  ["a secret that ends in the opening quote of a value after a nested name", "[[{\"secret\":\"MK3E34AA94\"}1.5secret", ["[{\"secret\":\""], [0, 30, 31]],
  ["a secret that holds a whole quoted name and its colon", "{:\"password\": \"MK2B1853A7\"secret\n[", ["\"password\": "], [0, 31]],
  ["a secret that starts inside a bare name and ends after a quoted name", "{api_key\n{\"secret\":\"MK9ABC5DD6\"}\"", ["ey\n{\"secret\""], [0, 31]],
  ["a secret that ends inside a quoted name", "{x \"password\": \"MK5949289F\"", ["x \"passw"], [0, 31]],
  ["a secret that holds the brace before a name and the name's delimiters", "{\"a\": true}token\" : \"MKE7FBA8FA\n[", ["}token\" : \""], [0, 28]],
  ["a secret that holds a name in single quotes after a JSON field", "{\"note\": \"hi\", 'PASS'\": \"MKDF387C20 ok]", ["PASS'\": \""], [0]],
];

describe("a known secret that cuts a credential name in a text that opens with a bracket", () => {
  for (const [title, leaf, secrets, depths] of cutNames) {
    for (const framed of sites) {
      for (const depth of depths) {
        test(`${title}: the whole text is the placeholder at depth ${depth} (${site(framed)})`, () => {
          expect(run(nest(depth, leaf), secrets, framed)).toEqual(nest(depth, "<redacted>"));
        });
        if (depth) test(`${title}: the whole text is the placeholder inside a JSON text ${depth} deep (${site(framed)})`, () => {
          expect(run(JSON.stringify(nest(depth - 1, leaf)), secrets, framed)).toBe(JSON.stringify(nest(depth - 1, "<redacted>")));
        });
      }
    }
  }
  for (const framed of sites) {
    test(`a name in single quotes after a JSON field: the whole text is the placeholder inside a JSON text 32 deep (${site(framed)})`, () => {
      const leaf = "{\"note\": \"hi\", 'PASS'\": \"MKDF387C20 ok]";
      expect(run(JSON.stringify(nest(31, leaf)), ["PASS'\": \""], framed)).toBe(JSON.stringify(nest(31, "<redacted>")));
    });
  }
});

// The same texts without the secret, or with a secret that cuts no name. A text that opens with a word is read by the pair rule, which
// redacts the value where it stands.
// d2dab4e8 (version 6, round 3): a text that begins as a JSON object or array, is not one JSON value and holds a credential name (as a
// quoted member name) is the placeholder as a whole, with or without a secret that cuts no name.
const kept: [title: string, leaf: string, secrets: string[], depths: number[], expected: string][] = [
  ["a nested name with no secret", "[[{\"secret\":\"MK3E34AA94\"}1.5secret", [], [0, 31], "<redacted>"],
  ["a nested name with a secret after the value", "[[{\"secret\":\"MK3E34AA94\"}1.5secret", ["}1.5"], [0, 31], "<redacted>"],
  ["a quoted name and colon with no secret", "{:\"password\": \"MK2B1853A7\"secret\n[", [], [0, 31], "<redacted>"],
  ["a quoted name and colon with a secret in the value's tail", "{:\"password\": \"MK2B1853A7\"secret\n[", ["A7\"secret"], [0, 31], "<redacted>"],
  ["a quoted name with no secret", "{x \"password\": \"MK5949289F\"", [], [0, 31], "<redacted>"],
  ["a quoted name with a secret before it", "{x \"password\": \"MK5949289F\"", ["{x "], [0, 31], "<redacted>"],
  ["a nested name with a secret after the value", "{api_key\n{\"secret\":\"MK9ABC5DD6\"}\"", ["\"}\""], [0], "<redacted>"],
  ["a text that opens with a word", "note \"password\": \"MK5949289F\"", ["e \"passw"], [0, 31], "not<redacted>"],
];

describe("a text with no secret that cuts a credential name", () => {
  for (const [title, leaf, secrets, depths, expected] of kept) {
    const reading = expected === "<redacted>" ? "the whole text is the placeholder" : "the value is redacted where it stands";
    for (const framed of sites) {
      for (const depth of depths) {
        test(`${title}: ${reading} at depth ${depth} (${site(framed)})`, () => {
          expect(run(nest(depth, leaf), secrets, framed)).toEqual(nest(depth, expected));
        });
        if (depth) test(`${title}: ${reading} inside a JSON text ${depth} deep (${site(framed)})`, () => {
          expect(run(JSON.stringify(nest(depth - 1, leaf)), secrets, framed)).toBe(JSON.stringify(nest(depth - 1, expected)));
        });
      }
    }
  }
});

describe("deep brackets", () => {
  test("a text of many nested brackets is read in linear time", () => {
    const value = "x" + "[".repeat(2e5) + "x" + "]".repeat(2e5);
    const started = process.hrtime.bigint();
    try { run(value, ["xx"], true); } catch { /* a budget error ends the read too */ }
    expect(Number(process.hrtime.bigint() - started) / 1e6).toBeLessThan(5000);
  });
  test("a text that opens with many brackets and holds a secret is read in linear time", () => {
    const value = "[".repeat(1e5) + "x \"password\": \"MK5949289F\"" + "]".repeat(1e5);
    const started = process.hrtime.bigint();
    try { run(value, ["x \"passw"], true); } catch { /* a budget error ends the read too */ }
    expect(Number(process.hrtime.bigint() - started) / 1e6).toBeLessThan(5000);
  });
});
