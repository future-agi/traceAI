import { describe, expect, test } from "@jest/globals";
import { scrubAttribute } from "../privacy-runtime";

// The reads of a quoted JSON-string attribute given registered values stay linear: four times the attribute takes at most about five and
// a half times as long (the least of two measurements of each ratio, each the best of three calls). Planted markers only.

const A = "PLNT7r4Sm6Kq9Jx2";
/** Deterministic text of letters and digits with a space every 101 characters (xorshift). */
const randomText = (n: number) => {
  let out = "", seed = 987654321;
  const alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789";
  for (let i = 0; i < n; i++) { seed ^= seed << 13; seed ^= seed >>> 17; seed ^= seed << 5; out += i % 101 === 100 ? " " : alphabet[(seed >>> 0) % alphabet.length]; }
  return out;
};
const best = (run: () => unknown) => {
  let low = Infinity;
  for (let r = 0; r < 3; r++) { const started = process.hrtime.bigint(); run(); low = Math.min(low, Number(process.hrtime.bigint() - started) / 1e6); }
  return low;
};
const ratios = (inputs: [string, string[]][]) => {
  scrubAttribute(...inputs[0]);
  return inputs.slice(1).map((input, i) => {
    let ratio = Infinity;
    for (let attempt = 0; attempt < 2 && ratio >= 5.5; attempt++) ratio = Math.min(ratio, best(() => scrubAttribute(...input)) / Math.max(best(() => scrubAttribute(...inputs[i])), 1));
    return ratio;
  });
};
const sizes = [65_536, 262_144, 1_048_576];

describe("a quoted attribute whose read of the string cannot be placed, at growing lengths", () => {
  // A JSON text inside the string, written with a space after ':' and every third character spelled as a \u escape: the read of the
  // string writes it again, so its reads are decided by the windows each hides.
  const filler = (() => { const raw = randomText(sizes[2]); let out = ""; for (let i = 0; out.length < sizes[2]; i++) out += i % 3 ? raw[i] : "\\u" + raw.charCodeAt(i).toString(16).padStart(4, "0"); return out; })();
  const make = (n: number): [string, string[]] => {
    const prefix = `"{\\"k\\": \\"${A}\\\\u00e9 `, suffix = ` pxx\\"}"`;
    let body = filler.slice(0, n - prefix.length - suffix.length);
    const cut = body.lastIndexOf("\\");
    if (body.length - cut < 6) body = body.slice(0, cut) + " ".repeat(body.length - cut);
    return [prefix + body + suffix, [A]];
  };
  test("four times the attribute takes at most about five and a half times as long", () => {
    for (const ratio of ratios(sizes.map(make))) expect(ratio).toBeLessThan(5.5);
    expect(scrubAttribute(...make(sizes[0]))).not.toContain(A);
  }, 300_000);
});

describe("a long quoted attribute that holds many registered values", () => {
  // Registered values that are prefixes of one spelling of the raw bytes (with a literal backslash-t), 4, 16 and 64 of them as the text
  // grows: holding more than eight of them in so long a text makes this attribute alone the quoted placeholder, in time linear in it.
  const shared = "PLNTpFx7P6cSlmdwE5p9eB8Kq\\tz5TwEu7" + randomText(320).replaceAll(" ", "x");
  const registry = Array.from({ length: 256 }, (_, i) => shared.slice(0, shared.length - i));
  const make = (n: number): [string, string[]] => {
    const prefix = `"start ${shared} `, unit = "quiet repeated x3\\t", suffix = ' end"', length = n - prefix.length - suffix.length;
    return [prefix + unit.repeat(Math.floor(length / unit.length)) + " ".repeat(length % unit.length) + suffix, registry.slice(0, n / 16_384)];
  };
  test("the attribute is the quoted placeholder and four times the text takes at most about five and a half times as long", () => {
    for (const ratio of ratios(sizes.map(make))) expect(ratio).toBeLessThan(5.5);
    expect(scrubAttribute(...make(sizes[1]))).toBe("\"<redacted>\"");
    expect(scrubAttribute("\"note PLNTq7Wd2Hn4Kc9Lz end\"", ["PLNTq7Wd2Hn4Kc9Lz"])).toBe("\"note <redacted> end\"");
  }, 300_000);
});
