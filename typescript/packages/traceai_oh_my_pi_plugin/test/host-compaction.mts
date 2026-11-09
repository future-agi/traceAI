// E2B only: exercise OMP 18.17.0's actual extension runner and compaction scheduler.
import assert from "node:assert/strict";
import { mkdtemp, rm } from "node:fs/promises";
import { SessionMaintenance } from "/work/host/node_modules/@oh-my-pi/pi-coding-agent/src/session/session-maintenance.ts";
import { ExtensionRunner } from "/work/host/node_modules/@oh-my-pi/pi-coding-agent/src/extensibility/extensions/runner.ts";
import { Settings } from "/work/host/node_modules/@oh-my-pi/pi-coding-agent/src/config/settings.ts";
import { cfgCompactionAsyncEnabled } from "/work/host/node_modules/@oh-my-pi/pi-coding-agent/src/session/context-settings.ts";
import extension from "../dist/index.js";
const futureAgi =
  typeof extension === "function" ? extension : extension.default;
const home = await mkdtemp("/tmp/omp-compaction-");
Object.assign(process.env, {
  HOME: home,
  FI_AGENT_CONFIG_DIR: `${home}/fi`,
  PI_CODING_AGENT_DIR: `${home}/agent`,
  XDG_STATE_HOME: `${home}/state`,
  FI_API_KEY: "synthetic-api",
  FI_SECRET_KEY: "synthetic-secret",
  FI_OTLP_ENDPOINT: "http://127.0.0.1:1/tracer/v1/traces",
});
try {
  for (const mode of ["baseline", "enabled", "disabled", "interceptor"]) {
    process.env.FI_AGENT_ENABLED = mode === "disabled" ? "false" : "true";
    const handlers = new Map();
    if (mode === "enabled" || mode === "disabled") {
      futureAgi({
        on(name, handler) {
          handlers.set(name, [handler]);
        },
        events: { on() {} },
      });
      await handlers.get("session_start")[0](
        {},
        {
          cwd: home,
          agent: { kind: "main", id: "main" },
          sessionManager: { getSessionId: () => "compaction" },
        },
      );
    }
    if (mode === "interceptor")
      handlers.set("session_before_compact", [async () => {}]);
    const runner = new ExtensionRunner([{ handlers }], {}, home, {}, {});
    const settings = Settings.isolated({
      "compaction.enabled": true,
      "compaction.methodOrder": ["handoff"],
      "compaction.thresholdPercent": 80,
    });
    assert.equal(
      cfgCompactionAsyncEnabled.get(settings),
      true,
      "host async default",
    );
    let snapshots = 0;
    const maintenance = new SessionMaintenance({
      settings,
      extensionRunner: runner,
      model: () => ({
        id: "test",
        provider: "mock",
        api: "openai-completions",
        contextWindow: 100000,
      }),
      isDisposed: () => false,
      isGeneratingHandoff: () => false,
      sessionManager: {
        getSessionId: () => "compaction",
        getBranch: () => {
          snapshots++;
          return [];
        },
      },
    });
    maintenance.maybeStartSpeculativeCompaction(79000, 100000);
    assert.equal(
      snapshots > 0,
      mode !== "interceptor",
      `${mode}: background speculation reached snapshot`,
    );
    const deferred = maintenance.deferThresholdCompactionToSpeculation(
      80001,
      100000,
    );
    assert.equal(
      deferred,
      mode !== "interceptor",
      `${mode}: threshold deferral unchanged`,
    );
    maintenance.cancelSpeculation();
    if (handlers.has("session_shutdown"))
      await handlers.get("session_shutdown")[0](
        {},
        { agent: { kind: "main" } },
      );
    console.log(
      JSON.stringify({
        case: "HOST_ASYNC_COMPACTION",
        mode,
        hasVeto: runner.hasHandlers("session_before_compact"),
        snapshots,
        deferred,
      }),
    );
  }
} finally {
  await rm(home, { recursive: true, force: true });
}
