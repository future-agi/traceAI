import { describe, expect, it, jest } from "@jest/globals";
import * as publicApi from "../index";
import {
  capBase64Images,
  __ImageCapEditsForTest,
} from "../trace/trace-config/imageCap";

// These expectations describe literal layouts, not a second union algorithm.
// The deep-module seam exposes the production class, never a package-index API.
const MARK = "__REDACTED__";
type Range = readonly [number, number];

function applyRanges(text: string, ranges: readonly Range[]): string {
  const edits = new __ImageCapEditsForTest(text.length);
  for (const [start, end] of ranges) edits.add(start, end);
  return edits.apply(text);
}

describe("literal Edits union topologies", () => {
  it("does not expose the internal constructor from the package index", () => {
    expect(Object.prototype.hasOwnProperty.call(publicApi, "__ImageCapEditsForTest")).toBe(false);
  });

  it.each([
    { order: "forward", ranges: [[0, 2048], [1024, 3072]] as Range[] },
    { order: "reverse", ranges: [[1024, 3072], [0, 2048]] as Range[] },
  ])("extends the crossing [0,2048), [1024,3072) pair: $order", ({ ranges }) => {
    expect(applyRanges("x".repeat(3072) + " tail", ranges)).toBe(MARK + " tail");
  });

  it("propagates an out-of-order extension chain through successive whole blocks", () => {
    expect(applyRanges("x".repeat(5000) + " tail", [
      [3072, 5000], [1024, 3072], [0, 2048], [2048, 4096],
    ])).toBe(MARK + " tail");
  });

  it.each([
    { order: "increasing", ends: [1536, 2048, 3072] },
    { order: "decreasing", ends: [3072, 2048, 1536] },
  ])("keeps both repeated-start maxima with $order ends", ({ ends }) => {
    const raw = "x".repeat(3072) + " tail";
    // A repeated outer start exercises ends[start], not just the block summary.
    expect(applyRanges(raw, ends.map((end) => [0, end] as const))).toBe(MARK + " tail");
    // Repeating an interior start exercises blocks[1] and its reach extension.
    const ranges: Range[] = [[0, 2048], ...ends.map((end) => [1024, end] as const)];
    expect(applyRanges(raw, ranges)).toBe(MARK + " tail");
  });

  it("absorbs nested ranges without extra markers or shrinking the container", () => {
    const gap = " keep " + ".".repeat(1018);
    const raw = "a".repeat(3072) + gap + "b".repeat(3072) + " tail";
    // The separate crossing component starts at 4096. It explicitly protects
    // the reach update; containment by itself would not detect its deletion.
    expect(applyRanges(raw, [
      [1024, 2048], [17, 30], [0, 3072], [2047, 2304],
      [4096, 6144], [5120, 7168],
    ])).toBe(MARK + " keep " + ".".repeat(1018) + MARK + " tail");
  });

  it("keeps genuinely touching aligned intervals as two adjacent markers", () => {
    const gap = " keep " + ".".repeat(2042);
    const raw = "a".repeat(2048) + gap + "b".repeat(3072) + " tail";
    expect(applyRanges(raw, [
      [0, 1024], [1024, 2048], [4096, 6144], [5120, 7168],
    ])).toBe(MARK + MARK + " keep " + ".".repeat(2042) + MARK + " tail");
  });

  it("does not absorb a start exactly at the current end inside a partial block", () => {
    const gap = " keep " + ".".repeat(1018);
    const raw = "a".repeat(3072) + gap + "b".repeat(3072) + " tail";
    // The summary for [1024,2048) includes start 2047, but that start is NOT
    // inside [0,2047). Using the whole summary early would merge the markers.
    expect(applyRanges(raw, [
      [0, 2047], [2047, 3072], [4096, 6144], [5120, 7168],
    ])).toBe(MARK + MARK + " keep " + ".".repeat(1018) + MARK + " tail");
  });

  it("unions a later fallback around earlier edits and preserves unrelated text", () => {
    const prefix = "lead " + "l".repeat(95);
    const gap = " between " + "g".repeat(1087);
    const raw = prefix + "a".repeat(2900) + gap + "b".repeat(3072) + " tail";
    expect(applyRanges(raw, [
      [1800, 2300], [2400, 2500],
      [4096, 6144], [5120, 7168],
      [100, 3000], // Original-coordinate fallback added AFTER the narrower edits.
    ])).toBe("lead " + "l".repeat(95) + MARK + " between " + "g".repeat(1087) + MARK + " tail");
  });
});

describe("reachable exhaustion after a pending image edit", () => {
  it("replaces only the exhausted outer token, enclosing its earlier image edit", () => {
    const slash = String.fromCharCode(92);
    // Each decode removes one five-unit u005c layer but keeps the 8192-unit tail.
    // 64 such views cannot fit this <10000-unit token's 32*rawLength budget.
    const expensive = slash + "u005c".repeat(256) + "u0078ordinary " + "z".repeat(8192);
    const image = "data:image/png;base64," + "A".repeat(80);
    // inspectScope is LIFO: the second array element is edited BEFORE the first
    // element exhausts the shared outer token's budget.
    const inner = JSON.stringify([expensive, image]);
    const encoded = JSON.stringify(inner);
    const prefix = '{ "before":"keep", "scope":';
    const raw = prefix + encoded + ', "other":"untouched ' + "o".repeat(96) + '", "after":"keep" }';
    const imageStart = raw.indexOf("data:image/png;base64,");
    const fallbackStart = prefix.length + 1;
    const fallbackEnd = prefix.length + encoded.length - 1;
    const add = jest.spyOn(__ImageCapEditsForTest.prototype, "add");
    try {
      const output = capBase64Images(raw, 64);
      expect(output).toBe(prefix + JSON.stringify(JSON.stringify(["[image-redacted]", MARK])) + ', "other":"untouched ' + "o".repeat(96) + '", "after":"keep" }');
      // The spy calls through. This pins the real order and containing range;
      // the output oracle above is still literal and independent of the calls.
      expect(add.mock.calls).toEqual([[0, image.length]]);
      expect(imageStart).toBeGreaterThan(fallbackStart);
      expect(imageStart + image.length).toBeLessThan(fallbackEnd);
    } finally {
      add.mockRestore();
    }
  });
});
