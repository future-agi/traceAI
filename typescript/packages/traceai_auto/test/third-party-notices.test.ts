import * as fs from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, relative, resolve } from "node:path";
import { runInNewContext } from "node:vm";

const root = resolve(__dirname, "..");
const bundles = [
  "dist/agent/hook-register.cjs",
  "dist/agent/hook.mjs",
  "dist/agent/runtime.cjs",
  "dist/edge/index.mjs",
];
type Notice = { name: string; version: string; license: string; files: { file: string; text: Buffer }[] };

// Consume license bodies by byte length, not by a delimiter that could also occur
// in an upstream license. A package name mentioned in someone else's text cannot
// satisfy coverage, and a header/identifier without license bytes cannot pass.
function parseNotices(bytes: Buffer): { firstParty: string[]; packages: Map<string, Notice> } {
  let offset = 0;
  const firstParty: string[] = [], packages = new Map<string, Notice>();
  function line(): string {
    const end = bytes.indexOf(10, offset);
    if (end < 0) throw new Error("Unterminated notice line");
    const value = bytes.subarray(offset, end).toString("utf8"); offset = end + 1;
    return value;
  }
  function field(prefix: string): string {
    const value = line();
    expect(value.startsWith(prefix)).toBe(true);
    return value.slice(prefix.length);
  }
  while (offset < bytes.length) {
    const value = line();
    if (value.startsWith("First-party package: ")) firstParty.push(value.slice("First-party package: ".length));
    const match = /^=== BEGIN THIRD-PARTY PACKAGE (.+) ===$/.exec(value);
    if (!match) continue;
    const id = match[1];
    expect(packages.has(id)).toBe(false);
    const name = field("Name: "), version = field("Version: "), license = field("package.json license: ");
    expect(`${name}@${version}`).toBe(id);
    expect(name.startsWith("@traceai/")).toBe(false);
    expect(license.trim()).not.toBe("");
    const countText = field("License files: ");
    expect(countText).toMatch(/^[1-9][0-9]*$/);
    const count = Number(countText);
    expect(Number.isSafeInteger(count)).toBe(true);
    const files: Notice["files"] = [];
    for (let i = 0; i < count; i++) {
      const header = /^--- BEGIN LICENSE FILE (".*") \(([0-9]+) bytes\) ---$/.exec(line());
      if (!header) throw new Error(`Missing license file header for ${id}`);
      const file: unknown = JSON.parse(header[1]), length = Number(header[2]);
      expect(typeof file).toBe("string");
      expect(String(file).split("/").pop()).toMatch(/^(?:LICENSE|LICENCE|COPYING|NOTICE)/i);
      expect(Number.isSafeInteger(length)).toBe(true);
      expect(length).toBeGreaterThan(0);
      expect(offset + length).toBeLessThanOrEqual(bytes.length);
      const text = bytes.subarray(offset, offset + length); offset += length;
      expect(text.toString("utf8").trim()).not.toBe("");
      expect(line()).toBe("");
      expect(line()).toBe("--- END LICENSE FILE ---");
      files.push({ file: String(file), text });
    }
    expect(line()).toBe("=== END THIRD-PARTY PACKAGE ===");
    packages.set(id, { name, version, license, files });
  }
  return { firstParty, packages };
}

function checkArtifacts(manifestPath: string, noticesPath: string) {
  const manifest: Record<string, string[]> = JSON.parse(fs.readFileSync(manifestPath, "utf8"));
  expect(manifest).not.toBeNull();
  expect(Array.isArray(manifest)).toBe(false);
  expect(Object.keys(manifest)).toEqual(bundles);
  const ids = new Set<string>();
  for (const bundle of bundles) {
    expect(Array.isArray(manifest[bundle])).toBe(true);
    expect(manifest[bundle]).toEqual([...new Set(manifest[bundle])].sort());
    for (const id of manifest[bundle]) {
      expect(typeof id).toBe("string");
      expect(id).toMatch(/^(?:@[^/\s@]+\/)?[^/\s@]+@[^/\s@]+$/);
      expect(id.startsWith("@traceai/")).toBe(false);
      ids.add(id);
    }
  }
  // Do not let an empty manifest make the package-completeness assertion vacuous.
  for (const bundle of bundles.filter(bundle => bundle.startsWith("dist/agent/"))) {
    expect(manifest[bundle].length).toBeGreaterThan(0);
  }
  const notices = parseNotices(fs.readFileSync(noticesPath));
  expect([...notices.packages.keys()]).toEqual([...ids].sort());
  expect(notices.firstParty).toEqual([...new Set(notices.firstParty)].sort());
  expect(notices.firstParty.some(id => id.startsWith("@traceai/auto@"))).toBe(true);
  expect(notices.firstParty.every(id => id.startsWith("@traceai/"))).toBe(true);
  return { manifest, notices };
}

test("built notices contain nonempty license texts for every third-party package in all four bundles", () => {
  // The package must be built before this suite; missing artifacts fail, never skip.
  checkArtifacts(join(root, "dist/third-party-manifest.json"), join(root, "dist/THIRD-PARTY-NOTICES"));
  expect(JSON.parse(fs.readFileSync(join(root, "package.json"), "utf8")).files).toContain("dist");
  expect(fs.readFileSync(join(root, "README.md"), "utf8")).toContain("dist/THIRD-PARTY-NOTICES");
});

describe("build-time notice completeness", () => {
  let directory: string, packageRoot: string, openai: string, hook: string, register: string;
  let inputs: Record<string, string[]>;
  function write(file: string, text: string) {
    fs.mkdirSync(dirname(file), { recursive: true }); fs.writeFileSync(file, text);
  }
  function dependency(location: string, name: string, version = "1.0.0", licenses: Record<string, string> = {}) {
    const target = join(directory, location);
    write(join(target, "package.json"), JSON.stringify({ name, version, license: "MIT" }));
    write(join(target, "index.js"), "module.exports = {};\n");
    for (const [file, text] of Object.entries(licenses)) write(join(target, file), text);
    return join(target, "index.js");
  }
  beforeEach(() => {
    directory = fs.mkdtempSync(join(process.env.TRACEAI_TEST_TMP || tmpdir(), "traceai-notices-"));
    packageRoot = dirname(dependency("auto", "@traceai/auto"));
    openai = dependency("openai", "@traceai/openai");
    const middle = dependency("middle", "import-in-the-middle", "1.0.0", { LICENSE: "Fixture full license\n" });
    register = join(dirname(middle), "lib/register.js"); hook = join(dirname(middle), "hook.mjs");
    write(register, "module.exports = {};\n"); write(hook, "export {};\n");
    write(join(packageRoot, "src/register.ts"), "export {};\n");
    write(join(packageRoot, "src/edge.ts"), "export {};\n");
    inputs = {
      "dist/agent/runtime.cjs": [join(packageRoot, "src/register.ts"), openai, middle],
      "dist/agent/hook-register.cjs": [register],
      "dist/agent/hook.mjs": [hook],
      "dist/edge/index.mjs": [join(packageRoot, "src/edge.ts")],
    };
  });
  afterEach(() => { if (directory) fs.rmSync(directory, { recursive: true, force: true }); });

  async function buildNotices() {
    const build = jest.fn(async (options: { outfile: string; metafile: boolean; absWorkingDir: string }) => {
      expect(options.metafile).toBe(true); expect(options.absWorkingDir).toBe(packageRoot);
      const bundle = relative(packageRoot, options.outfile).split("\\").join("/");
      return { metafile: { inputs: Object.fromEntries(inputs[bundle].map(file => [relative(packageRoot, file), {}])) } };
    });
    const loader: any = (name: string) => {
      if (name === "esbuild") return { build };
      if (name === "node:module") return { createRequire: () => ({ resolve: (id: string) => {
        if (id === "import-in-the-middle/hook.mjs") return hook;
        if (id === "import-in-the-middle/lib/register.js") return register;
        throw new Error(`Unexpected hook fixture resolution ${id}`);
      } }) };
      if (name === "node:fs") return fs;
      if (name === "node:path") return require("node:path");
      throw new Error(`Unexpected build fixture import ${name}`);
    };
    loader.resolve = (name: string) => {
      if (name === "@traceai/openai") return openai;
      if (name === "@opentelemetry/instrumentation") return register;
      throw new Error(`Unexpected build fixture resolution ${name}`);
    };
    const error = jest.fn(), result = { exitCode: 0 };
    // Exercise the actual build script with controlled metafiles and on-disk package
    // fixtures; no real bundle build or repo output is performed by these controls.
    await runInNewContext(fs.readFileSync(join(root, "scripts/build-agent.cjs"), "utf8"), {
      require: loader, __dirname: join(packageRoot, "scripts"), Buffer,
      process: result, console: { error, log() {} },
    });
    return { build, error, result };
  }

  test("walks nameless and symlinked package roots, retains all license bytes, and emits deterministically", async () => {
    const full = "Fixture copyright: café\r\nPermission text without trailing newline";
    const vendor = dependency("store/vendor", "fixture-vendor", "2.0.0", {
      "LICENSE.txt": full,
      "docs/LICENCE-MIT": "Fixture alternate spelling\n",
      "docs/COPYING": "Fixture copying terms\n",
      "docs/NOTICE": "Fixture notices\n",
    });
    write(join(dirname(vendor), "esm/package.json"), '{"type":"module"}');
    write(join(dirname(vendor), "esm/index.js"), "export {};\n");
    const link = join(directory, "vendor-link"); fs.symlinkSync(dirname(vendor), link, "dir");
    inputs["dist/agent/runtime.cjs"].push(join(link, "esm/index.js"));
    // A second installed copy with the same id must still contribute distinct texts.
    const copy = dependency("copy/vendor", "fixture-vendor", "2.0.0", {
      "LICENSE.txt": full, "NOTICE": "Different installed-copy notice\n",
    });
    inputs["dist/edge/index.mjs"].push(copy);
    const built = await buildNotices();
    expect(built.result.exitCode).toBe(0); expect(built.error).not.toHaveBeenCalled();
    expect(built.build).toHaveBeenCalledTimes(4);
    const noticesPath = join(packageRoot, "dist/THIRD-PARTY-NOTICES"), manifestPath = join(packageRoot, "dist/third-party-manifest.json");
    const { notices } = checkArtifacts(manifestPath, noticesPath);
    const entry = notices.packages.get("fixture-vendor@2.0.0")!;
    expect(entry.license).toBe('"MIT"');
    expect(entry.files.map(file => file.file)).toEqual(["LICENSE.txt", "NOTICE", "docs/COPYING", "docs/LICENCE-MIT", "docs/NOTICE"]);
    expect(entry.files[0].text.equals(Buffer.from(full))).toBe(true);
    const before = [fs.readFileSync(noticesPath), fs.readFileSync(manifestPath)];
    for (const bundle of bundles) inputs[bundle].reverse();
    const rebuilt = await buildNotices();
    expect(rebuilt.result.exitCode).toBe(0); expect(rebuilt.error).not.toHaveBeenCalled();
    expect(fs.readFileSync(noticesPath).equals(before[0])).toBe(true);
    expect(fs.readFileSync(manifestPath).equals(before[1])).toBe(true);
  });

  test.each(bundles)("fails with the package id when an unlicensed input occurs only in %s", async bundle => {
    inputs[bundle].push(dependency("unlicensed", "fixture-unlicensed"));
    const built = await buildNotices();
    expect(built.result.exitCode).toBe(1);
    expect(String(built.error.mock.calls[0][0])).toContain("fixture-unlicensed@1.0.0");
    expect(fs.existsSync(join(packageRoot, "dist/THIRD-PARTY-NOTICES"))).toBe(false);
    expect(fs.existsSync(join(packageRoot, "dist/third-party-manifest.json"))).toBe(false);
  });

  test.each(["", " \r\n\t"])("fails for an empty license body (%j) even beside a valid notice", async text => {
    inputs["dist/edge/index.mjs"].push(dependency("empty", "fixture-empty", "1.0.0", { LICENSE: text, NOTICE: "Fixture notice\n" }));
    const built = await buildNotices();
    expect(built.result.exitCode).toBe(1);
    expect(String(built.error.mock.calls[0][0])).toContain("fixture-empty@1.0.0");
    expect(fs.existsSync(join(packageRoot, "dist/third-party-manifest.json"))).toBe(false);
  });

  test("does not hide an unlicensed installed copy behind another copy of the same package id", async () => {
    const copy = dependency("copy/middle", "import-in-the-middle");
    inputs["dist/edge/index.mjs"].push(copy);
    const built = await buildNotices();
    expect(built.result.exitCode).toBe(1);
    expect(String(built.error.mock.calls[0][0])).toContain("import-in-the-middle@1.0.0");
    expect(fs.existsSync(join(packageRoot, "dist/third-party-manifest.json"))).toBe(false);
  });

  test("rejects absent metafile coverage instead of emitting an empty bundle inventory", async () => {
    inputs["dist/edge/index.mjs"] = [];
    const built = await buildNotices();
    expect(built.result.exitCode).toBe(1);
    expect(String(built.error.mock.calls[0][0])).toContain("Missing metafile inputs for dist/edge/index.mjs");
  });
});
