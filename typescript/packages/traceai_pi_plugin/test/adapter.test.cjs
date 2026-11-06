const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");
const os = require("node:os");
const c = require("@traceai/agent-core");
const {
  piAdapter,
  PACKAGE_SOURCE,
  supportedPiVersion,
} = require("../dist/adapter.js");
test("compatible host updates do not require exact pinned-version equality", () => {
  for (const version of ["1.5.0", "1.5.1", "1.6.0", "1.99.4"])
    assert.equal(supportedPiVersion(version), true, version);
  for (const version of [
    "1.4.9",
    "2.0.0",
    "unknown",
    "1.5.0-beta.1",
    "1.5.0\n",
  ])
    assert.equal(supportedPiVersion(version), false, version);
});
async function fixture(t) {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "pi-adapter-"));
  t.after(() => fs.rm(root, { recursive: true, force: true }));
  const host = path.join(root, "custom-agent"),
    project = path.join(root, "project");
  await fs.mkdir(host);
  await fs.mkdir(project);
  const config = await c.resolveConfig({
    agent: "pi",
    home: root,
    env: { PI_CODING_AGENT_DIR: host },
    projectDir: project,
  });
  const context = { config, scope: "user", projectDir: project };
  const settings = path.join(host, "settings.json");
  const installed = async () => {
    const dest = path.join(host, "npm/node_modules/@traceai/pi-plugin");
    await fs.mkdir(dest, { recursive: true });
    await fs.writeFile(
      path.join(dest, "package.json"),
      JSON.stringify({
        version: "0.1.0",
        pi: { extensions: ["./extension.mjs"] },
      }),
    );
  };
  return { root, host, project, settings, context, installed };
}
test("host detection uses the executable, including group-writable application locations", async (t) => {
  const f = await fixture(t);
  const bin = path.join(f.root, "Applications");
  await fs.mkdir(bin, { mode: 0o775 });
  await fs.chmod(bin, 0o775);
  const originalPath = process.env.PATH;
  process.env.PATH = `${bin}:${originalPath}`;
  t.after(() => {
    process.env.PATH = originalPath;
  });
  for (const version of ["1.5.1", "1.6.0", "2.0.0"]) {
    await fs.writeFile(
      path.join(bin, "pi"),
      `#!/bin/sh\nprintf '${version}\\n'\n`,
      { mode: 0o755 },
    );
    const result = await piAdapter.detect(f.context);
    assert.equal(result.version, version);
    assert.equal(result.supported, version !== "2.0.0");
  }
});
test("manifest alone is not an installed native extension", async (t) => {
  const f = await fixture(t);
  await fs.writeFile(
    f.settings,
    JSON.stringify({ packages: [PACKAGE_SOURCE] }),
  );
  assert.equal((await piAdapter.inspect(f.context)).registered, false);
  await f.installed();
  assert.equal((await piAdapter.inspect(f.context)).registered, true);
});
test("user and project declarations are reported as duplicates", async (t) => {
  const f = await fixture(t);
  await f.installed();
  await fs.writeFile(
    f.settings,
    JSON.stringify({ packages: [PACKAGE_SOURCE] }),
  );
  await fs.mkdir(path.join(f.project, ".pi"));
  await fs.writeFile(
    path.join(f.project, ".pi/settings.json"),
    JSON.stringify({ packages: ["npm:@traceai/pi-plugin"] }),
  );
  assert.equal((await piAdapter.inspect(f.context)).duplicate, true);
});
test("copied legacy observer prevents installing a second observer", async (t) => {
  const f = await fixture(t);
  await fs.mkdir(path.join(f.host, "extensions"));
  await fs.writeFile(
    path.join(f.host, "extensions/futureagi-otlp.ts"),
    "// old",
  );
  const result = await piAdapter.install(f.context);
  assert.equal(result.registered, false);
  assert.equal(result.duplicate, true);
});
test("uninstall preserves a pre-existing native install that setup does not own", async (t) => {
  const f = await fixture(t);
  await f.installed();
  const text = JSON.stringify({
    packages: [PACKAGE_SOURCE, "npm:unrelated"],
    compaction: { enabled: false },
  });
  await fs.writeFile(f.settings, text);
  const result = await piAdapter.uninstall(f.context);
  assert.equal(result.detail, "NOT_OWNED_REMOVE_WITH_PI");
  assert.equal(await fs.readFile(f.settings, "utf8"), text);
});
test("unsafe symlink settings are rejected without following them", async (t) => {
  const f = await fixture(t);
  const target = path.join(f.root, "other.json");
  await fs.writeFile(target, "{}");
  await fs.symlink(target, f.settings);
  await assert.rejects(piAdapter.install(f.context));
  assert.equal(await fs.readFile(target, "utf8"), "{}");
});

test("resource enablement agrees with the real Pi package manager", async (t) => {
  const { pathToFileURL } = require("node:url");
  const { extensionEnabled } = require("../dist/adapter.js");
  const nativeRoot = await fs.realpath(path.resolve(__dirname, "../node_modules/@earendil-works/pi-coding-agent"));
  const { DefaultPackageManager } = await import(pathToFileURL(path.join(nativeRoot, "dist/core/package-manager.js")));
  const f = await fixture(t);
  await f.installed();
  const packageRoot = path.join(f.host, "npm/node_modules/@traceai/pi-plugin");
  await fs.writeFile(path.join(packageRoot, "extension.mjs"), "export default function(){}\n");
  const manager = new DefaultPackageManager({ cwd: f.project, agentDir: f.host, settingsManager: {} });
  for (const filter of [
    {}, { autoload: false }, { extensions: [] }, { extensions: ["-extension.mjs"] },
    { extensions: ["!**/*.mjs"] }, { extensions: ["other.mjs"] }, { extensions: ["*.mjs"] },
    { extensions: ["!**", "+extension.mjs"] }, { extensions: ["!**", "+extension.mjs", "-./extension.mjs"] },
    { autoload: false, extensions: ["+extension.mjs"] },
    { autoload: false, extensions: ["+extension.mjs", "!**/*.mjs"] },
    { autoload: false, extensions: ["!**/*.mjs", "+extension.mjs"] },
    { extensions: ["-**"] },
  ]) {
    const entry = { source: PACKAGE_SOURCE, ...filter };
    const accumulator = manager.createAccumulator();
    manager.collectPackageResources(packageRoot, accumulator, entry, { source: PACKAGE_SOURCE, scope: "user", origin: "package" });
    const nativeEnabled = [...accumulator.extensions.values()].some((x) => x.enabled);
    assert.equal(extensionEnabled(entry, packageRoot), nativeEnabled, JSON.stringify(filter));
    const text = JSON.stringify({ packages: [entry] });
    await fs.writeFile(f.settings, text);
    const inspected = await piAdapter.inspect(f.context);
    assert.equal(inspected.registered, nativeEnabled, JSON.stringify(filter));
    if (!nativeEnabled) {
      assert(inspected.issues.includes("EXTENSION_DISABLED"));
      assert.equal((await piAdapter.install(f.context)).registered, false);
      assert.equal(await fs.readFile(f.settings, "utf8"), text, "explicit native disablement is never repaired silently");
    }
  }
});

test("historical activation cannot hide a missing or unsupported Pi executable", async (t) => {
  const f = await fixture(t);
  await f.installed();
  await fs.writeFile(f.settings, JSON.stringify({ packages: [PACKAGE_SOURCE] }));
  const bin = path.join(f.root, "bin");
  await fs.mkdir(bin);
  const oldPath = process.env.PATH;
  process.env.PATH = bin;
  t.after(() => { process.env.PATH = oldPath; });
  await c.updateJson(c.deliveryStatePath(f.context.config), () => ({ exported: 1, dropped: 0, activatedAt: new Date().toISOString() }));
  const options = { agent: "pi", home: f.root, env: { PI_CODING_AGENT_DIR: f.host }, projectDir: f.project, adapter: piAdapter, check: true };
  const absent = await c.inspectStatus(options);
  assert(absent.issues.includes("PI_NOT_FOUND"));
  assert.equal(absent.exitCode, 2);
  await fs.writeFile(path.join(bin, "pi"), "#!/bin/sh\nprintf '2.0.0\\n'\n", { mode: 0o755 });
  const unsupported = await c.inspectStatus(options);
  assert(unsupported.issues.includes("PI_VERSION_NOT_QUALIFIED"));
  assert.equal(unsupported.exitCode, 2);
});
