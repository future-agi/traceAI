import { readFileSync, existsSync } from "node:fs";
import { resolve, dirname } from "node:path";
import { execFileSync } from "node:child_process";

const root = resolve(__dirname, "..");
const repo = resolve(root, "../../..");
const read = (file: string) => readFileSync(resolve(root, file), "utf8");

test("README coverage is generated from the runtime registry", () => {
  expect(() => execFileSync(process.execPath, [resolve(root, "scripts/generate-readme.cjs"), "--check"])).not.toThrow();
});

test("configuration documents every init option and privacy control", () => {
  const config = read("src/config.ts");
  const reference = read("docs/CONFIGURATION.md");
  const options = config.split("export interface Options {")[1].split("\n}")[0];
  for (const [, option] of options.matchAll(/\b(\w+)\?:/g)) {
    expect(reference).toContain(`\`${option}\``);
  }
  const privacy = config.split('for (const key of [')[1].split(']')[0];
  for (const [, option] of privacy.matchAll(/"(\w+)"/g)) {
    expect(reference).toContain(`\`${option}\``);
  }
});

test("documentation does not require FI credentials for custom exporters", () => {
  expect(read("README.md")).not.toContain("It does not bypass the credential requirement");
  expect(read("docs/CONFIGURATION.md")).toContain("No FI credentials required");
});

test("the shared contract does not claim direct Undici wire capture", () => {
  const spec = readFileSync(resolve(repo, "spec/auto-instrumentation/README.md"), "utf8");
  expect(spec).not.toContain("covers undici and every fetch-based SDK");
  expect(spec).toContain("not direct `undici.fetch`/`undici.request`");
});

test("package guide links resolve within the repository", () => {
  const prefix = "https://github.com/future-agi/traceAI/blob/dev/";
  for (const name of ["README.md", "docs/CONFIGURATION.md", "docs/FRAMEWORKS.md", "docs/ARCHITECTURE.md"]) {
    for (const [, url] of read(name).matchAll(/\]\(([^)]+)\)/g)) {
      if (url.startsWith("#") || (/^https?:/.test(url) && !url.startsWith(prefix))) continue;
      const target = url.split("#")[0];
      const file = target.startsWith(prefix) ? resolve(repo, target.slice(prefix.length)) : resolve(dirname(resolve(root, name)), target);
      expect({ url, exists: existsSync(file) }).toEqual({ url, exists: true });
    }
  }
});
