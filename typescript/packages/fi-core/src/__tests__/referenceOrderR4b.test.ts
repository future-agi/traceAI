import { describe, expect, test } from "@jest/globals";
import { indexPrivacyHints, scrubValue } from "../privacy-runtime";

// A text with a JSON opener that no closer matches (or a complete JSON owner after a stray opener) is read in the reference's order:
// the URL and data URI catalog, then each known secret (longest first), then the value shapes, then the pair rule. A value shape
// never reads a run that a known secret has already taken, and a pair rule never reads a name that a secret has taken out.
// Each expected text in both blocks is what Reference.scrub (spec/auto-instrumentation/generate_privacy_fixtures.py) gives for the same input.
const none = indexPrivacyHints([]);
const run = (value: string, secrets: string[], framed: boolean) => scrubValue(value, secrets, "", 0, false, undefined, none, framed);
const KEY = "sk-TAIL51E8DDxC2DhHTVEOlnEGzmpassword=TAIL1A4A5ADF tail/";

describe("known secrets are replaced before value shapes in text with an unparsed JSON opener", () => {
  for (const framed of [true, false]) {
    const site = framed ? "framed" : "prose";
    test(`a provider key run that holds the start of a secret (${site})`, () => {
      expect(run(`{x ${KEY}`, ["word=TAIL1A4A5"], framed)).toBe("{x <redacted><redacted>ADF tail/");
      expect(run(`[x ${KEY}`, ["word=TAIL1A4A5"], framed)).toBe("[x <redacted><redacted>ADF tail/");
      expect(run(`}x ${KEY}`, ["word=TAIL1A4A5"], framed)).toBe("}x <redacted><redacted>ADF tail/");
    });
    test(`an authorization value that begins inside a secret (${site})`, () => {
      expect(run("{x TAILDB569FAD and BEARER TAIL09E1758BBcZvg y", ["DB569FAD and BEARER "], framed)).toBe("{x TAIL<redacted> y");
    });
    test(`the same text without the opener is unchanged (${site})`, () => {
      expect(run(`x ${KEY}`, ["word=TAIL1A4A5"], framed)).toBe("x <redacted><redacted>ADF tail/");
    });
  }
});

describe("a stray opener before a complete JSON owner, with a secret that holds a quote and a closing brace", () => {
  const owner = "{\"a\": {\"api_key\": \"TAILV0DDAD\"}, \"b\": [\"TAILV0DDAD\"]}";
  const secret = [" \"TAILV0DDAD\"}, "];
  // d2dab4e8 (version 6, round 3): a text that begins as a JSON object, is not one JSON value and holds a credential name (here the
  // quoted member name api_key) is the placeholder as a whole.
  const placeholder = "<redacted>";
  for (const framed of [true, false]) {
    const site = framed ? "framed" : "prose";
    test(`no member of the owner stays readable, with a later pair (${site})`, () => {
      const out = run(`{${owner}\napi_key=TAILV0DDAD`, secret, framed);
      expect(out).not.toContain("TAILV0DDAD");
      expect(out).toBe(placeholder);
    });
    test(`no member of the owner stays readable, owner alone (${site})`, () => {
      const out = run(`{${owner}`, secret, framed);
      expect(out).not.toContain("TAILV0DDAD");
      expect(out).toBe(placeholder);
    });
  }
});
