import { describe, expect, test } from "@jest/globals";
import { indexPrivacyHints, scrubValue } from "../privacy-runtime";

// A header pair such as 'Authorization=' or 'Cookie:' right before a line break has an empty value, and the pair rule writes its
// placeholder where that value starts. The reference replaces the secrets the caller passed before it reads any pair: a secret that
// runs from the separator across the line break into the value is taken out first, and the pair after it reads the placeholder as
// its value. Each expected value below is the output of the reference (spec/auto-instrumentation/generate_privacy_fixtures.py).
const none = indexPrivacyHints([]);
const run = (value: unknown, secrets: string[], framed: boolean) => scrubValue(value, secrets, "message", 0, false, undefined, none, framed);
const nest = (depth: number, leaf: unknown) => { let value = leaf; for (let i = 0; i < depth; i++) value = { k: value }; return value; };

describe("a secret that runs from a header separator across a line break into the value", () => {
  const value = "{Authorization=\nMKDE682829,";
  for (const framed of [true, false]) {
    const site = framed ? "framed" : "prose";
    for (const depth of [0, 31]) {
      test(`takes the value out with the secret, in text that starts with a brace (${site}, depth ${depth})`, () => {
        const out = run(nest(depth, value), ["=\nMKDE68"], framed);
        expect(JSON.stringify(out)).not.toContain("MKDE68");
        expect(out).toEqual(nest(depth, "{Authorization<redacted>2829,"));
      });
    }
  }
  test("takes the value out after a quoted Cookie name", () => {
    expect(run("{x x\ntrue\"Cookie:\nMK2867C3B1", ["ookie:\nMK286"], true)).toBe("{x x\ntrue\"C<redacted>7C3B1");
  });
  test("takes the name and the value out when the secret starts with the name, in text that starts with a bracket", () => {
    expect(run("[ Cookie=\nhunter233zz\\\"", ["Cookie=\nhunter"], true)).toBe("[ <redacted>233zz\\\"");
  });
  test("takes the value out one level before the depth limit, after escaped quotes (prose)", () => {
    const out = run(nest(31, "{x \\\"Cookie\\\"=\nTAILV285BE9\""), ["\"Cookie\\\"=\nT", "\\\"=\nTAILV", "\\\"=\nTAILV28"], false);
    expect(out).toEqual(nest(31, "{x \\<redacted>AILV285BE9\""));
  });
  test("takes the value out inside a JSON text nested thirty levels", () => {
    const leaf = "{{Authorization:\nMK116F2E46}\\\"";
    const out = run("{\"k\": ".repeat(30) + JSON.stringify(leaf) + "}".repeat(30), ["ization:\nMK116"], true);
    expect(out).toBe(JSON.stringify(nest(30, "{{Author<redacted>F2E46}\\\"")));
  });
  test("takes every value out when the pair repeats", () => {
    expect(run("{" + "Cookie=\nMKDE682829, ".repeat(3), ["=\nMKDE68"], true)).toBe("{Cookie<redacted>2829, Cookie<redacted>2829, Cookie<redacted>2829, ");
    expect(run("{" + "Cookie=\n x ".repeat(3), ["Cookie=\n x"], true)).toBe("{<redacted> <redacted> <redacted> ");
  });
});

describe("three secrets across the pairs of a text that starts with a brace", () => {
  const value = "{passwd\"%3DTAILV7B9F8D \"x-api-key\"':'TAILVDA0026\\\"Authorization\\\"=\nMKDE682829,";
  const secrets = ["\\\"=\nMKDE68", "9F8D \"x-a", "A0026\\\"Authoriz"];
  const clean = "{passwd\"%3DTAILV7B<redacted>pi-key\"':'TAILVD<redacted>ation<redacted>2829,";
  for (const depth of [0, 28]) {
    test(`are each replaced where they stand and take the value after the separator out (depth ${depth})`, () => {
      const out = run(nest(depth, value), secrets, true);
      expect(JSON.stringify(out)).not.toContain("MKDE68");
      expect(out).toEqual(nest(depth, clean));
    });
  }
});

describe("an empty header value that no secret runs into", () => {
  test("gets the placeholder where the value starts, as the reference writes it", () => {
    expect(run("{Authorization=\nMKDE682829,", [], true)).toBe("{Authorization=<redacted>\nMKDE682829,");
  });
  test("keeps that placeholder when the secret is elsewhere in the text", () => {
    expect(run("{Authorization=\nMKDE682829, x=\nMKDE68", ["x=\nMKDE68"], true)).toBe("{Authorization=<redacted>\nMKDE682829, <redacted>");
  });
});

describe("deep brackets around a short text", () => {
  test("are read in time linear in their number", () => {
    const value = "x" + "[".repeat(200000) + "x" + "]".repeat(200000);
    const started = Date.now();
    expect(run(value, ["[[[[x]]]]"], true)).toBe(value.replace("[[[[x]]]]", "<redacted>"));
    expect(Date.now() - started).toBeLessThan(5000);
  });
});
