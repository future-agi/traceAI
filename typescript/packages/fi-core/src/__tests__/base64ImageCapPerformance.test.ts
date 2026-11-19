import { describe, it, expect } from "@jest/globals";
import { capBase64Images } from "../trace/trace-config/imageCap";
import { REDACTED_VALUE } from "../trace/trace-config/constants";

const MB = 1024 * 1024;
/** Generous: the linear code needs a few ms per MB, a quadratic one seconds to minutes */
const LIMIT_MS = 200;

function repeatTo(unit: string, size: number): string {
  return unit.repeat(Math.ceil(size / unit.length));
}

/** The best of five runs, in CPU time, so a busy machine does not fail a linear algorithm */
function time(fn: () => void): number {
  let best = Infinity;
  for (let run = 0; run < 5; run++) {
    const before = process.cpuUsage();
    fn();
    const used = process.cpuUsage(before);
    best = Math.min(best, (used.user + used.system) / 1000);
  }
  return best;
}

const SHAPES: Array<[string, () => string]> = [
  ["many data URL headers", () => repeatTo("data:image/png;base64,", MB - 64)],
  ["a data URL header with a long parameter run", () => `data:image/${repeatTo(";a", MB - 64)}`],
  ["base64 text with a bad character at the end", () => `${repeatTo("QUJD", MB - 64)}!`],
  ["deeply nested unparseable JSON", () => repeatTo("[", MB)],
  ["a long JSON array of short strings", () => `[${repeatTo('"a",', MB - 64)}"a"]`],
  ["many escaped slashes after a header", () => `data:image\\/png;base64,${repeatTo("\\/", MB - 64)}`],
];

describe("the base64 image cap is linear in the value size", () => {
  it.each(SHAPES)("%s, 1 MB", (_name, build) => {
    const input = build();
    let result: unknown;
    const elapsed = time(() => {
      result = capBase64Images(input, 1000);
    });
    expect(typeof result).toBe("string");
    expect(elapsed).toBeLessThan(LIMIT_MS);
  });

  it("a 1 MB data URL is replaced whole", () => {
    expect(capBase64Images(`data:image/png;base64,${repeatTo("QUJD", MB - 64)}`, 1000)).toBe(REDACTED_VALUE);
  });
});
