import type { CoreAdapter } from "@traceai/agent-core";
import { codexAdapter, HOST_VERSION } from "@traceai/codex-plugin";
import { opencodeV1Adapter } from "./opencode-v1.js";
import { antigravityAdapter } from "@traceai/antigravity-plugin";
import { copilotAdapter, HOST_VERSION as COPILOT_HOST_VERSION } from "@traceai/copilot";
import { vscodeAdapter, EDITOR_HOLD } from "@traceai/copilot/vscode";

export interface AdapterRelease {
  id: string;
  packageName: string;
  version: string;
  mechanism: string;
  hostRange?: string;
  qualified: boolean;
  reason: string;
  /** Stop before credential collection or writes for unqualified surfaces. */
  setupBlocker?: string;
}
/** Proposed names only. Adapter owners must supply a tested host range before enabling a release. */
export const adapterRegistry: readonly AdapterRelease[] = [
  {
    id: "codex",
    packageName: "@traceai/codex-plugin",
    version: "0.1.0",
    mechanism: "codex plugin add",
    hostRange: HOST_VERSION,
    qualified: true,
    reason: "REGISTRY_PUBLICATION_REQUIRED",
  },
  {
    id: "antigravity",
    packageName: "@traceai/antigravity-plugin",
    version: "0.1.0",
    mechanism: "agy plugin install",
    hostRange: "1.x (Linux CLI; tested 1.5.2)",
    qualified: false,
    reason: "PACKAGE_PUBLICATION_AND_CLOUD_READBACK_REQUIRED",
  },
  {
    id: "antigravity-ide",
    packageName: "@traceai/antigravity-plugin",
    version: "0.1.0",
    mechanism: "native-gui-hooks",
    qualified: false,
    reason: "ANTIGRAVITY_GUI_QUALIFICATION_REQUIRED",
  },
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
    hostRange: ">=1.19.0 <2.0.0",
    qualified: false,
    reason: "REGISTRY_PUBLICATION_REQUIRED",
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
    id: "copilot",
    packageName: "@traceai/copilot",
    version: "0.1.0",
    mechanism: "process-scoped native telemetry launch",
    hostRange: COPILOT_HOST_VERSION,
    qualified: true,
    reason: "LAUNCH_WITH_FI_AGENT_RUN",
  },
  {
    id: "copilot-vscode",
    packageName: "@traceai/copilot",
    version: "0.1.0",
    mechanism: "native VS Code settings (harness-specific)",
    qualified: false,
    reason: EDITOR_HOLD,
    setupBlocker: EDITOR_HOLD,
  },
  ...Object.entries({
    cursor: "cursor-plugin",
    "cursor-cli": "cursor-plugin",
    claude: "claude-code",
    "claude-code": "claude-code",
    grok: "grok-plugin",
    gemini: "gemini-cli",
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
  if (id === "codex") return codexAdapter;
  if (id === "opencode-v1") return opencodeV1Adapter;
  if (id === "antigravity") return antigravityAdapter;
  if (id === "copilot") return copilotAdapter;
  if (id === "copilot-vscode") return vscodeAdapter;
  return undefined;
}
