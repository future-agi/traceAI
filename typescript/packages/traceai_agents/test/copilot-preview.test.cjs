const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const {
  copilotCliAdapter,
  copilotVSCodeAdapter,
} = require("../dist/src/profiles/copilot.js");
const { agentEnvironment } = require("../dist/src/cli/run.js");

test("preview Copilot CLI pins its binary and activates only native traces", async (t) => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "fi-preview-cli-"));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const bin = path.join(dir, "copilot");
  fs.writeFileSync(bin, '#!/bin/sh\necho "GitHub Copilot CLI 1.0.98"\n', {
    mode: 0o700,
  });
  const prior = process.env.PATH;
  process.env.PATH = dir;
  try {
    const context = { surface: "cli", routeRef: "copilot.cli", preview: true };
    const launch = await copilotCliAdapter.launch(context, { tracing: true });
    assert.equal(launch.executable, fs.realpathSync(bin));
    assert.equal(launch.native, true);
    assert.equal(launch.env.COPILOT_OTEL_ENABLED, "true");
    const disabled = await copilotCliAdapter.launch(context, {
      tracing: false,
    });
    assert.equal(disabled.native, false);
    assert.deepEqual(disabled.env, {});
    const overlay = agentEnvironment(
      {},
      {
        ...launch.env,
        OTEL_EXPORTER_OTLP_TRACES_ENDPOINT: "http://127.0.0.1:1234/v1/traces",
        OTEL_EXPORTER_OTLP_METRICS_ENDPOINT: "http://127.0.0.1:1234/v1/metrics",
      },
      true,
    );
    assert.equal(
      new URL(overlay.OTEL_EXPORTER_OTLP_METRICS_ENDPOINT).hostname,
      "127.0.0.1",
    );
    assert.equal(copilotVSCodeAdapter.capability.schemas.length, 0);
    await assert.rejects(
      copilotVSCodeAdapter.preflight("enable", context),
      /COPILOT_VSCODE_HARNESS_UNQUALIFIED/,
    );
    await assert.rejects(
      copilotCliAdapter.launch(
        { ...context, preview: false },
        { tracing: true },
      ),
      /COPILOT_PREVIEW_FLAG_REQUIRED/,
    );
    process.env.OTEL_EXPORTER_OTLP_HEADERS = "unowned";
    await assert.rejects(
      copilotCliAdapter.launch(context, { tracing: true }),
      /NATIVE_DESTINATION_CONFLICT/,
    );
    delete process.env.OTEL_EXPORTER_OTLP_HEADERS;
    fs.writeFileSync(bin, '#!/bin/sh\necho "GitHub Copilot CLI 1.0.99"\n', {
      mode: 0o700,
    });
    await assert.rejects(
      copilotCliAdapter.launch(context, { tracing: true }),
      /COPILOT_VERSION_UNQUALIFIED/,
    );
  } finally {
    process.env.PATH = prior;
    delete process.env.OTEL_EXPORTER_OTLP_HEADERS;
  }
});

test("explicit preview gate is required for CLI and never bypasses VS Code hold", async (t) => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "fi-preview-gate-"));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const previous = [
    process.env.FI_AGENT_CONFIG_HOME,
    process.env.FI_AGENT_STATE_HOME,
  ];
  process.env.FI_AGENT_CONFIG_HOME = path.join(fs.realpathSync(dir), "config");
  process.env.FI_AGENT_STATE_HOME = path.join(fs.realpathSync(dir), "state");
  const { main } = require("../dist/src/cli/main.js");
  const settings = {
    version: "0.1.0",
    entry: path.join(dir, "unused"),
    storageEntry: path.join(dir, "unused"),
  };
  try {
    assert.equal(
      await main(["enable", "copilot", "--surface", "cli"], settings),
      5,
    );
    assert.equal(
      await main(
        ["enable", "copilot", "--surface", "cli", "--preview"],
        settings,
      ),
      3,
    ); // Auth not configured; preview passed the static hold.
    assert.equal(
      await main(
        ["enable", "copilot", "--surface", "vscode", "--preview"],
        settings,
      ),
      5,
    );
    assert.equal(fs.existsSync(path.join(dir, "config", "config.json")), false);
  } finally {
    if (previous[0] === undefined) delete process.env.FI_AGENT_CONFIG_HOME;
    else process.env.FI_AGENT_CONFIG_HOME = previous[0];
    if (previous[1] === undefined) delete process.env.FI_AGENT_STATE_HOME;
    else process.env.FI_AGENT_STATE_HOME = previous[1];
  }
});

test("generic adapter cannot override signals, and preview cannot route metrics off-host", () => {
  assert.throws(
    () => agentEnvironment({}, { COPILOT_OTEL_ENABLED: "true" }),
    /UNSAFE_LAUNCH_OVERLAY/,
  );
  assert.throws(
    () =>
      agentEnvironment(
        {},
        {
          COPILOT_OTEL_ENABLED: "true",
          OTEL_EXPORTER_OTLP_TRACES_ENDPOINT: "http://127.0.0.1:1234/v1/traces",
          OTEL_EXPORTER_OTLP_METRICS_ENDPOINT:
            "https://outside.invalid/v1/metrics",
        },
        true,
      ),
    /UNSAFE_LAUNCH_OVERLAY/,
  );
});
