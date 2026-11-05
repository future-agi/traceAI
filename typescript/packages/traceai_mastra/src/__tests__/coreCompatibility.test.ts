import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { redactPiiInValue } from "@traceai/fi-core";

const manifest = (path: string) => JSON.parse(readFileSync(resolve(__dirname, path), "utf8"));
const compare = (left: string, right: string): number => {
  const a = left.split(".").map(Number);
  const b = right.split(".").map(Number);
  for (let i = 0; i < 3; i++) if (a[i] !== b[i]) return a[i] - b[i];
  return 0;
};

describe("shared privacy dependency floor", () => {
  it("excludes published core 1.1.0, which has no public PII helper", () => {
    const mastra = manifest("../../package.json");
    const core = manifest("../../../fi-core/package.json");
    const minimum = /^workspace:\^(\d+\.\d+\.\d+)$/.exec(mastra.dependencies["@traceai/fi-core"]);
    expect(minimum).not.toBeNull();
    expect(compare(minimum![1], "1.1.0")).toBeGreaterThan(0);
    expect(compare(core.version, minimum![1])).toBeGreaterThanOrEqual(0);
    expect(redactPiiInValue(["test@example.com", "safe"])).toEqual(["<EMAIL_ADDRESS>", "safe"]);
  });
});
