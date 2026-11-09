import { describe, expect, it } from "@jest/globals";
import { capBase64Images } from "../trace/trace-config/imageCap";
import { REDACTED_VALUE as MARK } from "../trace/trace-config/constants";

const MB = 1024 * 1024;
const LIMIT_MS = 200;
const SLASH = String.fromCharCode(92);
const fill = (unit: string) => unit.repeat(Math.floor((MB - 64) / unit.length));

// Same threshold, repetition count and CPU accounting as the protected suite.
// This is additive: the original six performance shapes must still run unchanged.
function bestCpuMs(fn: () => void): number {
  let best = Infinity;
  for (let run = 0; run < 5; run++) {
    const before = process.cpuUsage();
    fn();
    const used = process.cpuUsage(before);
    best = Math.min(best, (used.user + used.system) / 1000);
  }
  return best;
}

const SHAPES: Array<[string, () => [string, string]]> = [
  ["dense simple escapes", () => ["data:image" + SLASH + "/png;base64," + fill(SLASH + "/"), "[image-redacted]"]],
  ["dense Unicode escapes", () => ["data:image" + SLASH + "u002Fpng;base64," + fill(SLASH + "u002f"), "[image-redacted]"]],
  ["mixed raw, Unicode, slash and CR/LF units", () => ["data:image/png;base64," + fill(SLASH + "u0041B" + SLASH + "/" + SLASH + "r" + SLASH + "n") + "A", MARK]],
  ["ordinary escaped text", () => {
    const raw = "ordinary " + fill(SLASH + "t" + SLASH + "u0078" + SLASH + "/") + " end";
    return [raw, raw];
  }],
  ["one escape before a long raw run", () => ["data:image" + SLASH + "/png;base64," + "A".repeat(MB - 64), "[image-redacted]"]],
  ["declared JSON payload with dense escapes", () => {
    const payload = fill(SLASH + "u0041");
    const raw = '{ "data":"' + payload + '","media_type":"image/png","note":"keep" }';
    return [raw, raw.replace(payload, MARK)];
  }],
];

describe("escaped decode allocation performance", () => {
  it.each(SHAPES)("%s, 1 MB", (_label, build) => {
    const [raw, expected] = build();
    let result: unknown;
    const elapsed = bestCpuMs(() => { result = capBase64Images(raw, 1000); });
    // A passthrough, whole-input shortcut or partial payload cannot satisfy these
    // output controls merely by beating the timer. Ordinary text must be retained.
    expect(result).toBe(expected);
    expect(elapsed).toBeLessThan(LIMIT_MS);
  });
});
