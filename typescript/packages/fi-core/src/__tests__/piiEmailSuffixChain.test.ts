import { describe, expect, it } from "@jest/globals";
import { redactPiiInString } from "../trace/trace-config/piiRedaction";

/** Observe the real public scanner; do not run a reference regex on large text. */
function observeChain(text: string) {
  const nativeCode = String.prototype.charCodeAt;
  const nativeIndex = String.prototype.indexOf;
  let reads = 0, searchUnits = 0, searches = 0, nextSearchFloor = 0;
  try {
    String.prototype.charCodeAt = function (this: string, index: number) {
      if (this === text) reads++;
      return nativeCode.call(this, index);
    };
    String.prototype.indexOf = function (this: string, search: string, position?: number) {
      const found = nativeIndex.call(this, search, position);
      if (this === text && search === "@") {
        const from = position ?? 0;
        if (from < nextSearchFloor || from > text.length) {
          throw new Error("Suffix-chain search revisited an already searched prefix");
        }
        searches++;
        searchUnits += (found < 0 ? text.length : found + 1) - from;
        nextSearchFloor = found < 0 ? text.length : found + 1;
      }
      return found;
    };
    const output = redactPiiInString(text);
    return { output, reads, searchUnits, searches };
  } finally {
    String.prototype.indexOf = nativeIndex;
    String.prototype.charCodeAt = nativeCode;
  }
}

// .x fails the two-letter TLD rule; .failed_ fails the final word boundary.
// Neither is a separator between candidates: each failed suffix is reused as
// the next local run. In particular the dot after the preceding accepted 'o'
// is itself an original word boundary and belongs to that next replacement.
const SUFFIXES = [".x", ".failed_"];
const COUNTS = [1, 4, 16, 64, 1024, 32768];

describe("growing failed-domain suffix chains", () => {
  it.each(SUFFIXES)("reuses %s as the next local run without rescanning prefixes", (suffix) => {
    for (const count of COUNTS) {
      const raw = "keep | a@b.co" + (suffix + "@b.co").repeat(count) + suffix + " | a@b.c tail";
      // One initial address and one per repeated @b.co. The last failed suffix
      // has no following @ and must survive, as must the invalid final address.
      const expected = "keep | " + "<EMAIL_ADDRESS>".repeat(count + 1) + suffix + " | a@b.c tail";
      const observed = observeChain(raw);
      expect(observed.output).toBe(expected);
      expect(observed.reads).toBeGreaterThan(0);
      expect(observed.searches).toBeGreaterThan(0);
      expect(observed.reads).toBeLessThanOrEqual(8 * raw.length + 32);
      expect(observed.searchUnits).toBeLessThanOrEqual(raw.length);
    }
  });

  it.each(SUFFIXES)("preserves an all-failed chain ending in %s", (suffix) => {
    for (const count of COUNTS) {
      const raw = "keep | a@b.c" + (suffix + "@b.c").repeat(count) + suffix + " | ordinary";
      // There is no eligible TLD anywhere, even though many suffixes become
      // valid local runs for later failed candidates. A universal drop fails.
      const observed = observeChain(raw);
      expect(observed.output).toBe(raw);
      expect(observed.reads).toBeGreaterThan(0);
      expect(observed.searches).toBeGreaterThan(0);
      expect(observed.reads).toBeLessThanOrEqual(8 * raw.length + 32);
      expect(observed.searchUnits).toBeLessThanOrEqual(raw.length);
    }
  });
});
