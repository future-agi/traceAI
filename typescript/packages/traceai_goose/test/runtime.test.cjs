const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");
const os = require("node:os");
const { execFileSync } = require("node:child_process");
const { createHash } = require("node:crypto");
const {
  supportedGooseVersion,
  interpreterPath,
  pruneRuntimes,
} = require("../dist/install.js");
test("compatible stable Goose 1.x versions are accepted without exact patch pinning", () => {
  for (const v of ["1.54.0", "1.54.1", "1.55.0", "1.100.12"])
    assert.equal(supportedGooseVersion(v), true, v);
  for (const v of ["1.53.9", "2.0.0", "1.54.0-beta", "bad", undefined])
    assert.equal(supportedGooseVersion(v), false, v);
});
test("nonterminal hooks do not initialize the OTel exporter", () => {
  execFileSync(process.execPath, [
    "-e",
    `require(${JSON.stringify(require.resolve("../dist/hook.js"))}); if (Object.keys(require.cache).some(p => p.includes('@opentelemetry'))) throw Error('eager OTel import');`,
  ]);
});
test("pin an existing interpreter symlink and prune only intact owned runtimes", async (t) => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "goose-runtime-"));
  t.after(() => fs.rm(root, { recursive: true, force: true }));
  const stable = path.join(root, "stable");
  await fs.mkdir(stable);
  await fs.symlink(process.execPath, path.join(stable, "node"));
  assert.equal(
    await interpreterPath(process.execPath, stable),
    path.join(stable, "node"),
  );
  assert.equal(
    await interpreterPath(
      process.execPath,
      path.dirname(await fs.realpath(process.execPath)) + ":" + stable,
    ),
    path.join(stable, "node"),
  );
  assert.equal(
    await interpreterPath(process.execPath, "/missing"),
    process.execPath,
  );
  async function runtime(text, modified = false) {
    const hash = createHash("sha256").update(text).digest("hex");
    const dir = path.join(root, "runtime", "goose-0.1.0-" + hash.slice(0, 16));
    await fs.mkdir(dir, { recursive: true, mode: 0o700 });
    const file = path.join(dir, "hook.cjs");
    await fs.writeFile(file, modified ? "changed" : text, { mode: 0o600 });
    return file;
  }
  const old = await runtime("x".repeat(2 * 1024 * 1024));
  const keep = await runtime("current");
  const changed = await runtime("old-but-edited", true);
  await pruneRuntimes(root, keep);
  await assert.rejects(fs.stat(old), { code: "ENOENT" });
  await fs.stat(keep);
  await fs.stat(changed);
  await pruneRuntimes(root);
  await assert.rejects(fs.stat(keep), { code: "ENOENT" });
  await fs.stat(changed);
});
