import { describe, expect, test } from "@jest/globals";
import { scrubValue } from "../privacy-runtime";

// The URL catalog tests both the written and the once-read name in query, fragment and path pairs, including names after a
// second '?'. An undecided name can cause its value to be replaced, and a decoded exclusion can suppress a match. The signed
// query vocabulary does not apply to path names. Only the value is replaced: safe neighbors and the fragment stay in place.

const VALUE = "PLNTq7Zr4kXw9";

// [prefix through the value separator, suffix after the planted value]
const REDACTED: [string, string][] = [
  ["https://e.invalid/p?pass%C3%A9word=", ""],
  ["https://e.invalid/p?pass%C3%A9word=", "&safe=yes"],
  ["see https://e.invalid/p?pass%C3%A9word=", "&safe=yes#top next"],
  ["https://e.invalid/a#pass%C3%A9word=", ""],
  ["see https://e.invalid/a#pass%C3%A9word=", "&safe=yes next"],
  ["https://e.invalid/a;pass%C3%A9word=", ""],
  ["https://e.invalid/a/pass%C3%A9word=", "/b"],
  ["https://e.invalid/a;pass%C3%A9word%3d", ";safe=yes/b"],
  ["https://e.invalid/a/pass%C3%A9word;parts=", ";safe=yes/b"],
  ["https://e.invalid/?a?pass%C3%A9word=", "&safe=yes"],
  ["https://e.invalid/p?a=b?pass%C3%A9word=", "&safe=yes"],
  ["https://e.invalid/p#a?pass%C3%A9word=", "&safe=yes"],
  ["https://e.invalid/p#a=b?pass%C3%A9word=", "&safe=yes"],
  ["https://e.invalid/p?token+count=", "&safe=yes"],
  ["https://e.invalid/p?api+k%65y=", "&safe=yes"],
  ["https://e.invalid/p?%73v=", "&safe=yes"],
  ["https://e.invalid/p#%73v=", "&safe=yes"],
  ["https://e.invalid/p?%2521=", "&safe=yes"],
  ["https://e.invalid/a;%21=", ";safe=yes/b"],
  // A URL embedded in prose still owns the query pairs after a value it replaced.
  ["see https://e.invalid/p?%74oken=", "&safe=yes#top next"],
];

const GENERATED: [string, string][] = [
  ["https://e.invalid/a;pass%C3%A9wd=", "/b"],
  ["see https://e.invalid/p?x=1&a%3Authoriza%2Ction=", " next"],
  ["see https://e.invalid/p?x=1&passw%25d%2F=", " next"],
  ["https://e.invalid/p?a=1&secr%25et%2B=", "&b=2"],
  ["https://e.invalid/p?a=1&pass%c3%a9wd=", "&b=2"],
  ["https://e.invalid/a;to%25ken%21=", "/b"],
  ["https://e.invalid/a;a%0Auth_toke%22n=", "/b"],
  ["https://e.invalid/p?a=1&si%25gna%A9ture=", "&b=2"],
  ["https://e.invalid/p?a=1&s%25ig%2B=", "&b=2"],
  ["https://e.invalid/p#k%25ey%22=", "&x=1"],
  ["https://e.invalid/a/pass%C3%A9wd=", "/b"],
  ["https://e.invalid/a/s%25igna%22ture=", "/b"],
  ["https://e.invalid/p?a=b?passwo%25rd%E2%80%8B=", ""],
  ["https://e.invalid/a;pass%C3%A9word=", "/b"],
  ["see https://e.invalid/p?x=1&pass%C3%A9word=", " next"],
  ["https://e.invalid/a;tok%25en%3A=", "/b"],
];

const UNCHANGED = [
  `https://e.invalid/p?n%61me=${VALUE}&safe=yes`,
  `https://e.invalid/p?token%20count=${VALUE}&safe=yes`,
  `https://e.invalid/p#tok%65n_count=${VALUE}&safe=yes`,
  `https://e.invalid/a;token%20count=${VALUE};safe=yes/b`,
  `https://e.invalid/p?caf%C3%A9=${VALUE}&safe=yes`,
  `https://e.invalid/a/%73v=${VALUE};safe=yes/b`,
  `https://e.invalid/p?pass%C3%A9word=<redacted>&safe=${VALUE}`,
];

const RUNS = Array.from({ length: VALUE.length - 3 }, (_, i) => VALUE.slice(i, i + 4));

describe("URL pair names as written and after one reading", () => {
  test.each([...REDACTED, ...GENERATED])("%s[value]%s replaces just the value", (prefix, suffix) => {
    const out = scrubValue(prefix + VALUE + suffix, [], "");
    expect(out).toEqual(prefix + "<redacted>" + suffix);
    // The marker is ASCII. Read its percent forms even in a URL with unrelated malformed UTF-8.
    const raw = String(out);
    const decoded = raw.replace(/%([0-7][0-9a-f])/gi, (_, hex: string) => String.fromCharCode(parseInt(hex, 16)));
    expect(RUNS.filter(run => raw.includes(run) || decoded.includes(run))).toEqual([]);
  });

  test.each(UNCHANGED)("%j keeps the non-credential value", input => {
    expect(scrubValue(input, [], "")).toEqual(input);
  });
});
