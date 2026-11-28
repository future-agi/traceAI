import { describe, expect, test } from "@jest/globals";
import { scrubText, scrubValue } from "../privacy-runtime";

const P = "PLNTHead9&x=https://e.invalid/#%74oken=";

// Cover every occurrence of a registered secret from the value the catalog replaced
// to the end of its URL token (what stands before that value, the name and delimiter
// the catalog and the pair rule read, stays). An input placeholder is not a catalog
// edit.
// `exact` is the scrubValue output; where it is marked d2 it is also what d2dab4e8
// (generate_privacy_fixtures.py sha256 4dbcc945...) exports. Elsewhere d2 hides
// at most what this output hides.
const secretCases = [
  {
    name: "P1-1 hides both overlapping secrets across a path and query",
    input: "log https://e.invalid/%74oken=PLNTHead9?PLNTTail7&a=PLNTOther8 next",
    secrets: ["PLNTHead9?PLNTTail7", "PLNTTail7&a=PLNTOther8"],
    hidden: ["PLNTHead9", "PLNTTail7", "PLNTOther8"],
    kept: ["log https://e.invalid/"],
    exact: "log https://e.invalid/%74oken=<redacted>", // d2
  },
  {
    name: "P1-2 hides both occurrences when only one crosses a catalog edit",
    input: "log https://e.invalid/?%74oken=PLNTHead9&PLNTTail7&a=PLNTHead9&PLNTTail7 next",
    secrets: ["PLNTHead9&PLNTTail7"],
    hidden: ["PLNTHead9", "PLNTTail7"],
    exact: "log https://e.invalid/?%74oken=<redacted>", // d2
  },
  {
    name: "P1-2b hides the intervening pair between two catalog edits",
    input: "log https://e.invalid/?%74oken=PLNTHead9&safe=PLNTTail7&%74oken=PLNTOther8 next",
    secrets: ["PLNTHead9&safe=PLNTTail7&%74oken=PLNTOther8"],
    hidden: ["PLNTHead9", "PLNTTail7", "PLNTOther8", "&safe=PLNTTail7"],
    exact: "log https://e.invalid/?%74oken=<redacted>", // d2
  },
  {
    name: "P1-2c hides the secret tail in each of three URL tokens",
    input: "log " + "https://e.invalid/?%74oken=PLNTHead9&PLNTTail7 ".repeat(3) + "next",
    secrets: ["PLNTHead9&PLNTTail7"],
    hidden: ["PLNTTail7"],
    exact: "log " + "https://e.invalid/?%74oken=<redacted> ".repeat(2) + "https://e.invalid/?%74oken=<redacted>", // d2
  },
  {
    name: "P1-3 quoted hides a secret that includes the closing quote",
    input: "log 'https://e.invalid/?%74oken=PLNTHead9&PLNTTail7' next",
    secrets: ["PLNTHead9&PLNTTail7'"],
    hidden: ["PLNTHead9", "PLNTTail7"],
    exact: "log 'https://e.invalid/?%74oken=<redacted>", // d2
  },
  {
    name: "P1-3 self-overlap hides both overlapping occurrences",
    input: "log " + P + P + "PLNTHead9 next",
    secrets: [P + "PLNTHead9"],
    // Both overlapping occurrences are one union, hidden whole (d2 keeps the first occurrence's head,
    // "PLNTHead9&x=https://e.invalid/#%74oken=", before the value the catalog replaced).
    hidden: ["%74oken=PLNTHead9", "<redacted>&x=", "PLNTHead9 next", "PLNTHead9&x="],
    exact: "log <redacted>",
  },
  {
    name: "a cut secret holding a credential name keeps that name's value hidden",
    input: "https://e.invalid/?token=PLNTHead9&next=pass%77ord%3DPLNTTail7&extra=PLNTOther8",
    secrets: ["PLNTHead9&next=pass%77ord%3DPLNTTail7"],
    hidden: ["PLNTHead9", "PLNTTail7", "PLNTOther8"],
    exact: "https://e.invalid/?token=<redacted>",
  },
  {
    name: "a secret from a data URI over a credential name keeps the pair's value hidden",
    input: "begin data:text/plain;base64,SGVsbG8= password=PLNTval9 tail end",
    secrets: ["data:text/plain;base64,SGVsbG8= password"],
    hidden: ["PLNTval9"],
    exact: "begin <omitted: 8 base64 chars> password=<redacted>", // d2
  },
  {
    name: "P1-4 hides a secret starting at a literal input placeholder",
    input: "log https://e.invalid/?a=tok%65n%3D<redacted>&PLNTTail7 next",
    secrets: ["<redacted>&PLNTTail7"],
    hidden: ["PLNTTail7"],
    exact: "log https://e.invalid/?a=tok%65n%3D<redacted>", // d2
  },
];

const scrubbers = [
  { name: "scrubValue", scrub: (input: string, secrets: string[]) => scrubValue(input, secrets, ""), checkContext: true },
  { name: "scrubText", scrub: scrubText, checkContext: false },
];

describe.each(scrubbers)("$name", ({ scrub, checkContext }) => {
  test.each(secretCases)("$name", ({ input, secrets, hidden, kept, exact }) => {
    const out = scrub(input, secrets);
    for (const marker of hidden) expect(out).not.toContain(marker);
    if (checkContext && kept) for (const text of kept) expect(out).toContain(text);
    if (checkContext) expect(out).toBe(exact);
  });

  test("P3 keeps the trailing safe pair outside the secret in userinfo", () => {
    const out = scrub("https://u:PLNTHead9&safe=yes@e.invalid/?%74oken=x&safe=yes", ["PLNTHead9&safe=yes"]);
    expect(out).not.toContain("PLNTHead9");
    expect(out.endsWith("&safe=yes")).toBe(true);
    if (checkContext) expect(out).toBe("https://<redacted>@e.invalid/?%74oken=<redacted>&safe=yes"); // d2
  });

  test("P3b keeps the query text outside the secret in a later value", () => {
    const out = scrub("https://e.invalid/token=x?foo=bar&token=PLNTHead9?foo=bar", ["PLNTHead9?foo=bar"]);
    expect(out).not.toContain("PLNTHead9");
    expect(out).toContain("token=");
    // scrubText reads the URL as prose, where the pair rule takes 'token=' to the end of the text.
    if (checkContext) expect(out).toBe("https://e.invalid/token=<redacted>?foo=bar&token=<redacted>"); // d2
  });
});

test.each([
  {
    name: "KEEP a percent-spelled token preserves the other query pairs and fragment",
    input: "see https://e.invalid/p?a=1&%74oken=PLNTq7Zr4kXw9&safe=yes#top next",
    expected: "see https://e.invalid/p?a=1&%74oken=<redacted>&safe=yes#top next",
  },
  {
    name: "KEEP a percent-spelled secret preserves the following query pair",
    input: "https://e.invalid/p?%73ecret=PLNTq7Zr4kXw9&safe=yes",
    expected: "https://e.invalid/p?%73ecret=<redacted>&safe=yes",
  },
  {
    name: "KEEP a literal placeholder in a prose pair does not stop redaction",
    input: "tok%65n=<redacted>&safe=yes",
    expected: "tok%65n=<redacted>",
  },
])("$name", ({ input, expected }) => {
  expect(scrubValue(input, [], "")).toBe(expected);
});
