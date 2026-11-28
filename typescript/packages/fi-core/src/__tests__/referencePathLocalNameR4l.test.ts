import { describe, expect, test } from "@jest/globals";
import { scrubValue, scrubText } from "../privacy-runtime";

// A URL path pair's local name is read as written and once decoded, by the one name decision: an encoded space alone ('%20') is
// a name the pair rule cannot read, so its value is replaced, in a whole URL value and in a URL in prose, as d2dab4e8 exports it.
describe("the URL catalog reads each local path-pair name", () => {
  const raw = "https://e.invalid/p/safe=1;%20=PLNTEdgeValue9;next=ok";
  const clean = "https://e.invalid/p/safe=1;%20=<redacted>;next=ok";

  test("an undecided percent-spelled local name hides its value in a framed URL", () => {
    expect(scrubValue(raw, [], "")).toBe(clean);
  });

  test("the same local name hides its value in a URL in prose", () => {
    expect(scrubText(`see ${raw}`)).toBe(`see ${clean}`);
  });
});
