import { describe, expect, test } from "@jest/globals";
import { credentialSecrets, discoverSecrets, indexPrivacyHints, scrubValue } from "../privacy-runtime";
import { getRegisteredSecrets, registerSecret } from "../secrets";

// The minimum length of a known secret, the depth bound at a string leaf and the budget charged for a complete JSON text, each as
// Reference.scrub (spec/auto-instrumentation/generate_privacy_fixtures.py) reads them: the reference counts code points and
// charges a JSON text once for its own length and once per node of the tree it parses.
const none = indexPrivacyHints([]);
const scrub = (value: unknown, secrets: string[] = []) => scrubValue(value, secrets, "", 0, false, undefined, none, true);
const nest = (depth: number, leaf: unknown) => { let value = leaf; for (let i = 0; i < depth; i++) value = { k: value }; return value; };

describe("minimum secret length counts code points", () => {
  const seven = "\u{1D49C}".repeat(7), eight = "\u{1D49C}".repeat(8);
  test("a secret of seven astral characters (fourteen UTF-16 units) is not a known secret", () => {
    expect(scrub(`a ${seven} b`, [seven])).toBe(`a ${seven} b`);
    expect(scrub(seven, [seven])).toBe(seven);
  });
  test("a secret of eight astral characters is", () => {
    expect(scrub(`a ${eight} b`, [eight])).toBe("a <redacted> b");
    expect(scrub(eight, [eight])).toBe("<redacted>");
  });
  test("a secret found in the data or the headers is not one of seven code points", () => {
    expect(discoverSecrets([{ authorization: seven }])).not.toContain(seven);
    expect(discoverSecrets([{ authorization: eight }])).toContain(eight);
    expect(credentialSecrets({ authorization: seven })).not.toContain(seven);
    expect(credentialSecrets({ authorization: eight })).toContain(eight);
  });
  test("the registry keeps no secret of seven code points", () => {
    registerSecret(seven);
    expect(getRegisteredSecrets()).not.toContain(seven);
    registerSecret(eight);
    expect(getRegisteredSecrets()).toContain(eight);
  });
});

describe("a JSON string literal at depth 32", () => {
  test("is returned as the reference returns it", () => {
    expect(scrub(nest(32, "\"abc\""))).toEqual(nest(32, "\"abc\""));
    expect(scrub(nest(32, " \"abc\""))).toEqual(nest(32, " \"abc\""));
    expect(scrub(nest(32, "\"see TAILV1234ABCD ok\""), ["TAILV1234ABCD"])).toEqual(nest(32, "\"see <redacted> ok\""));
  });
  test("a JSON object there is over the depth budget, as in the reference", () => {
    expect(() => scrub(nest(32, "{\"a\":1}"))).toThrow(/budget/);
  });
});

describe("a complete JSON text is charged as the reference charges it", () => {
  test("a JSON string of about 2,000,000 characters is returned", () => {
    const text = JSON.stringify({ a: "x".repeat(2000000) });
    expect(scrub(text)).toBe(text);
  });
  test("one past the reference's text limit is refused", () => {
    expect(() => scrub(JSON.stringify({ a: "x".repeat(2100000) }))).toThrow(/text budget/);
  });
  test("a JSON array of 16380 numbers is returned", () => {
    const text = JSON.stringify(Array.from({ length: 16380 }, (_, i) => i));
    expect(scrub(text)).toBe(text);
  });
  test("one of 16383 numbers is refused", () => {
    expect(() => scrub(JSON.stringify(Array.from({ length: 16383 }, (_, i) => i)))).toThrow(/structure budget/);
  });
});

// The node budget of a complete JSON text, as Reference.scrub charges it: one node for the text, one for each container and one for
// each scalar or string member (an object key is charged by its length in text characters, not as a node). The largest text each
// shape can have and still be returned is 16384 nodes (privacy.json limits.max_nodes) less the text and its container; the next
// size is refused. A known secret in the text changes neither bound.
describe("the node charge of a complete JSON text is the reference's", () => {
  const SECRET = "TAILV1234ABCD";
  const text = (value: unknown) => JSON.stringify(value);
  const shapes: [string, (n: number) => string, number][] = [
    ["an array of strings", n => text(Array.from({ length: n }, () => "abc")), 16382],
    ["an array of the known secret", n => text(Array.from({ length: n }, () => SECRET)), 16382],
    ["an object of N keys with string values", n => text(Object.fromEntries(Array.from({ length: n }, (_, i) => ["k" + i, "v"]))), 16382],
    ["an object of N keys with number values", n => text(Object.fromEntries(Array.from({ length: n }, (_, i) => ["k" + i, i]))), 16382],
    ["an object of N string keys and string values", n => text(Object.fromEntries(Array.from({ length: n }, (_, i) => ["k" + i, "s" + i]))), 16382],
    ["an array of numbers", n => text(Array.from({ length: n }, (_, i) => i)), 16382],
    ["an array of mixed scalars", n => text(Array.from({ length: n }, (_, i) => [null, true, "x", 1.5][i % 4])), 16382],
    ["an array of objects", n => text(Array.from({ length: n }, (_, i) => ({ a: i }))), 8191],
    ["an array of arrays", n => text(Array.from({ length: n }, (_, i) => [i])), 8191],
    ["an array of two-string arrays", n => text(Array.from({ length: n }, () => ["a", "b"])), 5460],
  ];
  for (const secrets of [[], [SECRET]]) {
    for (const [name, make, largest] of shapes) {
      test(`${name}: ${largest} is returned and ${largest + 1} is refused${secrets.length ? " (with a known secret)" : ""}`, () => {
        const ok = make(largest);
        expect(scrub(ok, secrets)).toBe(secrets.length ? ok.split(SECRET).join("<redacted>") : ok);
        expect(() => scrub(make(largest + 1), secrets)).toThrow(/structure budget/);
      });
    }
  }
});
