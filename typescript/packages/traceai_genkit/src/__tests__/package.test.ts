/**
 * Package manifest contract: genkit is a peer dependency only, the tarball
 * ships built output (no tests, no build caches), and the entry points match
 * the build layout of the sibling packages (traceai_anthropic).
 */
import * as fs from "fs";
import * as path from "path";

const root = path.resolve(__dirname, "..", "..");
const manifest = JSON.parse(fs.readFileSync(path.join(root, "package.json"), "utf8"));
const sibling = JSON.parse(fs.readFileSync(path.join(root, "..", "traceai_anthropic", "package.json"), "utf8"));

describe("@traceai/genkit package.json", () => {
  it("keeps genkit a peer dependency (devDependency pinned for tests), never a dependency", () => {
    expect(manifest.peerDependencies).toEqual({ genkit: "^1.42.0" });
    expect(manifest.devDependencies.genkit).toBe("1.42.0");
    expect(manifest.dependencies.genkit).toBeUndefined();
    expect(manifest.bundledDependencies ?? manifest.bundleDependencies).toBeUndefined();
  });

  it("ships dist and README only, without tests or tsbuildinfo caches", () => {
    expect(manifest.files).toEqual(["dist", "README.md", "!**/__tests__/**", "!**/*.tsbuildinfo"]);
  });

  it("uses the sibling packages' entry points and build script", () => {
    for (const key of ["main", "module", "esnext", "types", "exports"]) {
      expect(manifest[key]).toEqual(sibling[key]);
    }
    expect(manifest.scripts.build).toBe(sibling.scripts.build);
    expect(manifest.scripts.postbuild).toBe(sibling.scripts.postbuild);
  });

  it("points repository metadata at future-agi/traceAI with this package's directory", () => {
    expect(manifest.name).toBe("@traceai/genkit");
    expect(manifest.license).toBe("Apache-2.0");
    expect(manifest.repository).toEqual({
      type: "git",
      url: sibling.repository.url,
      directory: "typescript/packages/traceai_genkit",
    });
    expect(manifest.bugs).toEqual(sibling.bugs);
    expect(manifest.homepage).toBe(
      "https://github.com/future-agi/traceAI/tree/main/typescript/packages/traceai_genkit#readme",
    );
  });
});
