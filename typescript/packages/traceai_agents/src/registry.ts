import type { CoreAdapter } from "@traceai/agent-core";

export interface AdapterRelease {
  id: string;
  packageName: string;
  version: string;
  mechanism: string;
  hostRange?: string;
  qualified: boolean;
  reason: string;
}
/** Proposed names only. Adapter owners must supply a tested host range before enabling a release. */
export const adapterRegistry: readonly AdapterRelease[] = [
  {
    id: "pi",
    packageName: "@traceai/pi-plugin",
    version: "0.1.0",
    mechanism: "pi install",
    qualified: false,
    reason: "HOST_AND_REGISTRY_QUALIFICATION_REQUIRED",
  },
  {
    id: "opencode",
    packageName: "@traceai/opencode-plugin",
    version: "0.1.0",
    mechanism: "opencode plugin add",
    qualified: false,
    reason: "HOST_AND_REGISTRY_QUALIFICATION_REQUIRED",
  },
  {
    id: "opencode-v1",
    packageName: "@traceai/opencode-v1-plugin",
    version: "0.1.0",
    mechanism: "configuration",
    qualified: false,
    reason: "HOST_AND_REGISTRY_QUALIFICATION_REQUIRED",
  },
  {
    id: "oh-my-pi",
    packageName: "@traceai/oh-my-pi-plugin",
    version: "0.1.0",
    mechanism: "omp install",
    qualified: false,
    reason: "HOST_AND_REGISTRY_QUALIFICATION_REQUIRED",
  },
  {
    id: "cursor", packageName: "@traceai/cursor-plugin", version: "0.1.0",
    mechanism: "persistent user hooks; no local-plugin policy changes",
    qualified: false, hostRange: "3.x hook schema (3.24.18, 3.26.13 observed)",
    reason: "NPM_AVAILABILITY_REQUIRED",
  },
  {
    id: "cursor-cli", packageName: "@traceai/cursor-plugin", version: "0.1.0",
    mechanism: "shared user hooks; exclusive print stream pending qualification",
    qualified: false, reason: "CURSOR_CLI_LIVE_CAPTURE_REQUIRED",
  },
  {
    id: "cursor-cloud", packageName: "@traceai/cursor-plugin", version: "0.1.0",
    mechanism: "environment install and repository command hooks; injected credentials only",
    qualified: false, reason: "AUTHORIZED_CLOUD_RUN_AND_NPM_AVAILABILITY_REQUIRED",
  },
  ...Object.entries({
    claude: "claude-code",
    "claude-code": "claude-code",
    codex: "codex-plugin",
    grok: "grok-plugin",
    antigravity: "antigravity-plugin",
    gemini: "gemini-cli",
    copilot: "copilot",
    muse: "muse-plugin",
    "kiro-cli": "kiro-plugin",
    "kiro-ide": "kiro-plugin",
    augment: "augment-plugin",
    openclaw: "openclaw",
    hermes: "hermes-agent",
    goose: "goose",
    "devin-local": "devin-plugin",
  }).map(([id, name]) => ({
    id,
    packageName: `@traceai/${name}`,
    version: "0.1.0",
    mechanism: "native",
    qualified: false,
    reason: "ADAPTER_QUALIFICATION_REQUIRED",
  })),
];
export function adapterRelease(id: string): AdapterRelease | undefined {
  return adapterRegistry.find((item) => item.id === id);
}
/** Adapter implementation owners register their tested, statically imported lifecycle here.
 * Never load a project-supplied executable path or treat a manifest alone as qualification.
 */
export function loadAdapter(id: string): CoreAdapter | undefined {
  if (!["cursor", "cursor-cli", "cursor-cloud"].includes(id)) return undefined;
  // Held distributions remain optional until real-host and npm qualification.
  try {
    if (id === "cursor-cli")
      return (require("@traceai/cursor-plugin/cli") as { cursorCliAdapter: CoreAdapter }).cursorCliAdapter;
    if (id === "cursor-cloud")
      return (require("@traceai/cursor-plugin/cloud") as { cursorCloudAdapter: CoreAdapter }).cursorCloudAdapter;
    return (require("@traceai/cursor-plugin") as { cursorAdapter: CoreAdapter }).cursorAdapter;
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== "MODULE_NOT_FOUND") throw error;
    return undefined;
  }
}
