import { describe, expect, test } from "@jest/globals";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { indexPrivacyHints, scrubText, scrubValue } from "../privacy-runtime";

// The shared privacy bundle (spec/auto-instrumentation/privacy-fixtures.json) runs here per rule group, with no
// SDK-specific expectation, override or skip list. Each case must give its expected output, and the output must be stable
// when scrubbed again. Values are read from the bundle; none is copied into this file.
const bundle = JSON.parse(readFileSync(resolve(__dirname, "../../../../../spec/auto-instrumentation/privacy-fixtures.json"), "utf8"));

interface Case { name: string; covers: string[]; input: { site: string; path: string; value: unknown; siblings?: Record<string, unknown>; known_secrets?: string[] }; expected: unknown }

function run(fixture: Case, value: unknown): unknown {
  const { path, siblings, known_secrets, site } = fixture.input;
  const hints = indexPrivacyHints(Object.entries(siblings ?? {}));
  return scrubValue(value, known_secrets ?? [], path, 0, false, undefined, hints, site !== "free_text_urls");
}

function failures(select: (covers: string[]) => boolean): { count: number; failed: string[] } {
  const cases = (bundle.cases as Case[]).filter(fixture => select(fixture.covers));
  const failed: string[] = [];
  for (const fixture of cases) {
    let ok = false;
    try {
      ok = JSON.stringify(run(fixture, fixture.input.value)) === JSON.stringify(fixture.expected)
        && JSON.stringify(run(fixture, fixture.expected)) === JSON.stringify(fixture.expected);
    } catch { ok = false; }
    if (!ok) failed.push(fixture.name);
  }
  return { count: cases.length, failed };
}

const GROUPS: [string, (covers: string[]) => boolean][] = [
  ["1 pair rule: every credential pair is replaced to the true end of its value", c => c.includes("credential-pair-rule")],
  ["2 whole-URL values: the pair rule skips only the spans the catalog replaced", c => c.includes("whole-url-pair-rule") || c.includes("omission-spans")],
  ["3 URL catalog: fragment, second '?', path segment and prose URL pairs", c => c.some(tag => ["url-fragment-pairs", "url-path-pairs", "url-query-boundaries", "url-apostrophes", "url-prefixed-prose", "credential-like-host"].includes(tag))],
  ["4 vocabulary: the shared credential words at every site", c => c.some(tag => tag.startsWith("vocabulary:"))],
  ["5 JSON quoted names: string, scalar and prose values in every quote spelling", c => c.includes("json-quoted-name-ruling") || c.includes("quoted-name-spellings")],
  ["6 map keys: keys take the export text rules, collisions keep one key", c => c.includes("map-keys")],
  ["7 depth: a name of more than max_depth components exports the placeholder", c => c.includes("name-path-budget")],
  ["8 bounds: a name past its window or a name after too many '?' is undecided", c => c.includes("bounded-names")],
  ["10 gaps: only up to 8 plain spaces form a readable name-to-separator gap", c => c.includes("pair-name-gaps") || c.includes("encoded-gaps")],
  ["11 path segments: every '=' and %3D in a URL path segment is read", c => c.includes("url-path-every-pair")],
  ["12 name read-back: a name that ends at a percent escape is decoded once", c => c.includes("encoded-name-read-back") || c.includes("escaped-name-decode")],
  ["13 --name and single-dash flags: gaps, line breaks and encoded controls", c => c.some(tag => tag.startsWith("dashes-") || tag === "single-dash-flags")],
  ["14 quoted names with '=' or %3D are pairs", c => c.includes("quoted-name-equals")],
  ["15 data URIs: a continuation line needs trailing base64 padding", c => c.includes("data-uri-continuation")],
];

describe("shared privacy spec v4, per rule group", () => {
  test.each(GROUPS)("%s", (_name, select) => {
    const { count, failed } = failures(select);
    expect(count).toBeGreaterThan(0);
    expect(failed).toEqual([]);
  });

  test("every bundle case passes, with no SDK-specific expectation", () => {
    const { count, failed } = failures(() => true);
    expect(count).toBe(bundle.cases.length);
    expect(failed).toEqual([]);
  });
});

describe("URL and data URI tokens end at the reference whitespace", () => {
  // U+0085 and U+001C-U+001F are whitespace in the shared reference (JS \s leaves them out), so a data URI after one is read.
  test.each(["\u0085", "\u001c", "\u001f"])("a data URI after %j is omitted", space => {
    expect(scrubValue(`?${space}data:,payload`)).toBe(`?${space}<omitted: data URI>`);
    expect(scrubText(`?${space}data:,payload`)).toBe(`?${space}<omitted: data URI>`);
  });
});

describe("9 performance: the pair rule and the URL catalog stay linear", () => {
  // 1.6M characters of adversarial input each: many '?', '=' and long names, in prose and in whole URL values.
  const N = 1_600_000;
  const fill = (unit: string, prefix = "") => (prefix + unit.repeat(Math.ceil(N / unit.length))).slice(0, N);
  const question = "?" + "n".repeat(120) + "?".repeat(9) + "token" + "=" + "v".repeat(40) + ",";
  const BOUND_MS = 10_000;
  const inputs: [string, string, boolean, string][] = [
    ["'?' marks and long names in prose", fill(question, "log "), false, "v".repeat(40)],
    ["'?' marks and long names in a whole URL value", fill(question, "https://example.invalid/p?" + "a=b&".repeat(10) + "q="), true, "v".repeat(40)],
    ["many short pairs in prose", fill("a=b,token=" + "w".repeat(12) + ","), false, "w".repeat(12)],
    ["names past their window before '='", fill("x".repeat(200) + "=" + "w".repeat(12) + " "), false, "w".repeat(12)],
    ["--name and line breaks", fill("--token\n" + "w".repeat(12) + " "), false, "w".repeat(12)],
    ["path segment and fragment pairs in a whole URL value", fill(";a=1;password=" + "w".repeat(12), "https://example.invalid/p"), true, "w".repeat(12)],
  ];
  // A text whose passes rewrite it is read again by the owner scan; past limits.max_text_chars in total that fails closed
  // with the text budget error (the export boundary keeps no part of the value), as before this rule.
  test.each(inputs)("%s: finishes under the bound and keeps no credential value", (_kind, input, framed, value) => {
    const started = process.hrtime.bigint();
    let clean: unknown, error: unknown;
    try { clean = framed ? scrubValue(input) : scrubText(input); } catch (caught) { error = caught; }
    const elapsed = Number(process.hrtime.bigint() - started) / 1e6;
    if (error !== undefined) expect(String(error)).toMatch(/Privacy text budget/);
    else { expect(typeof clean).toBe("string"); expect(clean as string).not.toContain(value); }
    expect(elapsed).toBeLessThan(BOUND_MS);
  });
});
