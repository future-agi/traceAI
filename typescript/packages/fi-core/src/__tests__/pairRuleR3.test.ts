import { describe, expect, test } from "@jest/globals";
import { indexPrivacyHints, scrubText, scrubValue } from "../privacy-runtime";

// Phase 2 round 3: the URL and data URI catalog runs first, then known secrets one at a time (longest first, equal lengths by
// code point), then value shapes once over the assembled text, then the pair rule. Each expected text is what Reference.scrub
// (spec/auto-instrumentation/generate_privacy_fixtures.py) gives for the same input, except in NAMES_AND_DELIMITERS: a registered value
// only adds redaction, so one that takes out a credential name, its delimiter or the head of its value leaves that value hidden (and
// is hidden whole with it) where the reference shows the rest of the value or the head of the registered value. Test values are
// placeholders by shape.
const none = indexPrivacyHints([]);
const framedValue = (value: unknown, secrets: string[]) => scrubValue(value, secrets, "", 0, false, undefined, none, true);
const proseValue = (value: unknown, secrets: string[]) => scrubValue(value, secrets, "", 0, false, undefined, none, false);
type Row = [string, string, string[], boolean, unknown];
// [name, input, known secrets, framed site, expected]
function check(rows: Row[]): void {
  const failed: string[] = [];
  for (const [name, value, secrets, framed, expected] of rows) {
    const got = framed ? framedValue(value, secrets) : proseValue(value, secrets);
    if (got !== expected) failed.push(`${name} (${framed ? "framed" : "prose"}): ${JSON.stringify(value)} gave ${JSON.stringify(got)}, expected ${JSON.stringify(expected)}`);
  }
  expect(failed).toEqual([]);
}

const CATALOG_FIRST: Row[] = [
  ["data-uri-secret-body", "x data:text/plain;base64,SECRETSECRET1 y", ["SECRETSECRET1"], false, "x <omitted: 13 base64 chars> y"],
  ["data-uri-secret-straddle", "x data:text/plain;base64,SECRETSECRET1 y", ["text/plain;base64,SECR"], false, "x <omitted: 13 base64 chars> y"],
  ["data-uri-in-url-query", "see https://h.example/p?img=data:text/plain;base64,SECRETSECRET1&a=b next", ["SECRETSECRET1"], false, "see https://h.example/p?img=<omitted: 13 base64 chars>&a=b next"],
  ["data-uri-secret-whole-url", "https://h.example/p?img=data:text/plain;base64,SECRETSECRET1&a=b", ["SECRETSECRET1"], false, "https://h.example/p?img=<omitted: 13 base64 chars>&a=b"],
  ["data-uri-secret-body", "x data:text/plain;base64,SECRETSECRET1 y", ["SECRETSECRET1"], true, "x <omitted: 13 base64 chars> y"],
  ["data-uri-secret-straddle", "x data:text/plain;base64,SECRETSECRET1 y", ["text/plain;base64,SECR"], true, "x <omitted: 13 base64 chars> y"],
  ["data-uri-in-url-query", "see https://h.example/p?img=data:text/plain;base64,SECRETSECRET1&a=b next", ["SECRETSECRET1"], true, "see https://h.example/p?img=<omitted: 13 base64 chars>&a=b next"],
  ["data-uri-secret-whole-url", "https://h.example/p?img=data:text/plain;base64,SECRETSECRET1&a=b", ["SECRETSECRET1"], true, "https://h.example/p?img=<omitted: 13 base64 chars>&a=b"],
];
const NAMES_AND_DELIMITERS: Row[] = [
  ["url-secret-over-name-password", "https://h.example/p?password=TAILVALUE1&a=b", ["ord=TAILVA"], false, "https://h.example/p?passw<redacted>"],
  ["prose-url-secret-over-name-password", "see https://h.example/p?password=TAILVALUE1&a=b next", ["ord=TAILVA"], false, "see https://h.example/p?passw<redacted>"],
  ["url-secret-over-eq-password", "https://h.example/p?password=TAILVALUE1&a=b", ["=TAILVAL"], false, "https://h.example/p?password<redacted>"],
  ["prose-secret-over-eq-password", "see password=TAILVALUE1 next", ["=TAILVAL"], false, "see password<redacted>"],
  ["prose-secret-over-name-password", "see password=TAILVALUE1 next", ["ord=TAILVA"], false, "see passw<redacted>"],
  ["prose-secret-over-colon-password", "see password: TAILVALUE1 next", ["rd: TAILVA"], false, "see passwo<redacted>"],
  ["prose-secret-whole-name-password", "see password=TAILVALUE1 next", ["password="], false, "see <redacted><redacted>"],
  ["url-secret-over-name-password", "https://h.example/p?password=TAILVALUE1&a=b", ["ord=TAILVA"], true, "https://h.example/p?password=<redacted>&a=b"],
  ["prose-url-secret-over-name-password", "see https://h.example/p?password=TAILVALUE1&a=b next", ["ord=TAILVA"], true, "see https://h.example/p?passw<redacted>"],
  ["url-secret-over-eq-password", "https://h.example/p?password=TAILVALUE1&a=b", ["=TAILVAL"], true, "https://h.example/p?password=<redacted>&a=b"],
  ["prose-secret-over-eq-password", "see password=TAILVALUE1 next", ["=TAILVAL"], true, "see password<redacted>"],
  ["prose-secret-over-name-password", "see password=TAILVALUE1 next", ["ord=TAILVA"], true, "see passw<redacted>"],
  ["prose-secret-over-colon-password", "see password: TAILVALUE1 next", ["rd: TAILVA"], true, "see passwo<redacted>"],
  ["prose-secret-whole-name-password", "see password=TAILVALUE1 next", ["password="], true, "see <redacted><redacted>"],
  ["url-secret-over-name-eq-whole-password", "https://h.example/p?password=TAILVALUE1", ["password=TAILVALUE1"], false, "https://h.example/p?password=<redacted>"],
  ["url-secret-over-name-eq-whole-password", "https://h.example/p?password=TAILVALUE1", ["password=TAILVALUE1"], true, "https://h.example/p?password=<redacted>"],
  ["url-secret-over-name-api_key", "https://h.example/p?api_key=TAILVALUE1&a=b", ["key=TAILVA"], false, "https://h.example/p?api_<redacted>"],
  ["prose-url-secret-over-name-api_key", "see https://h.example/p?api_key=TAILVALUE1&a=b next", ["key=TAILVA"], false, "see https://h.example/p?api_<redacted>"],
  ["url-secret-over-eq-api_key", "https://h.example/p?api_key=TAILVALUE1&a=b", ["=TAILVAL"], false, "https://h.example/p?api_key<redacted>"],
  ["prose-secret-over-eq-api_key", "see api_key=TAILVALUE1 next", ["=TAILVAL"], false, "see api_key<redacted>"],
  ["prose-secret-over-name-api_key", "see api_key=TAILVALUE1 next", ["key=TAILVA"], false, "see api_<redacted>"],
  ["prose-secret-over-colon-api_key", "see api_key: TAILVALUE1 next", ["ey: TAILVA"], false, "see api_k<redacted>"],
  ["prose-secret-whole-name-api_key", "see api_key=TAILVALUE1 next", ["api_key="], false, "see <redacted><redacted>"],
  ["url-secret-over-name-api_key", "https://h.example/p?api_key=TAILVALUE1&a=b", ["key=TAILVA"], true, "https://h.example/p?api_key=<redacted>&a=b"],
  ["prose-url-secret-over-name-api_key", "see https://h.example/p?api_key=TAILVALUE1&a=b next", ["key=TAILVA"], true, "see https://h.example/p?api_<redacted>"],
  ["url-secret-over-eq-api_key", "https://h.example/p?api_key=TAILVALUE1&a=b", ["=TAILVAL"], true, "https://h.example/p?api_key=<redacted>&a=b"],
  ["prose-secret-over-eq-api_key", "see api_key=TAILVALUE1 next", ["=TAILVAL"], true, "see api_key<redacted>"],
  ["prose-secret-over-name-api_key", "see api_key=TAILVALUE1 next", ["key=TAILVA"], true, "see api_<redacted>"],
  ["prose-secret-over-colon-api_key", "see api_key: TAILVALUE1 next", ["ey: TAILVA"], true, "see api_k<redacted>"],
  ["prose-secret-whole-name-api_key", "see api_key=TAILVALUE1 next", ["api_key="], true, "see <redacted><redacted>"],
  ["url-secret-over-name-eq-whole-api_key", "https://h.example/p?api_key=TAILVALUE1", ["api_key=TAILVALUE1"], false, "https://h.example/p?api_key=<redacted>"],
  ["url-secret-over-name-eq-whole-api_key", "https://h.example/p?api_key=TAILVALUE1", ["api_key=TAILVALUE1"], true, "https://h.example/p?api_key=<redacted>"],
  ["url-secret-over-name-token", "https://h.example/p?token=TAILVALUE1&a=b", ["ken=TAILVA"], false, "https://h.example/p?to<redacted>"],
  ["prose-url-secret-over-name-token", "see https://h.example/p?token=TAILVALUE1&a=b next", ["ken=TAILVA"], false, "see https://h.example/p?to<redacted>"],
  ["url-secret-over-eq-token", "https://h.example/p?token=TAILVALUE1&a=b", ["=TAILVAL"], false, "https://h.example/p?token<redacted>"],
  ["prose-secret-over-eq-token", "see token=TAILVALUE1 next", ["=TAILVAL"], false, "see token<redacted>"],
  ["prose-secret-over-name-token", "see token=TAILVALUE1 next", ["ken=TAILVA"], false, "see to<redacted>"],
  ["prose-secret-over-colon-token", "see token: TAILVALUE1 next", ["en: TAILVA"], false, "see tok<redacted>"],
  ["prose-secret-whole-name-token", "see token=TAILVALUE1 next", ["token="], false, "see token=<redacted>"],
  ["url-secret-over-name-token", "https://h.example/p?token=TAILVALUE1&a=b", ["ken=TAILVA"], true, "https://h.example/p?token=<redacted>&a=b"],
  ["prose-url-secret-over-name-token", "see https://h.example/p?token=TAILVALUE1&a=b next", ["ken=TAILVA"], true, "see https://h.example/p?to<redacted>"],
  ["url-secret-over-eq-token", "https://h.example/p?token=TAILVALUE1&a=b", ["=TAILVAL"], true, "https://h.example/p?token=<redacted>&a=b"],
  ["prose-secret-over-eq-token", "see token=TAILVALUE1 next", ["=TAILVAL"], true, "see token<redacted>"],
  ["prose-secret-over-name-token", "see token=TAILVALUE1 next", ["ken=TAILVA"], true, "see to<redacted>"],
  ["prose-secret-over-colon-token", "see token: TAILVALUE1 next", ["en: TAILVA"], true, "see tok<redacted>"],
  ["prose-secret-whole-name-token", "see token=TAILVALUE1 next", ["token="], true, "see token=<redacted>"],
  ["url-secret-over-name-eq-whole-token", "https://h.example/p?token=TAILVALUE1", ["token=TAILVALUE1"], false, "https://h.example/p?token=<redacted>"],
  ["url-secret-over-name-eq-whole-token", "https://h.example/p?token=TAILVALUE1", ["token=TAILVALUE1"], true, "https://h.example/p?token=<redacted>"],
  ["url-secret-over-name-secret", "https://h.example/p?secret=TAILVALUE1&a=b", ["ret=TAILVA"], false, "https://h.example/p?sec<redacted>"],
  ["prose-url-secret-over-name-secret", "see https://h.example/p?secret=TAILVALUE1&a=b next", ["ret=TAILVA"], false, "see https://h.example/p?sec<redacted>"],
  ["url-secret-over-eq-secret", "https://h.example/p?secret=TAILVALUE1&a=b", ["=TAILVAL"], false, "https://h.example/p?secret<redacted>"],
  ["prose-secret-over-eq-secret", "see secret=TAILVALUE1 next", ["=TAILVAL"], false, "see secret<redacted>"],
  ["prose-secret-over-name-secret", "see secret=TAILVALUE1 next", ["ret=TAILVA"], false, "see sec<redacted>"],
  ["prose-secret-over-colon-secret", "see secret: TAILVALUE1 next", ["et: TAILVA"], false, "see secr<redacted>"],
  ["prose-secret-whole-name-secret", "see secret=TAILVALUE1 next", ["secret="], false, "see secret=<redacted>"],
  ["url-secret-over-name-secret", "https://h.example/p?secret=TAILVALUE1&a=b", ["ret=TAILVA"], true, "https://h.example/p?secret=<redacted>&a=b"],
  ["prose-url-secret-over-name-secret", "see https://h.example/p?secret=TAILVALUE1&a=b next", ["ret=TAILVA"], true, "see https://h.example/p?sec<redacted>"],
  ["url-secret-over-eq-secret", "https://h.example/p?secret=TAILVALUE1&a=b", ["=TAILVAL"], true, "https://h.example/p?secret=<redacted>&a=b"],
  ["prose-secret-over-eq-secret", "see secret=TAILVALUE1 next", ["=TAILVAL"], true, "see secret<redacted>"],
  ["prose-secret-over-name-secret", "see secret=TAILVALUE1 next", ["ret=TAILVA"], true, "see sec<redacted>"],
  ["prose-secret-over-colon-secret", "see secret: TAILVALUE1 next", ["et: TAILVA"], true, "see secr<redacted>"],
  ["prose-secret-whole-name-secret", "see secret=TAILVALUE1 next", ["secret="], true, "see secret=<redacted>"],
  ["url-secret-over-name-eq-whole-secret", "https://h.example/p?secret=TAILVALUE1", ["secret=TAILVALUE1"], false, "https://h.example/p?secret=<redacted>"],
  ["url-secret-over-name-eq-whole-secret", "https://h.example/p?secret=TAILVALUE1", ["secret=TAILVALUE1"], true, "https://h.example/p?secret=<redacted>"],
  ["userinfo-secret-over-at", "https://user:pw12345@h.example/p", ["pw12345@h.example"], false, "https://<redacted>@h.example/p"],
  ["userinfo-secret-over-at-prose", "see https://user:pw12345@h.example/p next", ["pw12345@h.example"], false, "see https://<redacted>@h.example/p next"],
  ["userinfo-secret-over-colon", "https://user:pw12345@h.example/p?x=1", ["user:pw12345"], false, "https://<redacted>@h.example/p?x=1"],
  ["userinfo-secret-over-colon-prose", "see https://user:pw12345@h.example/p?x=1 next", ["user:pw12345"], false, "see https://<redacted>@h.example/p?x=1 next"],
  ["userinfo-secret-at-only", "see https://user:pw12345@h.example/p next", ["5@h.example"], false, "see https://<redacted>@h.example/p next"],
  ["userinfo-secret-over-at", "https://user:pw12345@h.example/p", ["pw12345@h.example"], true, "https://<redacted>@h.example/p"],
  ["userinfo-secret-over-at-prose", "see https://user:pw12345@h.example/p next", ["pw12345@h.example"], true, "see https://<redacted>@h.example/p next"],
  ["userinfo-secret-over-colon", "https://user:pw12345@h.example/p?x=1", ["user:pw12345"], true, "https://<redacted>@h.example/p?x=1"],
  ["userinfo-secret-over-colon-prose", "see https://user:pw12345@h.example/p?x=1 next", ["user:pw12345"], true, "see https://<redacted>@h.example/p?x=1 next"],
  ["userinfo-secret-at-only", "see https://user:pw12345@h.example/p next", ["5@h.example"], true, "see https://<redacted>@h.example/p next"],
];
const SHAPES_ONCE: Row[] = [
  ["shape-sk-before-https:", "x sk-TAILV1234567890abcdefghhttps://h.example/p y", [], false, "x <redacted>://h.example/p y"],
  ["shape-sk-before-https:", "x sk-TAILV1234567890abcdefghhttps://h.example/p y", [], true, "x <redacted>://h.example/p y"],
  ["shape-sk-before-https:-secret", "x sk-TAILV1234567890abcdefghhttps://h.example/p y", ["SECRETSECRET1"], false, "x <redacted>://h.example/p y"],
  ["shape-sk-before-http:/", "x sk-TAILV1234567890abcdefghhttp://h.example/p y", [], false, "x <redacted>://h.example/p y"],
  ["shape-sk-before-http:/", "x sk-TAILV1234567890abcdefghhttp://h.example/p y", [], true, "x <redacted>://h.example/p y"],
  ["shape-sk-before-http:/-secret", "x sk-TAILV1234567890abcdefghhttp://h.example/p y", ["SECRETSECRET1"], false, "x <redacted>://h.example/p y"],
  ["shape-sk-before-data:,", "x sk-TAILV1234567890abcdefghdata:,abc y", [], false, "x <redacted>:,abc y"],
  ["shape-sk-before-data:,", "x sk-TAILV1234567890abcdefghdata:,abc y", [], true, "x <redacted>:,abc y"],
  ["shape-sk-before-data:,-secret", "x sk-TAILV1234567890abcdefghdata:,abc y", ["SECRETSECRET1"], false, "x <redacted>:,abc y"],
  ["shape-sk-before-https:", "x sk-TAILV1234567890abcdefghhttps://h.example/p?a=b y", [], false, "x <redacted>://h.example/p?a=b y"],
  ["shape-sk-before-https:", "x sk-TAILV1234567890abcdefghhttps://h.example/p?a=b y", [], true, "x <redacted>://h.example/p?a=b y"],
  ["shape-sk-before-https:-secret", "x sk-TAILV1234567890abcdefghhttps://h.example/p?a=b y", ["SECRETSECRET1"], false, "x <redacted>://h.example/p?a=b y"],
  ["shape-aws-before-https:", "x AKIAABCDEFGHIJKLMNOPhttps://h.example/p y", [], false, "x AKIAABCDEFGHIJKLMNOPhttps://h.example/p y"],
  ["shape-aws-before-https:", "x AKIAABCDEFGHIJKLMNOPhttps://h.example/p y", [], true, "x AKIAABCDEFGHIJKLMNOPhttps://h.example/p y"],
  ["shape-aws-before-https:-secret", "x AKIAABCDEFGHIJKLMNOPhttps://h.example/p y", ["SECRETSECRET1"], false, "x AKIAABCDEFGHIJKLMNOPhttps://h.example/p y"],
  ["shape-aws-before-http:/", "x AKIAABCDEFGHIJKLMNOPhttp://h.example/p y", [], false, "x AKIAABCDEFGHIJKLMNOPhttp://h.example/p y"],
  ["shape-aws-before-http:/", "x AKIAABCDEFGHIJKLMNOPhttp://h.example/p y", [], true, "x AKIAABCDEFGHIJKLMNOPhttp://h.example/p y"],
  ["shape-aws-before-http:/-secret", "x AKIAABCDEFGHIJKLMNOPhttp://h.example/p y", ["SECRETSECRET1"], false, "x AKIAABCDEFGHIJKLMNOPhttp://h.example/p y"],
  ["shape-aws-before-data:,", "x AKIAABCDEFGHIJKLMNOPdata:,abc y", [], false, "x AKIAABCDEFGHIJKLMNOPdata:,abc y"],
  ["shape-aws-before-data:,", "x AKIAABCDEFGHIJKLMNOPdata:,abc y", [], true, "x AKIAABCDEFGHIJKLMNOPdata:,abc y"],
  ["shape-aws-before-data:,-secret", "x AKIAABCDEFGHIJKLMNOPdata:,abc y", ["SECRETSECRET1"], false, "x AKIAABCDEFGHIJKLMNOPdata:,abc y"],
  ["shape-aws-before-https:", "x AKIAABCDEFGHIJKLMNOPhttps://h.example/p?a=b y", [], false, "x AKIAABCDEFGHIJKLMNOPhttps://h.example/p?a=b y"],
  ["shape-aws-before-https:", "x AKIAABCDEFGHIJKLMNOPhttps://h.example/p?a=b y", [], true, "x AKIAABCDEFGHIJKLMNOPhttps://h.example/p?a=b y"],
  ["shape-aws-before-https:-secret", "x AKIAABCDEFGHIJKLMNOPhttps://h.example/p?a=b y", ["SECRETSECRET1"], false, "x AKIAABCDEFGHIJKLMNOPhttps://h.example/p?a=b y"],
  ["shape-jwt-before-https:", "x eyJhbGciOiJIUzI1.eyJzdWIiOiIxMjM0.SflKxwRJSMeKKF2Qhttps://h.example/p y", [], false, "x <redacted>://h.example/p y"],
  ["shape-jwt-before-https:", "x eyJhbGciOiJIUzI1.eyJzdWIiOiIxMjM0.SflKxwRJSMeKKF2Qhttps://h.example/p y", [], true, "x <redacted>://h.example/p y"],
  ["shape-jwt-before-https:-secret", "x eyJhbGciOiJIUzI1.eyJzdWIiOiIxMjM0.SflKxwRJSMeKKF2Qhttps://h.example/p y", ["SECRETSECRET1"], false, "x <redacted>://h.example/p y"],
  ["shape-jwt-before-http:/", "x eyJhbGciOiJIUzI1.eyJzdWIiOiIxMjM0.SflKxwRJSMeKKF2Qhttp://h.example/p y", [], false, "x <redacted>://h.example/p y"],
  ["shape-jwt-before-http:/", "x eyJhbGciOiJIUzI1.eyJzdWIiOiIxMjM0.SflKxwRJSMeKKF2Qhttp://h.example/p y", [], true, "x <redacted>://h.example/p y"],
  ["shape-jwt-before-http:/-secret", "x eyJhbGciOiJIUzI1.eyJzdWIiOiIxMjM0.SflKxwRJSMeKKF2Qhttp://h.example/p y", ["SECRETSECRET1"], false, "x <redacted>://h.example/p y"],
  ["shape-jwt-before-data:,", "x eyJhbGciOiJIUzI1.eyJzdWIiOiIxMjM0.SflKxwRJSMeKKF2Qdata:,abc y", [], false, "x <redacted>:,abc y"],
  ["shape-jwt-before-data:,", "x eyJhbGciOiJIUzI1.eyJzdWIiOiIxMjM0.SflKxwRJSMeKKF2Qdata:,abc y", [], true, "x <redacted>:,abc y"],
  ["shape-jwt-before-data:,-secret", "x eyJhbGciOiJIUzI1.eyJzdWIiOiIxMjM0.SflKxwRJSMeKKF2Qdata:,abc y", ["SECRETSECRET1"], false, "x <redacted>:,abc y"],
  ["shape-jwt-before-https:", "x eyJhbGciOiJIUzI1.eyJzdWIiOiIxMjM0.SflKxwRJSMeKKF2Qhttps://h.example/p?a=b y", [], false, "x <redacted>://h.example/p?a=b y"],
  ["shape-jwt-before-https:", "x eyJhbGciOiJIUzI1.eyJzdWIiOiIxMjM0.SflKxwRJSMeKKF2Qhttps://h.example/p?a=b y", [], true, "x <redacted>://h.example/p?a=b y"],
  ["shape-jwt-before-https:-secret", "x eyJhbGciOiJIUzI1.eyJzdWIiOiIxMjM0.SflKxwRJSMeKKF2Qhttps://h.example/p?a=b y", ["SECRETSECRET1"], false, "x <redacted>://h.example/p?a=b y"],
];
const EQUAL_LENGTH: Row[] = [
  ["equal-length-overlap", "xx abcdefgh12ijklm yy", ["abcdefgh12", "fgh12ijklm"], false, "xx <redacted>ijklm yy"],
  ["equal-length-overlap-3", "zz abcdefgh12ijklm12345678 yy", ["abcdefgh12", "fgh12ijklm", "ijklm12345"], false, "zz <redacted><redacted>678 yy"],
  ["equal-length-overlap", "xx abcdefgh12ijklm yy", ["abcdefgh12", "fgh12ijklm"], true, "xx <redacted>ijklm yy"],
  ["equal-length-overlap-3", "zz abcdefgh12ijklm12345678 yy", ["abcdefgh12", "fgh12ijklm", "ijklm12345"], true, "zz <redacted><redacted>678 yy"],
  ["equal-length-overlap", "xx abcdefgh12ijklm yy", ["fgh12ijklm", "abcdefgh12"], false, "xx <redacted>ijklm yy"],
  ["equal-length-overlap-3", "zz abcdefgh12ijklm12345678 yy", ["fgh12ijklm", "abcdefgh12", "ijklm12345"], false, "zz <redacted><redacted>678 yy"],
  ["equal-length-overlap", "xx abcdefgh12ijklm yy", ["fgh12ijklm", "abcdefgh12"], true, "xx <redacted>ijklm yy"],
  ["equal-length-overlap-3", "zz abcdefgh12ijklm12345678 yy", ["fgh12ijklm", "abcdefgh12", "ijklm12345"], true, "zz <redacted><redacted>678 yy"],
];

describe("known secrets are read after the URL and data URI catalog", () => {
  test("a known secret inside a data URI leaves the data URI omitted", () => check(CATALOG_FIRST));
  test("a known secret over a credential name, an '=' or ':' delimiter, or a userinfo '@' leaves the pair and userinfo rules in place", () => check(NAMES_AND_DELIMITERS));
  test("free text agrees with the prose site", () => {
    for (const [name, value, secrets, framed, expected] of [...CATALOG_FIRST, ...NAMES_AND_DELIMITERS]) {
      if (!framed) expect([name, scrubText(value, secrets)]).toEqual([name, expected]);
    }
  });
});

describe("value shapes are read once over the assembled text", () => {
  test("a shape run directly before https://, http:// or data: is replaced to its end", () => check(SHAPES_ONCE));
  test("free text agrees with the prose site", () => {
    for (const [name, value, secrets, framed, expected] of SHAPES_ONCE) {
      if (!framed) expect([name, scrubText(value, secrets)]).toEqual([name, expected]);
    }
  });
});

describe("known secrets of equal length are ordered by code point", () => {
  test("two overlapping equal-length secrets give one output in either order", () => check(EQUAL_LENGTH));
  test("free text gives the same output in either order", () => {
    const text = "xx abcdefgh12ijklm yy";
    const a = scrubText(text, ["abcdefgh12", "fgh12ijklm"]), b = scrubText(text, ["fgh12ijklm", "abcdefgh12"]);
    expect(a).toBe("xx <redacted>ijklm yy");
    expect(b).toBe(a);
  });
});

describe("the name path budget is checked before the depth budget", () => {
  const nested = (levels: number, leaf: unknown, wrap: (inner: unknown) => unknown): unknown => { let value = leaf; for (let i = 0; i < levels; i++) value = wrap(value); return value; };
  test.each([33, 34, 40, 200])("an object nested %i levels deep exports the placeholder", levels => {
    expect(framedValue(nested(levels, "x", inner => ({ a: inner })), [])).toBe("<redacted>");
    expect(proseValue(nested(levels, "x", inner => ({ a: inner })), [])).toBe("<redacted>");
  });
  test.each([34, 40, 200])("an array nested %i levels deep exports the placeholder", levels => {
    expect(framedValue(nested(levels, "x", inner => [inner]), [])).toBe("<redacted>");
  });
  test("a name of more than 32 components exports the placeholder at any depth", () => {
    const path = Array.from({ length: 40 }, () => "a").join(".");
    expect(scrubValue("x", [], path, 0, false, undefined, none, true)).toBe("<redacted>");
    expect(scrubValue("x", [], path, 40, false, undefined, none, true)).toBe("<redacted>");
  });
});

describe("many unclosed quotes and many key tails stay linear", () => {
  // Sizes double from 25k up to 1.6M characters. A run that takes longer than the bound, or that grows by more than a linear factor
  // from one size to the next, fails at that size, so a quadratic reading stops at a small size instead of running for minutes.
  const SIZES = [25_000, 50_000, 100_000, 200_000, 400_000, 800_000, 1_600_000];
  const BOUND_MS = 10_000, FLOOR_MS = 40, GROWTH = 3.4;
  const inputs: [string, (n: number) => string][] = [
    ["many unclosed quotes in prose", n => "x " + '"' + '\\"'.repeat(Math.ceil(n / 2))],
    ["many unclosed quotes after an opening brace", n => "{" + '"' + '\\"'.repeat(Math.ceil(n / 2))],
    ["a key tail with a long whitespace run in the key", n => '{"' + " ".repeat(n) + 'x":"v"'],
    ["many key tails then a key with a long whitespace run", n => "{" + '"k":"v",'.repeat(Math.ceil(n / 16)) + '"' + " ".repeat(n / 2) + 'x":"v"'],
    ["many key tails with no values", n => "{" + '"k":'.repeat(Math.ceil(n / 4))],
    ["many key tails with unclosed values", n => "{" + '"k":"\\"'.repeat(Math.ceil(n / 6))],
  ];
  test.each(inputs)("%s", (_name, make) => {
    let previous = 0;
    for (const size of SIZES) {
      const text = make(size);
      const start = process.hrtime.bigint();
      scrubText(text);
      const elapsed = Number(process.hrtime.bigint() - start) / 1e6;
      expect({ size, elapsed: elapsed <= BOUND_MS }).toEqual({ size, elapsed: true });
      if (previous >= FLOOR_MS) expect({ size, grew: elapsed / previous <= GROWTH }).toEqual({ size, grew: true });
      previous = elapsed;
    }
  }, 120_000);
});

describe("known secrets are read before private keys and bare media", () => {
  const PEM = "-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBgkqhkiG9w0BAQEFAASC\n-----END PRIVATE KEY-----";
  test("a known secret that holds or borders a private key is replaced whole", () => {
    for (const framed of [false, true]) {
      const run = (value: string, secrets: string[]) => framed ? framedValue(value, secrets) : proseValue(value, secrets);
      expect(run(`log: ${PEM}\nTAILVsecretTail1 end`, [`${PEM}\nTAILVsecretTail1`])).toBe("log: <redacted> end");
      expect(run(`log: TAILVsecretHead1\n${PEM} end`, [`TAILVsecretHead1\n${PEM}`])).toBe("log: <redacted> end");
      expect(run(`log: ${PEM} end`, [])).toBe("log: <redacted> end");
    }
  });
  test("a known secret that runs into a bare image is not left readable", () => {
    const jpeg = "/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAAMC";
    for (const framed of [false, true]) {
      const out = (framed ? framedValue : proseValue)(`x TAILV36E0718 ${jpeg}`, ["x TAILV36E0718 /9j/4AAQ"]) as string;
      expect(out).not.toContain("TAILV36E0718");
      expect(out).toContain("<redacted>");
    }
  });
});
