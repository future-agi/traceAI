const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { adapterRegistry, adapterRelease, loadAdapter } = require("../dist/registry.js");

const qualified = ["codex", "opencode-v1", "antigravity", "copilot", "copilot-vscode", "hermes", "openclaw", "grok"];
const held = ["antigravity-ide", "muse", "gemini", "kiro-cli", "kiro-ide", "augment", "augment-ide", "devin-cascade"];

test("assembly retains every qualified and held registry entry without duplicates", () => {
  assert.equal(new Set(adapterRegistry.map((entry) => entry.id)).size, adapterRegistry.length);
  for (const id of qualified) {
    assert.equal(adapterRelease(id)?.qualified, true, id);
    assert.ok(adapterRelease(id)?.hostRange, id);
    assert.ok(loadAdapter(id), id);
  }
  for (const id of held) {
    assert.equal(adapterRelease(id)?.qualified, false, id);
    assert.ok(adapterRelease(id)?.reason, id);
  }
  for (const id of ["kiro-ide", "augment-ide", "devin-cascade"]) {
    const entry = adapterRelease(id);
    assert.ok(entry.setupBlocker || entry.setupHold, id);
  }
  // Every merged group dispatches to its own adapter; held ones keep a reason.
  for (const id of ["claude", "goose", "oh-my-pi", "pi", "opencode", "cursor", "cursor-cli", "cursor-cloud"]) {
    assert.ok(loadAdapter(id), id);
    assert.equal(loadAdapter(id).id, id, id);
  }
  for (const id of ["claude", "goose", "oh-my-pi", "pi", "cursor", "cursor-cli"]) assert.equal(adapterRelease(id)?.qualified, true, id);
  for (const id of ["cursor-cloud"]) {
    assert.equal(adapterRelease(id)?.qualified, false, id);
    assert.ok(adapterRelease(id)?.reason, id);
  }
});

test("all assembled packages have release metadata and explicit build coverage", () => {
  const root = path.resolve(__dirname, "../../../..");
  const config = JSON.parse(fs.readFileSync(path.join(root, "release-please-config.json")));
  const versions = JSON.parse(fs.readFileSync(path.join(root, ".release-please-manifest.json")));
  const scripts = JSON.parse(fs.readFileSync(path.join(root, "typescript/package.json"))).scripts;
  const packages = [
    "traceai_agent_core", "traceai_agents", "traceai_codex_plugin", "traceai_opencode_v1_plugin",
    "traceai_antigravity_plugin", "traceai_copilot", "traceai_vscode_extension", "traceai_hermes_agent", "traceai_openclaw",
    "traceai_grok_plugin", "traceai_muse_plugin", "traceai_gemini_cli", "traceai_kiro_plugin",
    "traceai_augment_plugin", "traceai_devin_cascade", "traceai_claude_code", "traceai_cursor_plugin",
    "traceai_goose", "traceai_oh_my_pi_plugin", "traceai_opencode_plugin", "traceai_pi_plugin",
  ];
  for (const dir of packages) {
    const relative = `typescript/packages/${dir}`;
    const pkg = JSON.parse(fs.readFileSync(path.join(root, relative, "package.json")));
    assert.equal(config.packages[relative]?.["release-type"], "node", dir);
    assert.equal(config.packages[relative]?.["package-name"], pkg.name, dir);
    assert.equal(config.packages[relative]?.component, `npm-${pkg.name.replace(/^@/, "").replace("/", "-")}`, dir);
    assert.equal(versions[relative], "0.1.0", dir);
    assert.ok(scripts["build:consumers"].includes(`--filter ${pkg.name} `), dir);
    assert.ok(scripts["test:agents"].includes(`--filter ${pkg.name} `), dir);
  }
  for (const dir of ["traceai_muse_plugin", "traceai_gemini_cli", "traceai_kiro_plugin", "traceai_augment_plugin", "traceai_devin_cascade"]) {
    assert.equal(JSON.parse(fs.readFileSync(path.join(root, "typescript/packages", dir, "package.json"))).private, true, dir);
  }
});
