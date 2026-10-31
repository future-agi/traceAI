const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { spawn, spawnSync } = require("node:child_process");
const { paths, saveConfig, loadConfig } = require("../dist/src/config.js");
const { Spool } = require("../dist/src/storage/store.js");
const bin = path.resolve(__dirname, "../bin/fi-agent.cjs");

test("cleanup retries a losing daemon start while the previous storage owner is closing", async (t) => {
  const base = path.resolve(__dirname, "../../../../../t");
  fs.mkdirSync(base, { recursive: true, mode: 0o700 });
  const root = fs.mkdtempSync(path.join(base, "r-"));
  const env = {
    PATH: process.env.PATH,
    HOME: root,
    FI_AGENT_CONFIG_HOME: path.join(root, "c"),
    FI_AGENT_STATE_HOME: path.join(root, "s"),
  };
  const p = paths(env);
  const route = {
    id: "cursor.ide",
    binding: {
      id: "00000000-0000-4000-8000-000000000001",
      endpoint: "http://127.0.0.1:1/v1/traces",
      approvedOrigin: "http://127.0.0.1:1",
      project: "fixture",
      loopbackTest: true,
    },
    policy: { revision: "1", categories: [], ceiling: [] },
    enabled: true,
    epoch: 1,
  };
  saveConfig(p, {
    schema_version: 1,
    profiles: {},
    routes: {
      [route.id]: {
        route,
        agent: "cursor",
        surface: "ide",
        profile: "removed",
        transcriptConsent: false,
        captureMode: "hooks",
        activation: {
          installed: false,
          trusted: "unknown",
          activated: false,
          restartRequired: false,
        },
      },
    },
  });
  const owner = new Spool(path.join(p.state, "spool"), "0.1.0", true);
  owner.route(route);
  let released = false,
    timer;
  t.after(() => {
    clearTimeout(timer);
    if (!released) owner.close();
    spawnSync(process.execPath, [bin, "runtime", "stop"], {
      env,
      timeout: 10000,
    });
    fs.rmSync(root, { recursive: true, force: true });
  });
  const child = spawn(
    process.execPath,
    [bin, "disable", "cursor", "--surface", "ide"],
    { env, stdio: ["ignore", "pipe", "pipe"] },
  );
  let output = "";
  child.stdout.on("data", (b) => {
    output += b;
  });
  // Keep the prior owner for longer than one qualified 500ms ownership attempt,
  // but within the administrative startup budget. No lock stealing or longer
  // SQLite timeout is needed; the successor may retry only after a losing exit.
  timer = setTimeout(() => {
    assert.equal(loadConfig(p).routes[route.id].route.enabled, false);
    owner.close();
    released = true;
  }, 1100);
  const code = await new Promise((resolve, reject) => {
    child.once("exit", resolve);
    child.once("error", reject);
  });
  assert.equal(code, 0, output);
  assert.equal(released, true);
  assert.equal(loadConfig(p).routes[route.id].route.enabled, false);
});
