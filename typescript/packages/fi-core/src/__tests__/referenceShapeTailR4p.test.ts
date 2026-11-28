import { describe, expect, test } from "@jest/globals";
import { scrubAttribute, scrubText, scrubValue } from "../privacy-runtime";

// A registered value only adds redaction: a value shape is read on the original text, so a registered value that starts inside a
// shape's run (it holds the run's tail, or a part between its ends) never leaves the run's head shown. Planted markers only; every
// host is under .invalid.

/** The 4-character runs of `shape` that occur once in `input` and that `output` shows. */
const shapeRunsShown = (input: string, output: string, shape: string) => {
  const shown: string[] = [];
  for (let i = 0; i + 4 <= shape.length; i++) {
    const run = shape.slice(i, i + 4);
    if (input.indexOf(run) === input.lastIndexOf(run) && output.includes(run)) shown.push(run);
  }
  return shown;
};
const apis: [string, (value: string, secrets: string[]) => string][] = [
  ["scrubText", (value, secrets) => scrubText(value, secrets)],
  ["scrubValue", (value, secrets) => scrubValue(value, secrets)],
  ["scrubAttribute", (value, secrets) => scrubAttribute(value, secrets)],
];

describe("an sk-proj value followed by a fragment access_token and a quoted password pair, with three overlapping registered values", () => {
  // Both References (d2dab4e8 and the in-tree 40c761f6) give '<redacted> https://frag.invalid/p#access_token=<redacted>'.
  const shape = "sk-proj-PLNTZfn9p6SdxzQYNEp9jJHW5ZLp";
  const input = `${shape} https://frag.invalid/p#access_token=PLNTNIu9eGGwiZ6OlkyGLJFvRa23 = "password": "PLNTt2BUJKEQxGsrnk2T7RSgg1qk"`;
  const secrets = ["dxzQYNEp9jJHW5ZLp https://frag.invalid/p#access_token=PLNTNIu9eG", '9eGGwiZ6OlkyGLJFvRa23 = "password": "PLNTt2', 'GGwiZ6OlkyGLJFvRa23 = "pas'];
  test.each(apis)("%s shows no 4 characters of the sk-proj value or of either credential", (_, scrub) => {
    const output = scrub(input, secrets);
    expect(shapeRunsShown(input, output, shape)).toEqual([]);
    expect(shapeRunsShown(input, output, "PLNTNIu9eGGwiZ6OlkyGLJFvRa23")).toEqual([]);
    expect(shapeRunsShown(input, output, "PLNTt2BUJKEQxGsrnk2T7RSgg1qk")).toEqual([]);
  });
  test.each(apis)("%s with the first registered value alone hides the sk-proj value whole", (_, scrub) => {
    expect(shapeRunsShown(input, scrub(input, secrets.slice(0, 1)), shape)).toEqual([]);
  });
});

describe("a value shape followed by text, with registered values that start inside the shape's run", () => {
  // A fixed generator (a linear congruential sequence), so the cases are the same on every run.
  let state = 9502;
  const next = (n: number) => { state = (state * 1103515245 + 12345) % 2147483648; return state % n; };
  const ALNUM = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789", UPPER = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789";
  const word = (n: number, alphabet = ALNUM) => { let out = ""; for (let i = 0; i < n; i++) out += alphabet[next(alphabet.length)]; return out; };
  const shapes: Record<string, () => string> = {
    "sk-": () => "sk-PLNT" + word(22),
    "sk-proj-": () => "sk-proj-PLNT" + word(22),
    AKIA: () => "AKIA" + word(16, UPPER),
    JWT: () => "eyJ" + word(12) + "." + word(14) + "." + word(16),
    Bearer: () => "Bearer PLNT" + word(22),
  };
  const contexts: Record<string, () => string> = {
    "plain words": () => " and the PLNTword" + word(8) + " stays here",
    "a URL fragment access_token": () => " https://frag.invalid/p#access_token=PLNT" + word(20),
    "a URL query api_key": () => " https://e.invalid/p?q=1&api_key=PLNT" + word(14) + "&b=2 log",
    "a quoted password pair": () => ' = "password": "PLNT' + word(22) + '"',
    "a fragment access_token and a quoted password pair": () => " https://frag.invalid/p#access_token=PLNT" + word(20) + ' = "password": "PLNT' + word(22) + '"',
  };
  const cases: [string, string, string[], string][] = [];
  for (const [shapeName, makeShape] of Object.entries(shapes)) for (const [contextName, makeContext] of Object.entries(contexts)) {
    for (let i = 0; i < 8; i++) {
      const shape = makeShape(), context = makeContext(), prefix = i % 4 < 2 ? "" : "log: ", text = prefix + shape + context;
      // The first registered value starts inside the shape's run and runs into the context; the others overlap it.
      const from = prefix.length + (shapeName === "Bearer" ? 8 : 4) + next(shape.length - (shapeName === "Bearer" ? 9 : 5));
      const to = prefix.length + shape.length + 4 + next(context.length - 4);
      const secrets = [text.slice(from, to)];
      if (i % 3 >= 1) {
        const a = Math.max(from + 1, to - 12) + next(Math.max(1, to - 2 - Math.max(from + 1, to - 12))), b = Math.min(text.length, to + 6 + next(24));
        secrets.push(text.slice(a, b));
        if (i % 3 === 2) { const a2 = a + 1 + next(Math.max(1, Math.min(b - 2, a + 6) - a)); secrets.push(text.slice(a2, Math.min(text.length, a2 + 8 + next(16)))); }
      }
      cases.push([`${shapeName} then ${contextName} #${i}`, text, secrets.filter(secret => secret.length >= 8), shape]);
    }
  }
  test.each(cases)("%s: no 4 characters of the shape's run are shown", (_, text, secrets, shape) => {
    for (const [, scrub] of apis) expect(shapeRunsShown(text, scrub(text, secrets), shape)).toEqual([]);
  });
});

describe("a registered value between the two ends of a shape's run", () => {
  const cases: [string, string, string[], string][] = [
    ["sk- value, a value in its middle", "key sk-PLNTabcdefghijklmnopqrstuv end", ["cdefghijkl"], "sk-PLNTabcdefghijklmnopqrstuv"],
    ["sk-proj- value, a value in its middle", "sk-proj-PLNTq8Wn3Kd7Rt2Yx5Zb9Mv1 then", ["Kd7Rt2Yx5Z"], "sk-proj-PLNTq8Wn3Kd7Rt2Yx5Zb9Mv1"],
    ["AKIA value, a value in its middle", "aws AKIAPLNTQ7RS2TUV9WXY here", ["TQ7RS2TU"], "AKIAPLNTQ7RS2TUV9WXY"],
    ["JWT, a value in its middle segment", "jwt eyJPLNTabcdefgh.PLNTijklmnopqr.PLNTstuvwxyz12 ok", ["PLNTijklmno"], "eyJPLNTabcdefgh.PLNTijklmnopqr.PLNTstuvwxyz12"],
    ["Bearer value, a value in its middle", "Authorization Bearer PLNTa1b2c3d4e5f6g7h8i9 rest", ["c3d4e5f6g7"], "Bearer PLNTa1b2c3d4e5f6g7h8i9"],
    ["sk- value, two values in its middle", "sk-PLNTzyxwvutsrqponmlkjihg x", ["wvutsrqpon", "rqponmlkji"], "sk-PLNTzyxwvutsrqponmlkjihg"],
  ];
  test.each(cases)("%s: no 4 characters of the shape's run are shown", (_, text, secrets, shape) => {
    for (const [, scrub] of apis) expect(shapeRunsShown(text, scrub(text, secrets), shape)).toEqual([]);
  });
});

describe("a registered value that occurs before the shape, and one that starts inside the shape's run", () => {
  const cases: [string, string, string[], string][] = [
    ["an earlier value, then an sk- value", "PLNTearlyR8n2 note sk-PLNTabcdefghijklmnopqrstuv https://frag.invalid/p#access_token=PLNTw7Qk3Lp9", ["PLNTearlyR8n2", "pqrstuv https://frag.invalid/p#access_token=PLNTw7"], "sk-PLNTabcdefghijklmnopqrstuv"],
    ["two sk- values, each with a value starting inside it", "sk-PLNTzyxwvutsrqponmlkjihg and sk-PLNTqwertyuiopasdfghjklzx end", ["ponmlkjihg and", "fghjklzx end"], "sk-PLNTqwertyuiopasdfghjklzx"],
  ];
  test.each(cases)("%s: no 4 characters of the shape's run are shown", (_, text, secrets, shape) => {
    for (const [, scrub] of apis) expect(shapeRunsShown(text, scrub(text, secrets), shape)).toEqual([]);
  });
});

describe("a registered value that starts one character after a shape run's start, or ends one character before its end", () => {
  // The words around the run stay; the run is one placeholder.
  const cases: [string, string, string[]][] = [
    ["sk- value, a value from its second character", "keep sk-PLNTQ7sPLNTK9RcM8HzB4tV6 ; keep", ["k-PLNTQ7"]],
    ["sk- value, a longer value from its second character", "keep sk-PLNTabcdefghijklmnopqrstuv ; keep", ["k-PLNTabcd"]],
    ["AKIA value, a value from its second character", "keep AKIAPLNT01234567890Z ; keep", ["KIAPLNT0"]],
    ["AKIA value, a value from its last character on", "keep AKIAPLNT01234567890Z:PLNTXYZ ; keep", ["Z:PLNTXYZ"]],
    ["sk- value, a value from its last character on", "keep sk-PLNTabcdefghijklmnopqrstuv ; keep", ["v ; ke"]],
    ["Bearer value, a value from its second character", "keep Bearer PLNTa1b2c3d4e5f6g7h8i9 ; keep", ["earer PLNTa1"]],
    ["Bearer value, a value from its last character on", "keep Bearer PLNTa1b2c3d4e5f6g7h8i9 ; keep", ["9 ; kee"]],
    ["JWT, a value from its second character", "keep eyJPLNTabcdefgh.PLNTijklmnopqr.PLNTstuvwxyz12 ; keep", ["yJPLNTabc"]],
  ];
  test.each(cases)("%s", (_, text, secrets) => {
    for (const [, scrub] of apis) expect(scrub(text, secrets)).toBe("keep <redacted> ; keep");
  });
});
