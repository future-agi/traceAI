const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const os = require("node:os");
const path = require("node:path");
const { ohMyPiAdapter, PACKAGE_NAME } = require("../dist/adapter.js");
async function fixture(t) {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), "omp-adapter-"));
  const bin = path.join(dir, "bin");
  await fs.mkdir(bin);
  const state = path.join(dir, "native.json");
  await fs.writeFile(
    state,
    JSON.stringify({ version: "18.17.0", plugins: [], calls: [] }),
  );
  await fs.writeFile(
    path.join(bin, "omp"),
    `#!/usr/bin/env node\nconst fs=require('fs');const file=${JSON.stringify(state)};const s=JSON.parse(fs.readFileSync(file));const a=process.argv.slice(2);s.calls.push({args:a,credentialPresent:!!process.env.FI_API_KEY||!!process.env.FI_SECRET_KEY});if(a[0]==='--version')console.log('omp/'+s.version);else if(a[0]==='plugin'&&a[1]==='list')console.log(JSON.stringify({npm:s.plugins}));else if(a[0]==='install'){s.plugins=[{name:${JSON.stringify(PACKAGE_NAME)},version:'0.1.0',enabled:true,path:${JSON.stringify(path.join(dir, "installed"))},manifest:{extensions:['./extension.mjs']}}];console.log('{}')}else if(a[1]==='uninstall'){s.plugins=[];console.log('{}')}else process.exitCode=1;fs.writeFileSync(file,JSON.stringify(s));\n`,
    { mode: 0o700 },
  );
  const old = {
    PATH: process.env.PATH,
    FI_API_KEY: process.env.FI_API_KEY,
    FI_SECRET_KEY: process.env.FI_SECRET_KEY,
  };
  process.env.PATH = bin + ":" + old.PATH;
  process.env.FI_API_KEY = "synthetic-api";
  process.env.FI_SECRET_KEY = "synthetic-secret";
  t.after(async () => {
    for (const [k, v] of Object.entries(old))
      v === undefined ? delete process.env[k] : (process.env[k] = v);
    await fs.rm(dir, { recursive: true, force: true });
  });
  const c = {
    scope: "user",
    projectDir: dir,
    config: {
      paths: {
        configDir: path.join(dir, "fi"),
        user: path.join(dir, "agent/futureagi.json"),
      },
    },
  };
  return {
    dir,
    c,
    read: async () => JSON.parse(await fs.readFile(state, "utf8")),
    write: async (s) => fs.writeFile(state, JSON.stringify(s)),
  };
}
test("pinned host, native install, idempotence, no installer credentials and owned uninstall", async (t) => {
  const f = await fixture(t);
  assert.equal((await ohMyPiAdapter.detect(f.c)).supported, true);
  assert.equal((await ohMyPiAdapter.install(f.c)).registered, true);
  assert.equal((await ohMyPiAdapter.install(f.c)).registered, true);
  let s = await f.read();
  assert.equal(s.calls.filter((c) => c.args[0] === "install").length, 1);
  assert(s.calls.every((c) => !c.credentialPresent));
  assert.equal((await ohMyPiAdapter.uninstall(f.c)).registered, false);
});
test("auto-updates on the qualified major line remain supported", async (t) => {
  const f = await fixture(t);
  for (const version of ["18.17.1", "18.18.0", "18.99.7"]) {
    await f.write({ ...(await f.read()), version });
    assert.equal((await ohMyPiAdapter.detect(f.c)).supported, true, version);
  }
  for (const version of ["18.16.9", "19.0.0", "18.18.0-unknown", "unknown"]) {
    await f.write({ ...(await f.read()), version });
    assert.equal((await ohMyPiAdapter.detect(f.c)).supported, false, version);
  }
});
test("manual installs never become owned, disabled plugins not silently enabled", async (t) => {
  const f = await fixture(t);
  let s = await f.read();
  s.plugins = [
    {
      name: PACKAGE_NAME,
      version: "0.1.0",
      enabled: false,
      path: path.join(f.dir, "manual"),
      manifest: { extensions: ["./extension.mjs"] },
    },
  ];
  await f.write(s);
  assert.equal((await ohMyPiAdapter.install(f.c)).registered, false);
  assert.equal(
    (await ohMyPiAdapter.uninstall(f.c)).detail,
    "NOT_OWNED_OR_CHANGED_REMOVE_WITH_OMP",
  );
  assert.equal((await f.read()).plugins.length, 1);
});
test("legacy copied hooks block duplicate registration; unsupported host is explicit", async (t) => {
  const f = await fixture(t);
  await fs.mkdir(path.join(f.dir, ".omp/hooks/post"), { recursive: true });
  await fs.writeFile(
    path.join(f.dir, ".omp/hooks/post/futureagi-otlp.ts"),
    "// old",
  );
  assert.equal((await ohMyPiAdapter.install(f.c)).duplicate, true);
  const s = await f.read();
  s.version = "19.0.0";
  await f.write(s);
  assert.equal((await ohMyPiAdapter.detect(f.c)).supported, false);
  assert.equal(
    (await f.read()).calls.filter((c) => c.args[0] === "install").length,
    0,
  );
});
for (const scope of ["agent", ".omp"]) {
  for (const location of ["hooks/pre", "hooks/post", "extensions"]) {
    test(`migration refuses duplicate ${scope}/${location} and identifies removal path`, async (t) => {
      const f = await fixture(t);
      const file = path.join(f.dir, scope, location, "futureagi-otlp.ts");
      await fs.mkdir(path.dirname(file), { recursive: true });
      await fs.writeFile(file, "// synthetic legacy observer");
      const install = await ohMyPiAdapter.install(f.c);
      assert.equal(install.registered, false);
      assert.equal(install.duplicate, true);
      assert(install.detail.includes(file));
      assert.equal(
        (await f.read()).calls.filter((c) => c.args[0] === "install").length,
        0,
      );
      await fs.rm(file);
      assert.equal((await ohMyPiAdapter.install(f.c)).registered, true);
      await fs.writeFile(file, "// synthetic legacy observer");
      const status = await ohMyPiAdapter.inspect(f.c);
      assert.equal(status.registered, false);
      assert.equal(status.duplicate, true);
      assert(status.detail.includes(file));
    });
  }
}

test("update upgrades an unchanged setup-owned older registration and refreshes ownership", async (t) => {
  const f = await fixture(t);
  const core = require("@traceai/agent-core");
  await ohMyPiAdapter.install(f.c);
  const state = await f.read();
  state.plugins[0].version = "0.0.9";
  await f.write(state);
  const p = state.plugins[0];
  await core.updateJson(path.join(f.c.config.paths.configDir, "oh-my-pi-registration.json"), () => ({
    version: p.version, path: p.path, hash: core.contentHash(JSON.stringify(p)),
  }));
  assert.equal((await ohMyPiAdapter.install(f.c)).registered, true);
  assert.equal((await f.read()).plugins[0].version, "0.1.0");
  assert.equal((await f.read()).calls.filter((c) => c.args[0] === "install").length, 2);
  assert.equal((await ohMyPiAdapter.uninstall(f.c)).registered, false, "updated ownership supports safe uninstall");
});
for (const modified of ["manual", "manifest", "disabled"]) {
  test(`update refuses ${modified} older registrations`, async (t) => {
    const f = await fixture(t);
    const core = require("@traceai/agent-core");
    await ohMyPiAdapter.install(f.c);
    const state = await f.read(), p = state.plugins[0];
    p.version = "0.0.9";
    const ownerPath = path.join(f.c.config.paths.configDir, "oh-my-pi-registration.json");
    await core.updateJson(ownerPath, () => ({ version: p.version, path: p.path, hash: core.contentHash(JSON.stringify(p)) }));
    if (modified === "manual") await fs.rm(ownerPath);
    if (modified === "manifest") p.manifest.extensions.push("./custom.mjs");
    if (modified === "disabled") p.enabled = false;
    await f.write(state);
    const result = await ohMyPiAdapter.install(f.c);
    assert.equal(result.registered, false);
    assert.equal(result.detail, "EXISTING_PLUGIN_REQUIRES_MANUAL_UPDATE_OR_ENABLE");
    assert.equal((await f.read()).calls.filter((c) => c.args[0] === "install").length, 1);
  });
}
