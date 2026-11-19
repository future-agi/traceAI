const { test } = require("node:test");
const assert = require("node:assert/strict");
const { fixture, core, fs, path } = require("./helpers.cjs");
const {
  mergeHooks,
  editHostFile,
  commands,
  register,
  unregister,
  inspect,
  installRuntime,
} = require("../dist/registration.js");
const { EVENTS, cursorAdapter, qualification } = require("../dist/index.js");
const parse = require("jsonc-parser").parse;

test("fresh and existing hooks merge idempotently; comments and unrelated hooks survive uninstall", async (t) => {
  const f = await fixture(t),
    file = path.join(f.home, ".cursor/hooks.json");
  const original =
    '{\n // keep this comment\n "version":1,\n "hooks":{"stop":[\n // owned by someone else\n {"command":"other-hook", "timeout":9}], "unknownHook":[]},\n "custom":true\n}\n';
  await fs.writeFile(file, original, { mode: 0o644 });
  const runtime = await installRuntime(f.context);
  assert.equal((await register(f.context, runtime)).registered, true);
  const first = await fs.readFile(file, "utf8");
  assert.match(first, /keep this comment/);
  assert.match(first, /owned by someone else/);
  assert.equal(parse(first).custom, true);
  assert.equal(parse(first).hooks.stop.length, 2);
  assert.equal((await register(f.context, runtime)).registered, true);
  assert.equal(await fs.readFile(file, "utf8"), first);
  await unregister(f.context);
  const after = await fs.readFile(file, "utf8");
  assert.equal(parse(after).hooks.stop.length, 1);
  assert.equal(parse(after).hooks.stop[0].command, "other-hook");
  assert.match(after, /owned by someone else/);
  assert.equal((await inspect(f.context)).registered, false);
});
test("ownership never adopts modified or legacy hook entries", () => {
  const owned = commands("/home/test/runtime", "/home/test/config");
  const raw = JSON.stringify({
    version: 1,
    hooks: { stop: [{ ...owned.stop, timeout: 99 }] },
  });
  assert.throws(() => mergeHooks(raw, [owned], owned), /OWNED_HOOK_MODIFIED/);
  assert.throws(
    () =>
      mergeHooks(
        JSON.stringify({
          version: 1,
          hooks: { stop: [{ command: "fi-hook-to-otlp", timeout: 1 }] },
        }),
        [],
        owned,
      ),
    /DUPLICATE_REGISTRATION/,
  );
  assert.throws(
    () => mergeHooks('{"version":1,"version":1,"hooks":{}}', [], owned),
    /DUPLICATE_JSON_KEY/,
  );
});
test("duplicate project registration is reported; companion plugin has no capture hooks", async (t) => {
  const f = await fixture(t);
  await register(f.context, await installRuntime(f.context));
  const projectDir = path.join(f.home, "project");
  await fs.mkdir(path.join(projectDir, ".cursor"), {
    recursive: true,
    mode: 0o700,
  });
  await fs.writeFile(
    path.join(projectDir, ".cursor/hooks.json"),
    JSON.stringify({
      version: 1,
      hooks: { stop: [{ command: "@traceai/cursor-plugin", timeout: 1 }] },
    }),
  );
  assert.equal((await inspect({ ...f.context, projectDir })).duplicate, true);
  assert.deepEqual(
    JSON.parse(await fs.readFile(path.join(__dirname, "../hooks/hooks.json")))
      .hooks,
    {},
  );
});
test("symlinks and concurrent changes fail closed without overwriting host edits", async (t) => {
  const f = await fixture(t),
    victim = path.join(f.home, "victim"),
    file = path.join(f.home, ".cursor/hooks.json");
  await fs.writeFile(victim, "victim");
  await fs.symlink(victim, file);
  await assert.rejects(editHostFile(file, () => "{}"));
  assert.equal(await fs.readFile(victim, "utf8"), "victim");
  await fs.unlink(file);
  await fs.writeFile(file, '{"version":1,"hooks":{}}');
  let edits = 0;
  await editHostFile(file, (before) => {
    if (edits++ === 0)
      require("node:fs").writeFileSync(
        file,
        '{"version":1,"hooks":{},"hostSaved":true}',
      );
    return before.replace('"hooks":{}', '"hooks":{},"adapterSaved":true');
  });
  assert.equal(JSON.parse(await fs.readFile(file)).hostSaved, true);
  assert.equal(edits, 2);
});
test("runtime pins existing Node and private bundles, not copied executables", async (t) => {
  const f = await fixture(t),
    runtime = await installRuntime(f.context);
  assert.ok(
    runtime.startsWith(
      path.join(f.home, ".local/share/futureagi/agents/runtime/"),
    ),
  );
  for (const name of ["observer.cjs", "hook.cjs"])
    assert.ok((await fs.stat(path.join(runtime, name))).isFile());
  await assert.rejects(fs.stat(path.join(runtime, "node")), { code: "ENOENT" });
  assert.equal(await installRuntime(f.context), runtime);
  for (const hook of Object.values(
    commands(runtime, f.config.paths.configDir),
  )) {
    assert.ok(!hook.command.includes("npx"));
    assert.ok(hook.command.includes(runtime));
    assert.ok(hook.command.includes(process.execPath));
    assert.equal(hook.timeout, 10);
    assert.equal(hook.failClosed, false);
  }
});
test("host detection never becomes a registry or Linux GUI success claim", async (t) => {
  const f = await fixture(t);
  assert.equal(qualification.live, true);
  assert.deepEqual(qualification.blockers, ["NPM_AVAILABILITY_REQUIRED"]);
  assert.equal(cursorAdapter.requiresWorker, false);
  await assert.rejects(
    cursorAdapter.install(f.context),
    /UNSUPPORTED_HOST_VERSION/,
  );
  const result = await core.setup({ ...f.options, adapter: cursorAdapter });
  assert.equal(result.registered, false);
  assert.equal(result.code, "UNSUPPORTED_HOST_VERSION");
  assert.equal((await inspect(f.context)).registered, false);
});
