import * as wire from "../src/wire/privacy-runtime";
import * as core from "@traceai/fi-core/privacy";

function check(condition: boolean, id: string): void {
  if (!condition) throw new Error(id);
}
const freeText = (value: string, secrets: readonly string[] = []): string =>
  wire.scrubValue(value, secrets, "", 0, false, undefined, undefined, false);
const secret = "SYNTHETIC_PRIVATE_20261030";

test("R-F1-S6-FRAGMENT-2", () => {
  const value = "see #fragment?token=ordinary next";
  check(wire.scrubValue === core.scrubValue, "R-F1-S6-FRAGMENT-2-SHARED");
  check(freeText(value) === value, "R-F1-S6-FRAGMENT-2-BYTES");
  check(wire.scrubText(value) === value, "R-F1-S6-FRAGMENT-2-TEXT");
  check(freeText(freeText(value)) === value, "R-F1-S6-FRAGMENT-2-IDEMPOTENT");
});

test("R-F1-S6-URL-QUERY-FAMILY", () => {
  // The reviewed url-query-boundaries family, with explicit neighbouring
  // query masking controls rather than only the formerly failing safe string.
  const cases: readonly (readonly [string, string])[] = [
    ["see https://example.invalid/#fragment?token=ordinary next", "see https://example.invalid/#fragment?token=ordinary next"],
    ["see https://example.invalid/?model=keep#fragment?token=ordinary next", "see https://example.invalid/?model=keep#fragment?token=ordinary next"],
    ["see /path#fragment?token=ordinary next", "see /path#fragment?token=ordinary next"],
    [`see /path?token?filter=${secret}#fragment?token=ordinary next`, "see /path?token?filter=<redacted>#fragment?token=ordinary next"],
    ["https://example.invalid/?model=why?token=ordinary", "https://example.invalid/?model=why?token=ordinary"],
    [`https://example.invalid/#fragment?token=ordinary then https://example.invalid/?token=${secret}`, "https://example.invalid/#fragment?token=ordinary then https://example.invalid/?token=<redacted>"],
    [`see #fragment?token=ordinary then https://example.invalid/path?token=${secret}&access_token=${secret}&model=keep#anchor next`, "see #fragment?token=ordinary then https://example.invalid/path?token=<redacted>&access_token=<redacted>&model=keep#anchor next"],
    [`see ?token=${secret}&model=keep next`, "see ?token=<redacted>&model=keep next"],
  ];
  for (const [value, expected] of cases) {
    const clean = freeText(value);
    check(clean === expected, "R-F1-S6-URL-QUERY-FAMILY-BYTES");
    check(!clean.includes('"'), "R-F1-S6-URL-QUERY-FAMILY-NO-QUOTES");
    check(freeText(clean) === expected, "R-F1-S6-URL-QUERY-FAMILY-IDEMPOTENT");
  }
  for (const name of ["client_secret?tag", "password?foo", "token?filter"]) {
    const value = `https://example.invalid/?${name}=${secret}&model=keep#fragment?token=ordinary`;
    const expected = `https://example.invalid/?${name}=<redacted>&model=keep#fragment?token=ordinary`;
    check(freeText(value) === expected, "R-F1-S6-URL-QUERY-FAMILY-LITERAL-QUESTION");
    check(wire.scrubValue(value) === expected, "R-F1-S6-URL-QUERY-FAMILY-FRAMED");
  }
});

test("R-F1-S6-APOSTROPHE-AND-ENCODED-QUERY", () => {
  const cases: readonly (readonly [string, string])[] = [
    [`see https://example.invalid/?client%2Esecret=${secret}&model=keep next`, "see https://example.invalid/?client%2Esecret=<redacted>&model=keep next"],
    [`see /path?token'filter=${secret}&model=keep#fragment?token=ordinary next`, "see /path?token'filter=<redacted>&model=keep#fragment?token=ordinary next"],
    [`'https://example.invalid/?token='${secret}'`, "'https://example.invalid/?token=<redacted>'"],
    ["https://example.invalid/?model=author's-model&key_id=public", "https://example.invalid/?model=author's-model&key_id=public"],
    ["https://example.invalid/?token=&model=keep", "https://example.invalid/?token=<redacted>&model=keep"],
    ["https://example.invalid/?token=<redacted>&model=keep", "https://example.invalid/?token=<redacted>&model=keep"],
  ];
  for (const [value, expected] of cases) {
    check(freeText(value) === expected, "R-F1-S6-APOSTROPHE-AND-ENCODED-QUERY-BYTES");
    check(freeText(expected) === expected, "R-F1-S6-APOSTROPHE-AND-ENCODED-QUERY-IDEMPOTENT");
  }
});

test("R-F1-S6-FRAGMENT-SECRET-POLICIES", () => {
  check(freeText(`see #fragment?token=${secret} next`, [secret]) === "see #fragment?token=<redacted> next", "R-F1-S6-FRAGMENT-SECRET-POLICIES-EXACT");
  const shaped = "sk-" + "SYNTHETIC".repeat(4);
  check(freeText(`see #fragment?token=${shaped} next`) === "see #fragment?token=<redacted> next", "R-F1-S6-FRAGMENT-SECRET-POLICIES-SHAPE");
  check(freeText("see #fragment?blob=data:audio/wav;base64,AQIDBA== next") === "see #fragment?blob=<omitted: 8 base64 chars> next", "R-F1-S6-FRAGMENT-SECRET-POLICIES-MEDIA");
  check(wire.scrubValue("#fragment?token=ordinary", [], "token") === "<redacted>", "R-F1-S6-FRAGMENT-SECRET-POLICIES-STRUCTURED");
});

test("R-F1-S6-FRAGMENT-ASSIGNMENT-BOUNDARY", () => {
  const after = freeText(`see #fragment?token=ordinary next token=${secret}`);
  check(after.startsWith("see #fragment?token=ordinary next ") && !after.includes(secret) && after.includes("<redacted>"), "R-F1-S6-FRAGMENT-ASSIGNMENT-BOUNDARY-TRAILING");
  const nonReference = freeText(`label#token=${secret}`);
  check(!nonReference.includes(secret) && nonReference.includes("<redacted>"), "R-F1-S6-FRAGMENT-ASSIGNMENT-BOUNDARY-NONREFERENCE");
  const assigned = freeText(`token=https://example.invalid/#fragment?token=${secret}`);
  check(!assigned.includes(secret) && !assigned.includes("example.invalid") && assigned.includes("<redacted>"), "R-F1-S6-FRAGMENT-ASSIGNMENT-BOUNDARY-PRIVATE-KEY");
});
