export const CURSOR_CLI_VERSION = "0.1.0"; // x-release-please-version
/** Actual binary installed and inspected in isolated Linux E2B; not a model/hook qualification. */
export const INSPECTED_CLI_VERSION = "2026.11.05-86047df";
/** Existing preview schema fixtures only. Never infer IDE parity from this version. */
export const CLI_HOOK_FIXTURE_VERSION = "2026.10.17";
export const CLI_RELEASE_HOLD = "CURSOR_CLI_LIVE_CAPTURE_REQUIRED";
export const CLI_IMPORT_HOLD = "CURSOR_HOOK_STREAM_ALIAS_UNPROVEN";
export const cursorCliCapabilities = Object.freeze({
  id: "cursor-cli",
  packageName: "@traceai/cursor-plugin",
  version: CURSOR_CLI_VERSION,
  qualified: false,
  releaseHold: CLI_RELEASE_HOLD,
  inspectedBinaryVersion: INSPECTED_CLI_VERSION,
  ownership: "shared-cursor-hooks",
  interactive: Object.freeze({
    mechanism: "user-hooks", fixtureVersion: CLI_HOOK_FIXTURE_VERSION,
    liveQualified: false, promptBoundary: "requires-beforeSubmitPrompt",
    modelRequests: false, transcriptImport: false,
  }),
  print: Object.freeze({
    mechanism: "stream-json", liveQualified: false, fidelity: "aggregate-run",
    prompts: false, modelRequests: false, usage: false, cost: false,
    requiresExclusiveCapture: true, importHold: CLI_IMPORT_HOLD,
  }),
});
