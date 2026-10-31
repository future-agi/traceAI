"use strict";
// Runs against the installed tarball, never workspace node_modules or a user home.
const { createRequire } = require("node:module");
const fs = require("node:fs");
const path = require("node:path");
const { spawnSync } = require("node:child_process");
const assert = require("node:assert/strict");
(async () => {
  const [directory, name] = process.argv.slice(2),
    load = createRequire(path.join(directory, "package.json"));
  const root = path.join(directory, "node_modules", name);
  for (const schema of [
    "canonical-fact-v1",
    "capability-v1",
    "identity-receipt-v1",
    "usage-plan-v1",
  ])
    assert.equal(
      JSON.parse(
        fs.readFileSync(path.join(root, "schema", schema + ".schema.json")),
      ).type,
      "object",
    );
  if (name === "@traceai/agent-core") {
    for (const entry of ["client", "identity", "schema", "privacy", "usage"])
      assert.ok(Object.keys(load(name + "/" + entry)).length > 0);
  } else {
    const home = fs.mkdtempSync(
      path.join(fs.realpathSync(directory), "smoke-home-"),
    );
    fs.chmodSync(home, 0o700);
    try {
      const env = {
        PATH: process.env.PATH,
        HOME: home,
        FI_AGENT_CONFIG_HOME: path.join(home, "config"),
        FI_AGENT_STATE_HOME: path.join(home, "state"),
      };
      const binary = path.join(root, "bin/fi-agent.cjs");
      assert.ok(fs.statSync(binary).mode & 0o111);
      const version = spawnSync(process.execPath, [binary, "--version"], {
        env,
        encoding: "utf8",
        timeout: 10000,
      });
      assert.equal(version.status, 0);
      assert.match(version.stdout, /^\d+\.\d+\.\d+/);
      const hook = spawnSync(
        process.execPath,
        [path.join(root, "bin/fi-agent-hook.cjs")],
        {
          env,
          input: '{"invalid":"synthetic"}',
          encoding: "utf8",
          timeout: 10000,
        },
      );
      assert.equal(hook.status, 0);
      assert.equal(hook.stdout, "");
      assert.equal(hook.stderr, "");
      const cursorHook = path.join(root, "bin/fi-cursor-hook.cjs");
      // Internal entrypoint is invoked by the resolved Node executable, not as a PATH bin.
      assert.ok(fs.statSync(cursorHook).isFile());
      const cursor = spawnSync(process.execPath, [cursorHook], {
        env,
        input: '{"text":"SYNTHETIC_PRIVATE_CANARY"}',
        encoding: "utf8",
        timeout: 10000,
      });
      assert.equal(cursor.status, 0);
      assert.equal(cursor.stdout, "");
      assert.equal(cursor.stderr, "");
      const { cursorAdapters } = load(
        path.join(root, "dist/src/adapters/cursor/index.js"),
      );
      const adapters = cursorAdapters({
        node: process.execPath,
        hookEntry: cursorHook,
      });
      assert.deepEqual(
        adapters.map((a) => a.capability.surface),
        ["ide", "cli"],
      );
      const esm = await import(
        require("node:url").pathToFileURL(
          path.join(root, "dist/esm/adapters/cursor/index.js"),
        ).href
      );
      assert.equal(
        esm.cursorAdapters({ node: process.execPath, hookEntry: cursorHook })
          .length,
        2,
      );
      assert.deepEqual(
        JSON.parse(
          fs.readFileSync(path.join(root, "profiles/capabilities.json")),
        ).enabled_by_default,
        [],
      );
      const { StorageClient } = load(name);
      const client = new StorageClient(
        path.join(root, "dist/src/runtime/storage-child.js"),
      );
      try {
        await client.call("init", {
          root: path.join(home, "spool"),
          version: "0.1.0",
        });
        assert.equal((await client.call("status")).state, "ready");
      } finally {
        await client.close();
      }
    } finally {
      fs.rmSync(home, { recursive: true, force: true });
    }
  }
  console.log("agent tarball assets and runtime smoke passed");
})().catch(() => {
  console.error("agent tarball smoke failed");
  process.exitCode = 1;
});
