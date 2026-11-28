// @traceai/auto scrubs values through the fi-core privacy runtime (src/wire/privacy-runtime.ts re-exports
// @traceai/fi-core/privacy, i.e. fi-core/dist), so rebuild fi-core after changing its scrubber.
import { afterAll, describe, expect, it } from "@jest/globals";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { scrubAttribute, scrubText } from "../src/wire/privacy-runtime";

// Shared constants: reuse these in other SDK ports.
const N = 10000;
const BUDGET_MS = 100;
const MARKER = "<redacted>"; // privacy.json credentials.replacement

const ADVERSARIAL: [string, string][] = [
  ["jwt: eyJ- x N (dot-free)", "eyJ-".repeat(N)],
  ["jwt: eyJ- x N + '.'", "eyJ-".repeat(N) + "."],
  ["jwt: eyJ.eyJ- x 5000 (two dots)", "eyJ.eyJ-".repeat(5000)],
  ["jwt: eyJ-.-  segments x N", "eyJ-aaaaaaaa.".repeat(N)],
  ["sk-: sk- x N", "sk-".repeat(N)],
  ["sk-: sk-aaaa x N then '!'", "sk-aaaaaaaaaaaaaaaaaaa".repeat(N) + "!"],
  ["aws: AKIA x N", "AKIA".repeat(N)],
  ["aws: ASIA x N", "ASIA".repeat(N)],
  ["authorization: Bearer x N", "Bearer ".repeat(N)],
  ["authorization: Basic x N", "Basic ".repeat(N)],
  ["private-key: BEGIN PRIVATE KEY x N", "-----BEGIN PRIVATE KEY-----".repeat(N)],
  ["private-key: BEGIN RSA PRIVATE KEY x N + newline", "-----BEGIN RSA PRIVATE KEY-----\n".repeat(N)],
];

const timings: string[] = [];
const now = () => Number(process.hrtime.bigint()) / 1e6;
// The fastest of three runs, so one slow run (a collection, a busy machine) does not decide.
function time(fn: (s: string) => string, input: string): number {
  let best = Infinity;
  for (let run = 0; run < 3; run++) {
    const start = now();
    fn(input);
    best = Math.min(best, now() - start);
  }
  return best;
}

describe("credential value shapes are bounded-time (public scrubText / scrubAttribute)", () => {
  // Warm up the JIT and regex compilation on a tiny input.
  for (let i = 0; i < 20; i++) { scrubText("eyJ-sk-AKIABearer x"); scrubAttribute("eyJ-sk-AKIABearer x"); }

  for (const [name, input] of ADVERSARIAL) {
    it(`scrubText finishes ${name} within ${BUDGET_MS} ms`, () => {
      const ms = time(scrubText, input);
      timings.push(`scrubText | ${name} | len=${input.length} | ${ms.toFixed(1)} ms`);
      expect(ms).toBeLessThan(BUDGET_MS);
    });
    it(`scrubAttribute finishes ${name} within ${BUDGET_MS} ms`, () => {
      const ms = time(scrubAttribute, input);
      timings.push(`scrubAttribute | ${name} | len=${input.length} | ${ms.toFixed(1)} ms`);
      expect(ms).toBeLessThan(BUDGET_MS);
    });
  }
  afterAll(() => { process.stderr.write("TIMINGS\n" + timings.join("\n") + "\n"); });
});

const b64 = (o: object) => Buffer.from(JSON.stringify(o)).toString("base64url");
const HEADER = b64({ alg: "HS256", typ: "JWT" });
const PAYLOAD = b64({ sub: "1234567890", name: "Fake User", iat: 1700000000 });
const jwt = (sig: string) => `${HEADER}.${PAYLOAD}.${sig}`;
const SIG = "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c";

describe("real-looking JWTs are replaced whole", () => {
  const cases: [string, string, string][] = [
    ["plain", jwt(SIG), MARKER],
    ["signature ending in '-'", jwt("SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5-"), MARKER],
    ["signature ending in '_'", jwt("SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5_"), MARKER],
    ["signature starting with '-'", jwt("-flKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"), MARKER],
    ["directly preceded by '-'", "x-" + jwt(SIG), "x-" + MARKER],
    ["in surrounding text", `token is ${jwt(SIG)} ok.`, `token is ${MARKER} ok.`],
    ["RS256 long", `${b64({ alg: "RS256", kid: "k1" })}.${PAYLOAD}.${"A1b2-C3d4_".repeat(30)}`, MARKER],
  ];
  for (const [name, input, expected] of cases) {
    for (const [fn, label] of [[scrubText, "scrubText"], [scrubAttribute, "scrubAttribute"]] as const) {
      it(`${label}: ${name}`, () => {
        // Exact equality: the whole token became MARKER, nothing of it is left.
        expect(fn(input)).toBe(expected);
      });
    }
  }
});

// ---- Large real tokens must be redacted whole (the jwt shape is unbounded). ----
// Deterministic pseudo-random base64url text (mixed characters, not a single repeated run).
function b64run(length: number, seed: number): string {
  const alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";
  let x = seed >>> 0, out = "";
  for (let i = 0; i < length; i++) { x = (Math.imul(x, 1664525) + 1013904223) >>> 0; out += alphabet[x >>> 26]; }
  return out;
}
describe("large JWTs (header > 1024 or payload > 8192 characters) are replaced whole", () => {
  const bigHeader = "eyJ" + b64run(2000, 1);           // ~2 KB x5c-like header
  const bigPayload = "eyJ" + b64run(12288, 2);          // 12 KB payload
  const cases: [string, string][] = [
    ["2 KB header", `${bigHeader}.${PAYLOAD}.${SIG}`],
    ["12 KB payload", `${HEADER}.${bigPayload}.${SIG}`],
  ];
  for (const [name, token] of cases) {
    for (const [fn, label] of [[scrubText, "scrubText"], [scrubAttribute, "scrubAttribute"]] as const) {
      it(`${label}: ${name}`, () => { expect(fn(token)).toBe(MARKER); });
      it(`${label}: Bearer + ${name}`, () => { expect(fn("Bearer " + token)).toBe("Bearer " + MARKER); });
    }
  }
});

// ---- Differential: scrubText against the plain unbounded reference regex. ----
describe("differential: jwt value shape equals the unbounded reference regex", () => {
  const R = /\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}/g;
  const PIECES = ["eyJ", "eyJaaaaaaaa", "aaaaaaaa", ".", "-", "_", " ", "x", "é", "eyJ-", "-eyJ", "..", "a.", ".a"];
  const catalog = JSON.parse(readFileSync(resolve(__dirname, "../src/wire/privacy.json"), "utf8"));
  const entry = catalog.credentials.value_shapes.find((item: any) => item.id.includes("jwt"));
  const catalogShape = new RegExp(entry.regex, entry.flags + "g");
  let seed = 20261121;
  const rnd = (n: number) => { seed = (Math.imul(seed, 1664525) + 1013904223) >>> 0; return Math.floor((seed / 4294967296) * n); };
  let hits = 0, compared = 0, rounds = 0;
  // privacy-runtime keeps its value-shape pass private, and scrubText also runs a bare-media pass that
  // rewrites some strings to "<omitted: N base64 chars>". So (a) every round checks the catalog's jwt
  // shape against R, and (b) rounds where no other pass fired check scrubText (the linear scan) against R.
  const check = (s: string) => {
    rounds++;
    const expected = s.replace(R, MARKER);
    if (expected !== s) hits++;
    if (s.replace(catalogShape, MARKER) !== expected) throw new Error(`catalog mismatch for ${JSON.stringify(s.slice(0, 200))}`);
    const actual = scrubText(s);
    if (actual.includes("<omitted:")) return; // a different pass (bare media) fired
    compared++;
    if (actual !== expected) throw new Error(`mismatch for ${JSON.stringify(s.slice(0, 200))}: expected ${JSON.stringify(expected.slice(0, 200))}, got ${JSON.stringify(actual.slice(0, 200))}`);
  };
  it("20000 seeded random strings (1-20 pieces) match", () => {
    for (let round = 0; round < 20000; round++) {
      const count = 1 + rnd(20);
      let s = "";
      for (let i = 0; i < count; i++) s += PIECES[rnd(PIECES.length)];
      check(s);
    }
    expect(compared).toBeGreaterThan(10000);
    expect(hits).toBeGreaterThan(50); // the corpus really exercises redactions
  });
  it("near-misses: a JWT-like run broken at every position, 3 ways, with several contexts", () => {
    const bases = [
      `${HEADER}.${PAYLOAD}.${SIG}`,
      "eyJaaaaaaaa.bbbbbbbb.cccccccc",
      "eyJ-aaaaaaa.b-b_bbbbb.c_c-cccc",
      "eyJaaaaaaaa.bbbbbbbb.cccccccc.eyJdddddddd.eeeeeeee.ffffffff",
    ];
    const contexts: [string, string][] = [["", ""], ["token ", ""], ["x-", " y."], ["é", ""], ["a.", ".b"], ["-", "-"], ["_", "_"], [".", "."]];
    const before = rounds;
    for (const base of bases) {
      for (const [pre, post] of contexts) {
        check(pre + base + post);
        for (let i = 0; i < base.length; i++) {
          for (const edit of ["!", ".", "-", "_", " ", "é"]) check(pre + base.slice(0, i) + edit + base.slice(i + 1) + post); // replace one char
          check(pre + base.slice(0, i) + base.slice(i + 1) + post);                 // delete one char (a dot removed when base[i] is '.')
          check(pre + base.slice(0, i) + "." + base.slice(i) + post);               // dot added
          check(pre + base.slice(0, i) + "eyJ" + base.slice(i) + post);             // a second header start inserted
        }
      }
    }
    expect(rounds - before).toBeGreaterThan(5000);
  });
  it("runs mixing '.', '-' and '_' around eyJ starts (exhaustive, short alphabet)", () => {
    const alphabet = ["eyJ", "aaaaaaaa", ".", "-", "_", "é"];
    const before = rounds;
    const walk = (s: string, depth: number) => { check(s); if (depth < 6) for (const a of alphabet) walk(s + a, depth + 1); };
    walk("", 0);
    expect(rounds - before).toBeGreaterThan(5000);
  });
  it("non-ASCII letters adjacent to eyJ (JS \\b is ASCII-only, so they do not block a match)", () => {
    const token = "eyJaaaaaaaa.bbbbbbbb.cccccccc";
    for (const s of ["é" + token, "á" + token, token + "é", "é" + token + "é", "aeyJaaaaaaaa.bbbbbbbb.cccccccc", "_" + token, "-" + token]) {
      check(s);
      expect(scrubText(s)).toBe(s.replace(R, MARKER));
    }
    expect(scrubText("é" + token)).toBe("é" + MARKER);
  });
  it("one large input (2080000 characters) matches the backtracking reference", () => {
    // Many tokens, near-misses and long runs; the reference finishes this size because it has no 'eyJ-' x N pathology.
    const parts: string[] = [];
    let size = 0;
    while (size < 2080000) {
      const kind = rnd(6);
      const part = kind === 0 ? `${HEADER}.${PAYLOAD}.${SIG}`
        : kind === 1 ? "eyJ" + b64run(1 + rnd(3000), rnd(1 << 30)) + "." + b64run(rnd(3000), rnd(1 << 30)) + "." + b64run(rnd(3000), rnd(1 << 30))
        : kind === 2 ? "eyJaaaaaaaa.bbbbbbbb." + (rnd(2) ? "ccccccc" : "cccccccc")
        : kind === 3 ? b64run(1 + rnd(200), rnd(1 << 30)) + "." + b64run(rnd(200), rnd(1 << 30))
        : kind === 4 ? "éeyJaaaaaaaa.bbbbbbbb.cccccccc"
        : "word ";
      parts.push(part); size += part.length;
    }
    const s = parts.join(rnd(2) ? " " : "-");
    // Random runs can contain 'sk-...' or 'AKIA...' and the sk-/aws shapes run before jwt, so the reference
    // applies every catalog shape in order, with the jwt entry replaced by the unbounded R.
    const everyShape = (text: string, jwt: RegExp) => catalog.credentials.value_shapes.reduce(
      (acc: string, item: any) => acc.replace(item.id.includes("jwt") ? jwt : new RegExp(item.regex, item.flags + "g"), MARKER), text);
    const expected = everyShape(s, R);
    const start = Date.now();
    const actual = scrubText(s);
    const elapsed = Date.now() - start;
    process.stderr.write(`LARGE differential: ${s.length} chars, scrubText ${elapsed} ms\n`);
    expect(everyShape(s, catalogShape)).toBe(expected);
    if (!actual.includes("<omitted:")) expect(actual).toBe(expected);
    expect(actual).not.toBe(s);
    expect(actual.includes("<omitted:")).toBe(false); // so the comparison above really ran
    expect(elapsed).toBeLessThan(2000);
  });
});
