import type { ResolvedConfig } from "./config.js";

export type AgentAttachmentSource = "typed-image-blocks" | "source_unavailable";

/**
 * Per-host media source readiness, keyed by adapter id. Separate from span support,
 * server authorization and product playback. A plugin, hook or native OTel feed is
 * not screenshot access: only hosts that hand the integration inline typed image
 * bytes are capturable with the current Bytes/Blob attachment core.
 */
export const AGENT_ATTACHMENT_SOURCES: Readonly<Record<string, AgentAttachmentSource>> = Object.freeze({
  pi: "typed-image-blocks",
  "oh-my-pi": "typed-image-blocks",
  claude: "typed-image-blocks",
  codex: "source_unavailable",
  cursor: "source_unavailable",
  "cursor-cli": "source_unavailable",
  "cursor-cloud": "source_unavailable",
  opencode: "source_unavailable",
  "opencode-v1": "source_unavailable",
  openclaw: "source_unavailable",
  hermes: "source_unavailable",
  goose: "source_unavailable",
  gemini: "source_unavailable",
  copilot: "source_unavailable",
  "copilot-vscode": "source_unavailable",
  "kiro-cli": "source_unavailable",
  "kiro-ide": "source_unavailable",
  antigravity: "source_unavailable",
  "antigravity-ide": "source_unavailable",
  augment: "source_unavailable",
  "augment-ide": "source_unavailable",
  "devin-cascade": "source_unavailable",
  grok: "source_unavailable",
  muse: "source_unavailable",
});

/** Local inspection does not mint a capability or confuse OTLP acceptance with attachment scope. */
export function agentAttachmentStatus(config: ResolvedConfig, env: NodeJS.ProcessEnv = process.env) {
  const source: AgentAttachmentSource = Object.prototype.hasOwnProperty.call(AGENT_ATTACHMENT_SOURCES, config.agent)
    ? AGENT_ATTACHMENT_SOURCES[config.agent]
    : "source_unavailable";
  const requested = /^(true|1|yes)$/i.test(env.FI_ATTACHMENTS_ENABLED?.trim() ?? "");
  return {
    state: !requested ? "disabled" :
      !config.enabled || !config.captureContent || (config.hideInputs && config.hideOutputs) ? "policy_denied" :
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
