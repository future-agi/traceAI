import { readdirSync, readFileSync } from "node:fs";
import { join, relative, resolve } from "node:path";

// The shared spec folder and the catalog copies built from it ship publicly.
// They must not name an absolute path on a developer machine: a macOS volume
// or home directory such as "/Volumes/<name>" or "/Users/<name>".
const root = resolve(__dirname, "../../../..");
const LOCAL_PATH = /(?<![\w.-])\/(?:Volumes|Users)\/[\w.-]+/;

function files(directory: string): string[] {
  return readdirSync(directory, { withFileTypes: true }).flatMap(entry => {
    const path = join(directory, entry.name);
    // Interpreter caches are local build output, not part of the spec.
    if (entry.isDirectory()) return entry.name === "__pycache__" ? [] : files(path);
    return entry.isFile() ? [path] : [];
  });
}

test("the spec folder and catalog copies contain no absolute local paths", () => {
  const scanned = [
    ...files(resolve(root, "spec/auto-instrumentation")),
    resolve(root, "typescript/packages/fi-core/src/privacy.json"),
    resolve(root, "typescript/packages/traceai_auto/src/wire/privacy.json"),
  ];
  expect(scanned.map(path => relative(root, path))).toEqual(expect.arrayContaining([
    "spec/auto-instrumentation/README.md",
    "spec/auto-instrumentation/privacy.json",
  ]));
  const found = scanned.flatMap(path => readFileSync(path, "utf8").split("\n")
    .flatMap((line, index) => (LOCAL_PATH.test(line) ? [`${relative(root, path)}:${index + 1}`] : [])));
  expect(found).toEqual([]);
});
