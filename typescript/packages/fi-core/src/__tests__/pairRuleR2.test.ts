import { describe, expect, test } from "@jest/globals";
import { indexPrivacyHints, scrubAttribute, scrubText, scrubValue } from "../privacy-runtime";

// Phase 2 round 2: value shapes read as Python's re reads them, structured JSON means the whole input, known secrets apply one at
// a time, and the '://' userinfo lookup is linear. Each expected text is what Reference.scrub (spec/auto-instrumentation/
// generate_privacy_fixtures.py) gives for the same input.
const R = "<redacted>";
const none = indexPrivacyHints([]);
const framed = (value: unknown, secrets: string[] = [], path = "") => scrubValue(value, secrets, path, 0, false, undefined, none, true);
const unframed = (value: unknown, secrets: string[] = []) => scrubValue(value, secrets, "", 0, false, undefined, none, false);

// str.isspace, exactly.
const PY_SPACE = [0x09, 0x0a, 0x0b, 0x0c, 0x0d, 0x1c, 0x1d, 0x1e, 0x1f, 0x20, 0x85, 0xa0, 0x1680,
  ...Array.from({ length: 11 }, (_, i) => 0x2000 + i), 0x2028, 0x2029, 0x202f, 0x205f, 0x3000];
const hex = (code: number) => "U+" + code.toString(16).toUpperCase().padStart(4, "0");

describe("value shapes use Python str.isspace between the scheme and the value", () => {
  for (const code of PY_SPACE) {
    const c = String.fromCharCode(code);
    test(`Bearer and Basic with ${hex(code)}`, () => {
      for (const scheme of ["Bearer", "Basic"]) {
        expect(scrubText(`a ${scheme}${c}TAILV12345678 b`)).toBe(`a ${R} b`);
        expect(unframed(`a ${scheme}${c}TAILV12345678 b`)).toBe(`a ${R} b`);
        expect(framed(`a ${scheme}${c}TAILV12345678 b`)).toBe(`a ${R} b`);
      }
    });
  }
  test("U+FEFF and U+200B are not whitespace", () => {
    for (const code of [0xfeff, 0x200b]) {
      const text = `a Bearer${String.fromCharCode(code)}TAILV12345678 b`;
      expect(scrubText(text)).toBe(text);
    }
  });
});

describe("value shapes use Python's Unicode word boundary", () => {
  const key = "sk-TAILV1234567890abcdefgh", aws = "AKIAABCDEFGHIJKLMNOP", jwt = "eyJhbGciOiJIUzI1.eyJzdWIiOiIxMjM0.SflKxwRJSMeKKF2Q";
  // Combining marks, zero-width characters and connector punctuation are not word characters; letters, digits and '_' are.
  // Python's \w is a letter, a number or '_': connector punctuation (U+203F, U+2040, U+FE33, U+FF3F) is not a word character.
  const open = [0x0300, 0x0301, 0x0308, 0x0336, 0x200b, 0x200c, 0x200d, 0x2060, 0xfeff, 0x203f, 0x2040, 0xfe33, 0xff3f].map(code => String.fromCharCode(code));
  const word = ["\u00e9", "\u03a9", "\u0663", "\u4e94", "_"];
  for (const [name, token] of [["sk", key], ["aws", aws], ["jwt", jwt]]) {
    test(`${name}: a non-word neighbour leaves the boundary`, () => {
      for (const c of open) expect(scrubText(`x ${c}${token}${c} y`)).toBe(`x ${c}${R}${c} y`);
    });
    test(`${name}: a word neighbour before the token keeps it`, () => {
      for (const c of word) expect(scrubText(`x ${c}${token} y`)).toBe(`x ${c}${token} y`);
    });
  }
  test("aws: a word neighbour after the token keeps it", () => {
    for (const c of word) expect(scrubText(`x ${aws}${c} y`)).toBe(`x ${aws}${c} y`);
  });
});

describe("value shapes fold case as Python's IGNORECASE does", () => {
  test("U+017F, U+0131 and U+0130 stand for s and i in the scheme", () => {
    for (const scheme of ["Ba\u017fic", "Bas\u0131c", "Bas\u0130c", "BASIC", "ba\u017fic"]) expect(scrubText(`a ${scheme} TAILV12345678 b`)).toBe(`a ${R} b`);
  });
  test("U+212A, U+017F, U+0130 and U+0131 are value characters", () => {
    for (const c of ["\u212a", "\u017f", "\u0130", "\u0131"]) {
      expect(scrubText(`a Bearer ${c}TAILV1234567 b`)).toBe(`a ${R} b`);
      expect(scrubText(`a Bearer TAILV1234567${c}TAILW b`)).toBe(`a ${R} b`);
    }
  });
});

describe("a private key is read over the whole text", () => {
  const pem = "-----BEGIN PRIVATE KEY-----\ndYPdktpZJJyJZO2LZsS4TAILKEY\n-----END PRIVATE KEY-----";
  test("a leading '/' or '?' before the header", () => {
    for (const lead of ["/", "?"]) expect(scrubText(`log: ${lead}${pem}\n.`)).toBe(`log: ${lead}${R}\n.`);
  });
  test("a truncated key reaches the end across a URL-like token", () => {
    expect(scrubText("-----BEGIN PRIVATE KEY-----\nTAILKEY\n-----END PRIVATE ?TAIL2")).toBe(R);
    expect(scrubText("-----BEGIN PRIVATE KEY-----\nTAILKEY\n-----END PRIVATE /TAIL2.")).toBe(R);
  });
});

describe("structured JSON is the whole input", () => {
  const inputs = ["{} password=TAILV tail", "[1] token: TAILV", '{"a":1} trailing password=TAILV', '{"a":"x"} password=TAILV', "{}api_keydata:TAILVTAILV"];
  for (const input of inputs) {
    test(JSON.stringify(input), () => {
      for (const out of [scrubText(input), scrubAttribute(input), unframed(input), framed(input), framed(input, [], "response_body"), framed({ a: input })]) {
        expect(JSON.stringify(out)).not.toContain("TAILV");
      }
    });
  }
});

describe("known secrets apply one at a time, longest first", () => {
  const first = "AAAAAAAABBBB", second = "BBBBCCCCCCCC", both = [first, second];
  const texts = [
    "password=AAAAAAAABBBBCCCCCCCC tail", "password: AAAAAAAABBBB BBBBCCCCCCCC tail", "token=BBBBCCCCCCCCAAAAAAAABBBB x",
    "api_key=AAAAAAAABBBBBBBBCCCCCCCC", "x password=BBBBCCCCCCCC AAAAAAAABBBB", "{x password=AAAAAAAABBBBCCCCCCCC tail",
  ];
  for (const text of texts) {
    test(JSON.stringify(text), () => {
      for (const out of [scrubText(text, both), unframed(text, both), framed(text, both)]) {
        expect(out).not.toMatch(/AAAAAAAA|BBBB|CCCCCCCC/);
        expect(out).toContain(R);
      }
    });
  }
  test("a pair value that starts with the first secret is redacted to its end", () => {
    expect(unframed("password=AAAAAAAABBBBCCCCCCCC tail", both)).toBe(`password=${R}`);
  });
});

describe("a value shape next to a known secret in text that is not JSON", () => {
  const secret = "AAAAAAAABBBB";
  const shapes: [string, string][] = [["sk-", "sk-NSfQ2qdKveUYu28QKRZGTAIL1"], ["AKIA", "AKIAHVXTCJATAIL23456"], ["Bearer", "Bearer QxN3XKGLMTAIL3"]];
  for (const [name, shape] of shapes) {
    for (const prefix of ["{", "[", "{x"]) {
      test(`${name} after prefix ${prefix}, both orders, framed and unframed`, () => {
        // A letter before a shape is a word character, so only a prefix without one lets the shape lead.
        const cases: [string, string][] = [[`${prefix}${secret}${shape} tail`, `${prefix}${R}${R} tail`], [`${prefix} ${secret}${shape} tail`, `${prefix} ${R}${R} tail`]];
        for (const [text, expected] of cases) {
          for (const out of [scrubText(text, [secret]), unframed(text, [secret]), framed(text, [secret])]) expect(out).toBe(expected);
        }
        // The shape first: a shape that takes the secret's characters into its own run (sk-, Bearer) leaves one placeholder, which is
        // more redaction than the reference's two; none of either value stays.
        for (const text of [`${prefix} ${shape}${secret} tail`, `${prefix}${shape}${secret} tail`]) {
          for (const out of [scrubText(text, [secret]), unframed(text, [secret]), framed(text, [secret])]) {
            if (prefix === "{x" && text.startsWith("{x" + shape.slice(0, 2))) continue; // a letter before the shape: no word boundary
            expect(out).not.toMatch(/TAIL|AAAAAAAA/);
            expect(out).toMatch(/^\S* ?(<redacted>)+ tail$/);
          }
        }
      });
    }
  }
});

describe("a structured string is read again with the URL catalog", () => {
  // Census cases c052421, c091796, c115856, c185936 and c191756: a JSON object in a string whose leaf holds a credential URL
  // pair, then U+00A0 and a second URL. The reference's text for each, after JSON.
  const cases: [string, string, string][] = [
    ["c052421", "https://example.invalid/v1/api_key%3DTAIL0052421/keep\u00a0https://example.invalid/v1;Authorization: TAIL0052421B", "https://example.invalid/v1/api_key%3D<redacted> <redacted>"],
    ["c091796", "https://example.invalid/v1;api_key%09%3DBearer TAIL0091796\u00a0https://example.invalid/v1;Authorization: TAIL0091796B", "https://example.invalid/v1;api_key%09%3D<redacted> <redacted>"],
    ["c115856", 'https://example.invalid/?pwd%20="TAIL0115856"\u00a0https://example.invalid/v1;api_key= TAIL0115856B', "https://example.invalid/?pwd%20=<redacted>"],
    ["c185936", "https://example.invalid/?pwd=TAIL0185936&keep\u00a0https://example.invalid/v1;password= TAIL0185936B", "https://example.invalid/?pwd=<redacted>"],
    ["c191756", "https://example.invalid/?a=1?pwd%C2%A0=TAIL0191756#keep\u00a0https://example.invalid/v1;api_key= TAIL0191756B", "https://example.invalid/?a=1?pwd%C2%A0=<redacted>"],
  ];
  for (const [id, leaf, expected] of cases) {
    test(id, () => {
      const message = JSON.stringify({ log: leaf }).replace(/:/, ": ");
      const out = framed({ message }, [], "") as { message: string };
      expect(JSON.parse(out.message)).toEqual({ log: expected });
      expect(JSON.parse(scrubText(message))).toEqual({ log: expected });
    });
  }
  test("a leaf that holds a second URL with a non-credential parameter exports no text of it", () => {
    const leaf = "https://example.invalid/?pwd=TAILX&keep\u00a0https://example.invalid/v1?model=TAILPLAIN";
    const out = framed({ message: JSON.stringify({ log: leaf }) }, []) as { message: string };
    expect(JSON.parse(out.message)).toEqual({ log: "https://example.invalid/?pwd=<redacted>" });
  });
});

describe("the '://' userinfo lookup is linear", () => {
  // 1.6M characters of '://'-heavy text: each input stays under its bound and a quarter of it costs about a quarter.
  const shapes: [string, (n: number) => string][] = [
    ["bare '://'", n => "://".repeat(n / 3)],
    ["a path after each '://'", n => "x://a/b ".repeat(n / 8)],
    ["a word without '@'", n => "ftp://user:pass/path?x=1 ".repeat(n / 25)],
    ["'://' and '@' in one long word", n => "a://b@c".repeat(n / 7)],
    ["'@' only before the window", n => "@" + "://q".repeat(n / 4)],
  ];
  // A text that its own rewrites make longer than limits.max_text_chars fails closed with the text budget error.
  const time = (text: string) => {
    const start = performance.now();
    try { scrubText(text); } catch (error) { expect(String(error)).toMatch(/Privacy (text|structure) budget/); }
    return performance.now() - start;
  };
  for (const [name, make] of shapes) {
    test(`${name}: 1.6M characters`, () => {
      time(make(100000));
      const quarter = Math.min(time(make(400000)), time(make(400000))), whole = time(make(1600000));
      process.stderr.write(`://-heavy ${name}: 400K ${quarter.toFixed(0)} ms, 1.6M ${whole.toFixed(0)} ms\n`);
      expect(whole).toBeLessThan(10000);
      // Linear growth is a factor of 4; a quadratic one is 16.
      expect(whole).toBeLessThan(Math.max(quarter, 50) * 9);
    });
  }
});
