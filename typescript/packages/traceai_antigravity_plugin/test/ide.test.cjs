const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");
const {
  parseGuiObservation, guiPathCandidates, antigravityGuiAdapter,
  GUI_QUALIFICATION_REQUIRED, GUI_PROFILE_VERSION,
} = require("../dist/ide.js");

// Schema-shaped data only. None of these tests asserts actual IDE/App loading,
// transcript availability, model execution, GUI environment, or OTLP export.
const home = "/home/synthetic-user";
const conversationId = "fixture-ag-conv-0001";
const root = `${home}/.gemini/antigravity-ide/brain/${conversationId}`;
const common = {
  conversationId,
  modelName: "gemini-3.6-flash-medium",
  workspacePaths: ["/workspace/never-export"],
  transcriptPath: `${root}/.system_generated/logs/transcript.jsonl`,
  artifactDirectoryPath: root,
};
function options(privacy = {}, surface = "ide") {
  return {
    home, surface, identityKey: Buffer.alloc(32, 7),
    privacy: {
      enabled: true, captureContent: true, hideInputs: false, hideOutputs: false,
      hideErrorDetails: false,
      credentials: { apiKey: "unit-api-private", secretKey: "unit-secret-private" },
      ...privacy,
    },
  };
}
function parse(event, extra = {}, config = options()) {
  return parseGuiObservation(event, JSON.stringify({ ...common, ...extra }), config);
}
const tool = { name: "run_command", args: { CommandLine: "npm test" } };

test("GUI capability manifest is non-installable and does not equate invocations with model requests", async () => {
  const profile = JSON.parse(await fs.readFile(path.join(__dirname, "../profile/ide-v1.json")));
  assert.equal(profile.profileVersion, GUI_PROFILE_VERSION);
  assert.equal(profile.installable, false);
  assert.equal(profile.exportsEnabled, false);
  assert.equal(profile.requiresWorker, false);
  assert.deepEqual(profile.qualifiedHostVersions, []);
  assert.equal(profile.qualification, "schema-fixtures-only");
  assert.equal(profile.blocker, GUI_QUALIFICATION_REQUIRED);
  assert.equal(profile.capabilities.userTurnBoundaries, "unqualified");
  assert.equal(profile.capabilities.modelRequests, "unqualified");
  assert.ok(!profile.observedEvents.includes("PreToolUse"));
  assert.notEqual(profile.surfaces.ide.appDataRelativeToHome, profile.surfaces.app.appDataRelativeToHome);
});

test("documented GUI discovery candidates do not select the CLI app-data directory", () => {
  assert.equal(guiPathCandidates(home, "ide").appData, `${home}/.gemini/antigravity-ide`);
  assert.equal(guiPathCandidates(home, "app").appData, `${home}/.gemini/antigravity`);
  assert.equal(guiPathCandidates(home, "ide").hooks, `${home}/.gemini/config/hooks.json`);
  assert.throws(() => guiPathCandidates("relative", "ide"), /INVALID_GUI_HOME/);
  assert.throws(() => guiPathCandidates(home, "cli"), /INVALID_GUI_SURFACE/);
});

test("session identity is opaque, stable and surface-separated without any raw paths", () => {
  const result = parse("SessionStart");
  assert.match(result.conversationKey, /^[a-f0-9]{64}$/);
  assert.deepEqual(result, parse("SessionStart"));
  const app = parse("SessionStart", {
    transcriptPath: common.transcriptPath.replace("antigravity-ide", "antigravity"),
    artifactDirectoryPath: root.replace("antigravity-ide", "antigravity"),
  }, options({}, "app"));
  assert.equal(app.surface, "app");
  assert.notEqual(result.conversationKey, app.conversationKey);
  const serialized = JSON.stringify(result);
  for (const denied of [home, conversationId, "workspacePaths", "transcriptPath", "artifactDirectoryPath"])
    assert.ok(!serialized.includes(denied));
});

test("wrong/missing/ambiguous surface paths never fall through to the CLI", () => {
  for (const transcriptPath of [undefined, "", "/tmp/transcript.jsonl",
    common.transcriptPath.replace("antigravity-ide", "antigravity-cli"),
    common.transcriptPath.replace("antigravity-ide", "antigravity"),
    common.transcriptPath.replace("/brain/", "/other/../brain/"),
    common.transcriptPath.replace(conversationId, "other-session"),
    `file://${common.transcriptPath}`,
    common.transcriptPath.replace("transcript.jsonl", "transcript-CANARY.jsonl"),
    common.transcriptPath.replace(home, "/another-user"),
  ]) assert.equal(parse("SessionStart", { transcriptPath }), undefined);
  assert.equal(parse("SessionStart", { artifactDirectoryPath: root.replace("antigravity-ide", "antigravity-cli") }), undefined);
  assert.equal(parse("SessionStart", { conversationId: "../escape" }), undefined);
  assert.ok(parse("SessionStart", { artifactDirectoryPath: undefined }));
});

test("Windows-shaped paths are parsed lexically, not claimed as host qualification", () => {
  const windowsHome = "C:\\Users\\synthetic";
  const windowsRoot = `${windowsHome}\\.gemini\\antigravity-ide\\brain\\${conversationId}`;
  const result = parse("SessionStart", {
    transcriptPath: `${windowsRoot}\\.system_generated\\logs\\transcript.jsonl`,
    artifactDirectoryPath: windowsRoot,
  }, { ...options(), home: windowsHome });
  assert.equal(result.surface, "ide");
});

test("documented invocation counters are observations, not generated spans or usage", () => {
  for (const event of ["PreInvocation", "PostInvocation"]) {
    const result = parse(event, {
      invocationNum: 0, initialNumSteps: 1,
      lastUserInput: "UNPROVEN_PROMPT", modelOutput: "UNPROVEN_OUTPUT",
      usage: { inputTokens: 123 }, cost: 7, subagentId: "unproven-child",
    });
    assert.equal(result.invocationNum, 0);
    assert.equal(result.initialNumSteps, 1);
    for (const denied of ["turnId", "traceId", "spanId", "usage", "cost", "lastUserInput", "modelOutput", "subagentId"])
      assert.equal(result[denied], undefined);
    assert.ok(!JSON.stringify(result).includes("UNPROVEN"));
  }
});

test("invalid invocation counters and tool envelopes fail open", () => {
  for (const value of [-1, 0.5, "0", null, Number.MAX_SAFE_INTEGER + 1]) {
    assert.equal(parse("PreInvocation", { invocationNum: value, initialNumSteps: 1 }), undefined);
    assert.equal(parse("PostInvocation", { invocationNum: 0, initialNumSteps: value }), undefined);
    assert.equal(parse("PostToolUse", { toolCall: tool, stepIdx: value }), undefined);
  }
  for (const toolCall of [null, [], {}, { name: "unsafe name" }])
    assert.equal(parse("PostToolUse", { stepIdx: 1, toolCall }), undefined);
  assert.equal(parse("PostToolUse", { stepIdx: 1, toolCall: tool, error: {} }), undefined);
});

test("content capture uses the core privacy policy and omits undocumented tool output", () => {
  const event = { stepIdx: 2, toolCall: tool, error: "synthetic failure", result: "UNPROVEN_TOOL_OUTPUT" };
  const captured = parse("PostToolUse", event);
  assert.equal(captured.toolInput, JSON.stringify(tool.args));
  assert.equal(captured.error, "synthetic failure");
  assert.equal(captured.failed, true);
  assert.ok(!JSON.stringify(captured).includes("UNPROVEN_TOOL_OUTPUT"));
  const hidden = parse("PostToolUse", event, options({ captureContent: false }));
  assert.equal(hidden.toolInput, undefined);
  assert.equal(hidden.error, undefined);
  assert.equal(hidden.failed, true);
  assert.equal(parse("PostToolUse", event, options({ hideInputs: true })).toolInput, undefined);
  assert.equal(parse("PostToolUse", event, options({ hideErrorDetails: true })).error, undefined);
});

test("known keys are redacted even when crossing the content cap; fields remain bounded", () => {
  const event = {
    stepIdx: 2,
    toolCall: { name: "run_command", args: { CommandLine: "unit-api-private unit-secret-private" } },
    error: "x".repeat(19_998) + "unit-secret-private" + "z".repeat(22_000),
  };
  const result = parse("PostToolUse", event);
  assert.ok(result.error.length <= 20_000);
  assert.ok(!result.error.includes("unit"));
  assert.ok(!result.toolInput.includes("unit-api-private"));
  assert.ok(!result.toolInput.includes("unit-secret-private"));
  assert.match(result.toolInput, /REDACTED/);
  assert.equal(parse("SessionStart", { modelName: "unit-secret-private" }).model, undefined);
  assert.equal(parse("PostToolUse", { stepIdx: 2, toolCall: { name: "unit-api-private" } }), undefined);
});

test("Stop is incomplete when background work is active or terminal reason is unknown", () => {
  const stop = { executionNum: 1, fullyIdle: true, terminationReason: "model_stop" };
  assert.equal(parse("Stop", stop).outcome, "success");
  assert.equal(parse("Stop", { ...stop, fullyIdle: false }).outcome, "incomplete");
  assert.equal(parse("Stop", { ...stop, terminationReason: "UNKNOWN" }).outcome, "incomplete");
  assert.equal(parse("Stop", { ...stop, terminationReason: "USER_CANCELED" }).outcome, "interrupted");
  assert.equal(parse("Stop", { ...stop, terminationReason: "ERROR" }).outcome, "error");
  assert.equal(parse("Stop", { ...stop, error: "failure" }).outcome, "error");
  for (const fullyIdle of [undefined, "true", 1])
    assert.equal(parse("Stop", { ...stop, fullyIdle }), undefined);
  assert.equal(parse("Stop", { ...stop, finalModelOutput: "UNPROVEN_OUTPUT" }).output, undefined);
});

test("malformed, oversized, permission hooks and disabled collection produce no observation", () => {
  for (const raw of ["", "{", "null", "[]", '"string"', " ".repeat(256 * 1024 + 1),
    JSON.stringify({ ...common, extra: "界".repeat(100_000) })])
    assert.equal(parseGuiObservation("SessionStart", raw, options()), undefined);
  assert.equal(parse("PreToolUse", { stepIdx: 1, toolCall: tool }), undefined);
  assert.equal(parse("SessionStart", {}, options({ enabled: false })), undefined);
  assert.equal(parse("SessionStart", {}, { ...options(), identityKey: Buffer.alloc(1) }), undefined);
});

test("GUI adapter cannot register, activate, run, or claim support", async () => {
  const noContext = undefined;
  assert.equal(antigravityGuiAdapter.requiresWorker, false);
  assert.deepEqual(await antigravityGuiAdapter.detect(noContext), {
    supported: false, reason: GUI_QUALIFICATION_REQUIRED,
  });
  for (const operation of ["install", "inspect", "uninstall"]) {
    const result = await antigravityGuiAdapter[operation](noContext);
    assert.equal(result.registered, false);
    assert.equal(result.activated, false);
  }
  assert.equal(antigravityGuiAdapter.run, undefined);
});
