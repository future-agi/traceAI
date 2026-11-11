import type { ResolvedConfig } from "./config.js";

/** Source readiness is separate from span support, server authorization and product playback. */
export const AGENT_ATTACHMENT_SOURCES = Object.freeze({
  pi: "typed-image-blocks",
  "oh-my-pi": "typed-image-blocks",
  "pi-coding-agent": "source_unavailable",
  "claude-code": "source_unavailable",
  codex: "source_unavailable",
  "codex-sdk": "source_unavailable",
  cursor: "source_unavailable",
  "cursor-cli": "source_unavailable",
  "cursor-cloud": "source_unavailable",
  "cursor-sdk": "source_unavailable",
  opencode: "source_unavailable",
  "opencode-v1": "source_unavailable",
  openclaw: "source_unavailable",
  hermes: "source_unavailable",
  goose: "source_unavailable",
  gemini: "source_unavailable",
  copilot: "source_unavailable",
  kiro: "source_unavailable",
  antigravity: "source_unavailable",
  augment: "source_unavailable",
  "augment-ide": "source_unavailable",
  "devin-cascade": "source_unavailable",
  grok: "source_unavailable",
  muse: "source_unavailable",
  "copilot-vscode": "source_unavailable",
} as const);

/** Local inspection does not mint a capability or confuse OTLP acceptance with attachment scope. */
export function agentAttachmentStatus(config: ResolvedConfig, env: NodeJS.ProcessEnv = process.env) {
  const source = AGENT_ATTACHMENT_SOURCES[config.agent as keyof typeof AGENT_ATTACHMENT_SOURCES] ?? "source_unavailable";
  const requested = /^(true|1|yes)$/i.test(env.FI_ATTACHMENTS_ENABLED?.trim() ?? "");
  return {
    state: !requested ? "disabled" :
      !config.enabled || !config.captureContent ? "policy_denied" :
      source === "source_unavailable" ? "source_unavailable" :
      !env.FI_ATTACHMENTS_ENDPOINT ? "endpoint_unconfigured" : "scope_unverified",
    source,
    credentialScope: "unverified-runtime-bootstrap-required",
    productPlayback: "unverified",
    recording: "not-created",
    maxImageBytes: 256 * 1024,
    localFile: "source_unavailable",
    remoteUrl: "source_unavailable",
  } as const;
}
