export {
  claudeAdapter,
  detectClaude,
  interpreterPath,
  installHooks,
  inspectHooks,
  uninstallHooks,
  nativeConflict,
} from "./install.js";
export {
  VERSION,
  TESTED_HOST,
  supportedHost,
  EVENTS,
  readTranscript,
  exportTranscriptTurn,
  safeTool,
} from "./transcript.js";
export type { SessionState, TurnExport } from "./transcript.js";
export const CAPABILITIES = Object.freeze({
  mode: "exclusive-transcript-hooks",
  generations: "source-message-and-request-anchored",
  modelInput: "bounded-visible-transcript-history-not-system-prompt",
  timing: "reconstructed-generations-and-observed-tool-hooks",
  cost: "absent",
  nativeTelemetry: false,
  requiresWorker: false,
  qualification: "LINUX_CLAUDE_2_1_318",
});
