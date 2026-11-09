const { test } = require("node:test");
const assert = require("node:assert/strict");
const { fixture, child, fs, path, payload } = require("./helpers.cjs");
const { normalizeEvent } = require("../dist/events.js");
const {
  fragmentRedactionCases,
  fragmentRedactionRegression,
  failurePayloads,
  errorContextRegression,
  boundaryCases,
  boundaryRegression,
} = require("./round3-regressions.cjs");
const { spans } = require("./round2-golden.cjs");
const {
  installRuntime,
  register,
  commands,
} = require("../dist/registration.js");
const {
  terminalCases,
  terminalRegression,
  structuredRedactionRegression,
} = require("./review-regressions.cjs");
async function installed(t, capture = true) {
  const f = await fixture(t, capture);
  const runtime = await installRuntime(f.context);
  await register(f.context, runtime);
  const hooks = commands(runtime, f.config.paths.configDir);
  return {
    home: f.home,
    readSpans: () => spans(f.requests),
    readDiagnostics: async () =>
      JSON.parse(
        await fs.readFile(
          path.join(f.config.paths.stateDir, "cursor-diagnostics.json"),
          "utf8",
        ),
      ),
    send: async (row, extraEnv = {}) => {
      const result = await child(
        hooks[row.hook_event_name].command,
        { ...row, workspace_roots: [f.home] },
        { HOME: f.home, PATH: "/missing", NODE_OPTIONS: "", ...extraEnv },
      );
      assert.equal(result.code, 0);
      assert.equal(result.stdout, "");
      assert.equal(result.stderr, "");
      assert.ok(result.ms < 8000);
    },
  };
}
for (const name of terminalCases)
  test(`installed hooks: ${name}, including duplicate terminals`, async (t) => {
    await terminalRegression(await installed(t), name);
  });
test("installed hooks: no-prompt usage survives content opt-out", async (t) => {
  await terminalRegression(
    await installed(t, false),
    "response-stop-no-prompt",
    false,
  );
});
test("installed hooks: structured credentials never reach generation output or subsequent context", async (t) => {
  await structuredRedactionRegression(await installed(t));
});
for (const variant of fragmentRedactionCases)
  test(`installed hooks: sanitize ${variant} before joining generation content`, async (t) => {
    await fragmentRedactionRegression(await installed(t), variant);
  });
for (const hide of [true, false])
  test(`installed hooks: payload-only failures obey hideErrorDetails=${hide} in generation context`, async (t) => {
    await errorContextRegression(await installed(t), hide);
  });
for (const scenario of boundaryCases)
  test(`installed hooks: ${scenario.count} detail events retain terminal outcome and usage once`, async (t) => {
    await boundaryRegression(await installed(t), scenario);
  });
test("hook failure classifier covers the same result payloads as transcript parsing", async (t) => {
  const f = await fixture(t);
  for (const flags of failurePayloads)
    for (const encode of [
      (v) => v,
      JSON.stringify,
      (v) => [{ type: "text", text: JSON.stringify(v) }],
    ]) {
      const output = encode({
        ...flags,
        stderr: "SYNTHETIC_PRIVATE_HOOK_ERROR",
      });
      const row = payload("postToolUse", {
        tool_name: "Shell",
        tool_use_id: "call",
        tool_output: output,
      });
      const hidden = normalizeEvent(row, "postToolUse", {
        ...f.config,
        hideErrorDetails: true,
      });
      assert.equal(hidden.outcome, "error");
      assert.equal(hidden.output, undefined);
      const visible = normalizeEvent(row, "postToolUse", {
        ...f.config,
        hideErrorDetails: false,
      });
      assert.equal(visible.outcome, "error");
      assert.match(visible.output, /SYNTHETIC_PRIVATE_HOOK_ERROR/);
    }
});
