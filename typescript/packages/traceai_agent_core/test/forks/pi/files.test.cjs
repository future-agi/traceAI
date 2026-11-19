const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");
const os = require("node:os");
const { fork } = require("node:child_process");
const { once } = require("node:events");
const core = require("../../../dist/src/index.js");
const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
async function fixture(t) {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), "fi-lock-"));
  t.after(() => fs.rm(dir, { recursive: true, force: true }));
  const file = path.join(dir, "config.json");
  await core.writeProtected(file, '{"count":0}');
  return file;
}
async function hold(t, file) {
  const child = fork(path.join(__dirname, "fixtures/hold-write.cjs"), [file], {
    stdio: ["ignore", "ignore", "inherit", "ipc"],
  });
  t.after(() => {
    if (child.exitCode === null) child.kill("SIGKILL");
  });
  const [message] = await once(child, "message");
  assert.equal(message.type, "held");
  return child;
}
test(
  "overlapping live writers wait, renew their lease, and re-read after a stale merge",
  { timeout: 20000 },
  async (t) => {
    const file = await fixture(t);
    const child = await hold(t, file);
    assert.equal((await fs.stat(`${file}.write-lock`)).mode & 0o777, 0o700);
    let complete = false;
    const write = core
      .updateJson(file, (old) => ({ count: old.count + 1 }))
      .then(() => {
        complete = true;
      });
    // Longer than the stale threshold: a live heartbeat must not be stolen.
    await delay(10500);
    assert.equal(complete, false);
    child.send("release");
    await write;
    assert.equal(JSON.parse(await fs.readFile(file, "utf8")).count, 2);
    await assert.rejects(fs.lstat(`${file}.write-lock`), { code: "ENOENT" });
  },
);
test(
  "SIGKILL writer leaves a recoverable lease and does not apply its unfinished update",
  { timeout: 20000 },
  async (t) => {
    const file = await fixture(t);
    const child = await hold(t, file);
    const exit = once(child, "exit");
    child.kill("SIGKILL");
    await exit;
    const start = Date.now();
    await core.updateJson(file, (old) => ({ ...old, disabled: true }));
    assert(Date.now() - start < 15000);
    assert.deepEqual(JSON.parse(await fs.readFile(file, "utf8")), {
      count: 0,
      disabled: true,
    });
    await assert.rejects(fs.lstat(`${file}.write-lock`), { code: "ENOENT" });
  },
);
test("lock paths reject symlinks, regular files, broad modes and foreign ownership", async (t) => {
  const file = await fixture(t);
  const lock = `${file}.write-lock`;
  const target = `${file}.target`;
  await fs.mkdir(target, { mode: 0o700 });
  await fs.symlink(target, lock);
  await assert.rejects(core.writeProtected(file, "{}"), /UNSAFE_LOCK/);
  assert((await fs.lstat(lock)).isSymbolicLink());
  await fs.unlink(lock);
  await fs.writeFile(lock, "not a lease", { mode: 0o600 });
  await assert.rejects(core.writeProtected(file, "{}"), /UNSAFE_LOCK/);
  await fs.unlink(lock);
  await fs.mkdir(lock, { mode: 0o700 });
  await fs.chmod(lock, 0o777);
  await assert.rejects(core.writeProtected(file, "{}"), /UNSAFE_LOCK/);
  await fs.chmod(lock, 0o700);
  if (process.getuid?.() === 0) {
    await fs.chown(lock, 65534, 65534);
    await assert.rejects(core.writeProtected(file, "{}"), /UNSAFE_LOCK/);
    await fs.chown(lock, 0, 0);
  }
  assert.equal(await fs.readFile(file, "utf8"), '{"count":0}');
});
