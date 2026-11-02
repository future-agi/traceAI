import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { redactPiiInValue } from "@traceai/fi-core";

const packageJson = (path: string): { version: string; dependencies: Record<string, string> } =>
  JSON.parse(readFileSync(resolve(__dirname, path), "utf8"));

const parts = (version: string): number[] => version.split(".").map(Number);
const compare = (left: string, right: string): number => {
  const a = parts(left);
  const b = parts(right);
  for (let i = 0; i < 3; i++) {
    if (a[i] !== b[i]) return a[i] - b[i];
  }
  return 0;
};

describe("released fi-core compatibility", () => {
  it("requires a fi-core release that exports the PII helper", () => {
    const mastra = packageJson("../../package.json");
    const core = packageJson("../../../fi-core/package.json");
    const range = mastra.dependencies["@traceai/fi-core"];
    const match = /^workspace:\^(\d+\.\d+\.\d+)$/.exec(range);
    expect(match).not.toBeNull();
    // fi-core 1.1.0 is already published without redactPiiInValue. A plain
    // workspace:^ would pack as ^1.1.0 and allow a lockfile to retain it.
    expect(compare(match![1], "1.1.0")).toBeGreaterThan(0);
    expect(compare(core.version, match![1])).toBeGreaterThanOrEqual(0);
    expect(redactPiiInValue("test@example.com")).toBe("<EMAIL_ADDRESS>");
  });
});
