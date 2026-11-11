import { createNodeAttachmentManager, type NodeAttachmentManagerOptions } from "@traceai/fi-core/attachments/node";
import type { AttachmentManager } from "@traceai/fi-core/attachments";
import type { Config, Options } from "./config";

export function attachmentOptions(config: Config, options: Options): NodeAttachmentManagerOptions {
  const explicit = options.attachments ?? {};
  const restrictive = Object.fromEntries(Object.entries(explicit.traceConfig ?? {}).filter(([key, value]) =>
    (key.startsWith("hide") || key === "piiRedaction") && value === true));
  return { ...explicit, environment: () => process.env, projectName: config.projectName, projectType: config.projectType,
    // Only resolved FI credentials, never model/provider credentials. A neutral
    // exporter does not imply an attachment destination or credential authority.
    headers: explicit.headers ?? (config.apiKey && config.secretKey ? { "x-api-key": config.apiKey, "x-secret-key": config.secretKey } : {}),
    traceConfig: { ...explicit.traceConfig, ...config.traceConfig, ...restrictive } };
}
interface AttachmentProvider { attachments?: AttachmentManager; getDelegate?(): AttachmentProvider }
export function selectAttachmentManager(provider: AttachmentProvider, config: Config, options: Options, ownsProvider: boolean): { manager: AttachmentManager; owned: boolean } {
  const existing = options.attachmentManager ?? provider?.attachments ?? provider?.getDelegate?.()?.attachments;
  if (existing) {
    // Borrowing must not loosen application policy or turn capture on implicitly.
    const effective = attachmentOptions(config, options);
    const restrictions = Object.fromEntries(Object.entries(effective.traceConfig ?? {}).filter(([key, value]) =>
      (key.startsWith("hide") || key === "piiRedaction") && value === true));
    const environmentDenies = process.env.FI_ATTACHMENTS_ENABLED !== undefined && !/^(true|1|yes)$/i.test(process.env.FI_ATTACHMENTS_ENABLED.trim());
    existing.updatePolicy({ ...(options.attachments?.enabled === false || environmentDenies ? { enabled: false } : {}), traceConfig: restrictions });
    return { manager: existing, owned: ownsProvider && !options.attachmentManager };
  }
  return { manager: createNodeAttachmentManager(attachmentOptions(config, options)), owned: true };
}
export interface AttachmentReport {
  state: ReturnType<AttachmentManager["status"]>["state"] | "not_initialized";
  transport: "shared_manager"; integration: "component_only"; sources: string[]; unsupported: string[];
}
export function attachmentReport(manager?: AttachmentManager): AttachmentReport {
  return {
    state: manager?.status().state ?? "not_initialized",
    transport: "shared_manager",
    integration: "component_only",
    sources: ["explicit-policy native-span FormData Blob", "consumed response arrayBuffer/blob", "consumed PNG JSON output <=256KiB"],
    unsupported: ["unqualified native span policy", "JSON inputs", "wire-only FormData", "incremental/encoded audio", "RemoteUrl/ProviderFile", "WebSocket/RTC", "new recordings", "product playback qualification"],
  };
}
