import { readFileSync } from "node:fs";
import { resolve } from "node:path";

test("Node 22 example loads ESM-only pgvector without require", () => {
  const readme = readFileSync(resolve(__dirname, "../../README.md"), "utf8");
  const example = readme.match(/Save this as `search\.cjs`[\s\S]*?```javascript\n([\s\S]*?)\n```/)?.[1];
  expect(example).toBeDefined();
  expect(example).toContain('await import("pgvector")');
  expect(example).not.toMatch(/require\(["']pgvector["']\)/);
});
