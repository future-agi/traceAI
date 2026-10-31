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
      assert.deepEqual(
        JSON.parse(
          fs.readFileSync(path.join(root, "profiles/capabilities.json")),
        ).enabled_by_default,
        [],
      );
      assert.equal(
        JSON.parse(fs.readFileSync(path.join(root, "profiles/copilot.json")))
          .release_state,
        "qualification-only",
      );
      for (const surface of ["cli", "vscode"]) {
        const doctor = spawnSync(
          process.execPath,
          [binary, "doctor", "copilot", "--surface", surface],
          { env, encoding: "utf8", timeout: 10000 },
        );
        assert.equal(doctor.status, 5);
        assert.equal(JSON.parse(doctor.stdout).state, "qualification-only");
        assert.equal(fs.existsSync(path.join(home, "config")), false);
      }
      // Fixture data comes from the repo; ALL implementation code and constants
      // resolve from the clean install, in both module formats.
      const fixture = require("../../integrations/coding-agents/fixtures/source/copilot-cli-1.0.98.json");
      const esm = await import(
        require("node:url").pathToFileURL(path.join(root, "dist/esm/index.js"))
          .href
      );
      for (const api of [load(name), esm]) {
        const facts = fixture.resourceSpans.flatMap((r) =>
          r.scopeSpans.flatMap((s) =>
            s.spans.map((span) =>
              api.normalizeCopilotCli(
                { span, resource: r.resource, scope: s.scope },
                {
                  identityKey: Buffer.alloc(32, 7),
                  policyRevision: "1",
                  sourceSchema: api.COPILOT_SCHEMA,
                },
              ),
            ),
          ),
        );
        assert.equal(
          facts.reduce((n, f) => n + (f.usage.input ?? 0), 0),
          42925,
        );
        assert.equal(
          facts.reduce((n, f) => n + (f.usage.output ?? 0), 0),
          146,
        );
        const sourceRoot = facts.find((f) => !f.parentSpanId);
        const wire = api.spanObject(sourceRoot);
        assert.equal(
          wire.attributes.find(
            (a) => a.key === "fi.source.summary.input_tokens",
          ).value.doubleValue,
          42925,
        );
        assert.equal(
          wire.attributes.find((a) => a.key === "fi.usage.contribution").value
            .stringValue,
          "none",
        );
      }
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
