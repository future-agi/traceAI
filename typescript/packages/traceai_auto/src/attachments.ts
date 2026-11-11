import { createNodeAttachmentManager, type NodeAttachmentManagerOptions } from "@traceai/fi-core/attachments/node";
import type { AttachmentManager, AttachmentStatus } from "@traceai/fi-core/attachments";
import type { Config, Options } from "./config";
import { WIRE_MEDIA_ROUTES } from "./wire/attachments";

const restrictive = (traceConfig: Record<string, unknown> | undefined) => Object.fromEntries(Object.entries(traceConfig ?? {}).filter(([key, value]) =>
  (key.startsWith("hide") || key === "piiRedaction") && value === true));

export function attachmentOptions(config: Config, options: Options): NodeAttachmentManagerOptions {
  const explicit = options.attachments ?? {};
  return { ...explicit, environment: () => process.env, projectName: config.projectName, projectType: config.projectType,
    // Only resolved FI credentials, never model/provider credentials. A neutral
    // exporter does not imply an attachment destination or credential authority.
    headers: explicit.headers ?? (config.apiKey && config.secretKey ? { "x-api-key": config.apiKey, "x-secret-key": config.secretKey } : {}),
    // A restriction from either side wins; an attachment-level `false` cannot relax a global hide flag.
    traceConfig: { ...explicit.traceConfig, ...config.traceConfig, ...restrictive(explicit.traceConfig as Record<string, unknown>) } as NodeAttachmentManagerOptions["traceConfig"] };
}
interface AttachmentProvider { attachments?: AttachmentManager; getDelegate?(): AttachmentProvider }
/**
 * One manager per process: the application's (explicit option, then the provider's), otherwise the one auto's own
 * provider created. Adapters and wire observation all receive this same manager.
 */
export function selectAttachmentManager(provider: AttachmentProvider, config: Config, options: Options, ownsProvider: boolean): { manager: AttachmentManager; owned: boolean } {
  let existing: AttachmentManager | undefined;
  try { existing = options.attachmentManager ?? provider?.attachments ?? provider?.getDelegate?.()?.attachments; } catch { existing = undefined; }
  if (existing) {
    // Borrowing never turns capture on and never loosens application policy: only auto's hide/PII flags are added.
    // The env override (FI_ATTACHMENTS_ENABLED=false) is already applied by the manager at every admission, so a
    // borrowed manager is not permanently disabled here (updatePolicy is tighten-only).
    try { existing.updatePolicy({ traceConfig: restrictive(attachmentOptions(config, options).traceConfig as Record<string, unknown>) as NodeAttachmentManagerOptions["traceConfig"] }); } catch { /* fail closed below */ }
    return { manager: existing, owned: ownsProvider && !options.attachmentManager };
  }
  return { manager: createNodeAttachmentManager(attachmentOptions(config, options)), owned: true };
}

/** What auto can capture, independent of whether the manager is on. Nothing here is a playback claim. */
export const AUTO_ATTACHMENT_SOURCES = Object.freeze([
  "wire FormData Blob (transcription audio, image edit image/mask) under a native model span with an explicit capture policy",
  "wire JSON input images/files/computer screenshots (OpenAI chat, responses) via the @traceai/openai adapter",
  "wire JSON output images (OpenAI images b64_json, responses image_generation_call) consumed by the application via json()",
  "wire speech audio materialized by the application via arrayBuffer()/blob()",
  "wire speech audio read incrementally by the application (reader or async iterator) under a native model span with an explicit capture policy",
]);
export const AUTO_ATTACHMENT_UNSUPPORTED = Object.freeze([
  "wire-only FormData (the wire span does not exist before the upload; no source is retained until then)",
  "wire-only incremental speech reads (correlation_unavailable until the core supports deferred-target preflight)",
  "native model spans without an explicit integration capture policy",
  "raw multipart bodies, streamed request bodies, compressed responses consumed before the span is emitted",
  "RemoteUrl / ProviderFile retrieval",
  "piped or teed response bodies",
  "WebSocket / RTC audio",
  "new recordings",
  "product playback qualification",
]);
export interface AttachmentReport {
  state: AttachmentStatus["state"] | "not_initialized";
  status?: Omit<AttachmentStatus, "state">;
  transport: "shared_manager";
  routes: string[];
  sources: readonly string[];
  unsupported: readonly string[];
}
export function attachmentReport(manager?: AttachmentManager): AttachmentReport {
  let status: AttachmentStatus | undefined;
  try { status = manager?.status(); } catch { status = undefined; }
  const { state, ...counts } = status ?? { state: "not_initialized" as const };
  return {
    state, ...(status ? { status: counts as Omit<AttachmentStatus, "state"> } : {}),
    transport: "shared_manager",
    routes: WIRE_MEDIA_ROUTES.map(route => `POST https://${route.host}${route.path} (${route.operation})`),
    sources: AUTO_ATTACHMENT_SOURCES,
    unsupported: AUTO_ATTACHMENT_UNSUPPORTED,
  };
}
