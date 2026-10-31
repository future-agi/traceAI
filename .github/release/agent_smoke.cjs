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
      const codexHook = spawnSync(
        process.execPath,
        [
          path.join(root, "bin/fi-agent-codex-hook.cjs"),
          "--socket",
          "/missing-fixture.sock",
          "--route",
          "codex.cli",
        ],
        {
          env,
          input:
            '{"hook_event_name":"PostModelRequest","session_id":"fixture","transcript_path":null}',
          encoding: "utf8",
          timeout: 10000,
        },
      );
      assert.equal(codexHook.status, 0);
      assert.equal(codexHook.stdout, "");
      assert.equal(codexHook.stderr, "");
      assert.ok(
        fs.existsSync(path.join(root, "dist/src/adapters/codex/codec.js")),
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
