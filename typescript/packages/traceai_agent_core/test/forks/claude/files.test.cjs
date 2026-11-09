const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const os = require("node:os");
const path = require("node:path");
const { spawn } = require("node:child_process");
const files = require("../../../dist/src/files.js");

async function fixture(t) {
  const home = await fs.mkdtemp(path.join(os.tmpdir(), "fi-atomic-lock-"));
  t.after(() => fs.rm(home, { force: true, recursive: true }));
  return path.join(home, "state.json");
}

// Pause a real writeProtected after its lease acquisition but before the atomic rename.
async function heldWriter(t, file) {
  const code = `
    const fs = require('node:fs/promises');
    const original = fs.open;
    fs.open = async function(p, ...args) {
      if (p.startsWith(process.argv[1] + '.') && p.endsWith('.tmp')) {
        process.send('locked');
        setInterval(() => {}, 1000);
        await new Promise(() => {});
      }
      return original.call(this, p, ...args);
    };
    require(${JSON.stringify(require.resolve("../../../dist/src/files.js"))})
      .writeProtected(process.argv[1], '{"writer":true}').catch(() => process.exit(2));
  `;
  const writer = spawn(process.execPath, ["-e", code, file], {
    stdio: ["ignore", "ignore", "pipe", "ipc"],
  });
  const exited = new Promise((resolve) => writer.once("exit", resolve));
  t.after(async () => {
    writer.kill("SIGKILL");
    await exited;
  });
  await new Promise((resolve, reject) => {
    const timer = setTimeout(
      () => reject(new Error("writer did not acquire lock")),
      5000,
    );
    writer.once("message", (message) => {
      clearTimeout(timer);
      assert.equal(message, "locked");
      resolve();
    });
    writer.once("error", reject);
    writer.once("exit", () => {
      clearTimeout(timer);
      reject(new Error("writer exited before acquiring lock"));
    });
  });
  return async () => {
    writer.kill("SIGKILL");
    await exited;
  };
}

test("atomic writer crash recovers using a stale lease without deleting state", async (t) => {
  const file = await fixture(t);
  await files.updateJson(file, () => ({ previous: true }));
  const kill = await heldWriter(t, file);
  await kill();
  assert((await fs.stat(file + ".write-lock")).isDirectory());
  await files.updateJson(file, (old) => ({ ...old, recovered: true }));
  assert.deepEqual(await files.readJson(file), {
    previous: true,
    recovered: true,
  });
  await assert.rejects(fs.stat(file + ".write-lock"), { code: "ENOENT" });
});

test("a live writer renews its lease and a bounded waiter never steals it", async (t) => {
  const file = await fixture(t);
  await files.writeProtected(file, '{"original":true}');
  const kill = await heldWriter(t, file);
  const before = Date.now();
  await assert.rejects(files.writeProtected(file, "{}"), /CONCURRENT_SAVE/);
  assert(Date.now() - before < 15000);
  assert.deepEqual(await files.readJson(file), { original: true });
  assert((await fs.stat(file + ".write-lock")).isDirectory());
  await kill();
  await files.writeProtected(file, '{"after":true}');
  assert.deepEqual(await files.readJson(file), { after: true });
});

test("legacy ownerless crash residue is inert and unsafe new lock paths are rejected", async (t) => {
  const file = await fixture(t);
  await fs.writeFile(file + ".lock", "", { mode: 0o600 });
  const old = new Date(Date.now() - 3600000);
  await fs.utimes(file + ".lock", old, old);
  await files.updateJson(file, () => ({ recovered: true }));
  assert.deepEqual(await files.readJson(file), { recovered: true });
  // The obsolete lock is not unlinked: it could still belong to an older writer.
  assert((await fs.stat(file + ".lock")).isFile());
  const directory = path.join(path.dirname(file), "other");
  await fs.mkdir(directory);
  await fs.symlink(directory, file + ".write-lock");
  await assert.rejects(files.writeProtected(file, "{}"), /UNSAFE_LOCK/);
  assert.deepEqual(await fs.readdir(directory), []);
});
