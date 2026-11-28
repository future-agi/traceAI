import { describe, expect, test } from "@jest/globals";
import { scrubAttribute } from "../privacy-runtime";

// Every join of a quoted JSON-string attribute that hides each registered value it holds, whole, stays linear: where the registered
// values the attribute holds would take more than eight passes over it (and over 2^20 characters in all), that attribute alone is the
// quoted placeholder. With 64 registered values held, four times the attribute takes at most about five and a half times as long (the
// least of two measurements of each ratio, each the best of three calls). The number held stays the same as the text grows: the read of
// the string itself searches each registered value it holds once (charged to the privacy budget), so a number that grows with the text
// grows that read too. Planted markers only.

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

describe("a long quoted attribute whose registered values are spelled only in the string it encodes", () => {
  // Each registered value holds a tab that the raw bytes write as \t, so only the read of the string finds them; 64 of them (prefixes
  // of one another).
  const shared = "PLNTpFx7P6cSlmdwE5p9eB8Kq\tz5TwEu7" + randomText(320).replaceAll(" ", "x");
  const registry = Array.from({ length: 256 }, (_, i) => shared.slice(0, shared.length - i));
  const make = (n: number): [string, string[]] => {
    const prefix = `"start ${JSON.stringify(shared).slice(1, -1)} `, unit = "quiet repeated x3\\t", suffix = ' end"', length = n - prefix.length - suffix.length;
    return [prefix + unit.repeat(Math.floor(length / unit.length)) + " ".repeat(length % unit.length) + suffix, registry.slice(0, 64)];
  };
  test("the attribute is the quoted placeholder, and four times the text takes at most about five and a half times as long", () => {
    for (const ratio of ratios(sizes.map(make))) expect(ratio).toBeLessThan(5.5);
    for (const n of sizes) expect(scrubAttribute(...make(n))).toBe("\"<redacted>\"");
    expect(scrubAttribute(make(65_536)[0], registry.slice(0, 4))).toMatch(/^"start <redacted> quiet repeated x3\\tquiet/);
    expect(scrubAttribute("\"note PLNTq7Wd\\u0032Hn4Kc9Lz end\"", ["PLNTq7Wd2Hn4Kc9Lz"])).toBe("\"note <redacted> end\"");
  }, 300_000);
});

describe("a long quoted attribute holding a JSON text and many registered values the raw bytes hold", () => {
  // The JSON text is written with a space after ':', so the read of the string writes it again and cannot be placed in the raw bytes;
  // the reads are then joined as the strings they encode, with every occurrence of each registered value hidden. 64 of them are held.
  const shared = "PLNTqW3eR5tY7uI9oP1aS2dF" + randomText(320).replaceAll(" ", "x");
  const registry = Array.from({ length: 256 }, (_, i) => shared.slice(0, shared.length - i));
  const make = (n: number): [string, string[]] => {
    const prefix = `"{\\"k\\": \\"start ${shared} `, unit = "quiet repeated x3 ", suffix = ' end\\"}"', length = n - prefix.length - suffix.length;
    return [prefix + unit.repeat(Math.floor(length / unit.length)) + " ".repeat(length % unit.length) + suffix, registry.slice(0, 64)];
  };
  test("the attribute is the quoted placeholder, and four times the text takes at most about five and a half times as long", () => {
    for (const ratio of ratios(sizes.map(make))) expect(ratio).toBeLessThan(5.5);
    for (const n of sizes) expect(scrubAttribute(...make(n))).toBe("\"<redacted>\"");
    expect(scrubAttribute(make(65_536)[0], registry.slice(0, 4))).not.toContain(shared.slice(0, 16));
    expect(scrubAttribute("\"{\\\"k\\\": \\\"note PLNTq7Wd2Hn4Kc9Lz end\\\"}\"", ["PLNTq7Wd2Hn4Kc9Lz"])).toBe("\"{\\\"k\\\":\\\"note <redacted> end\\\"}\"");
  }, 300_000);
});
