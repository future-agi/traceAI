import { describe, it, expect } from "@jest/globals";
import { readdirSync, readFileSync } from "node:fs";
import { builtinModules } from "node:module";
import { join } from "node:path";

// The workspace hoists every package, so an undeclared import still resolves here.
// Strict installs (Yarn PnP, pnpm with hoist=false) only expose declared packages,
// and an undeclared import makes `@traceai/langchain` fail to load there.
const packageRoot = join(__dirname, "..", "..");
const manifest = JSON.parse(readFileSync(join(packageRoot, "package.json"), "utf8"));
const declared = new Set([
  ...Object.keys(manifest.dependencies ?? {}),
  ...Object.keys(manifest.peerDependencies ?? {}),
]);

function packageName(specifier: string) {
  const parts = specifier.split("/");
  return specifier.startsWith("@") ? parts.slice(0, 2).join("/") : parts[0];
}

describe("published dependencies", () => {
  it("declares every package the shipped source imports", () => {
    const sourceDir = join(packageRoot, "src");
    const imported = new Set<string>();
    for (const file of readdirSync(sourceDir)) {
      if (!file.endsWith(".ts")) continue;
      const source = readFileSync(join(sourceDir, file), "utf8");
      for (const [, specifier] of source.matchAll(
        /(?:\bfrom\s+|\bimport\s*\(\s*|\brequire\s*\(\s*)["']([^"']+)["']/g,
      )) {
        if (specifier.startsWith(".") || specifier.startsWith("node:")) continue;
        if (builtinModules.includes(packageName(specifier))) continue;
        imported.add(packageName(specifier));
      }
    }
    expect(imported.size).toBeGreaterThan(0);
    expect([...imported].filter((name) => !declared.has(name)).sort()).toEqual([]);
  });
});
