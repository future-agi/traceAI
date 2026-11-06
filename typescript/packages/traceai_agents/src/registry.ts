import type { CoreAdapter } from "@traceai/agent-core";
import { grokAdapter, HOST_RANGE } from "@traceai/grok-plugin";

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
    id: "grok",
    packageName: "@traceai/grok-plugin",
    version: "0.1.0",
    mechanism: "grok plugin install",
    hostRange: HOST_RANGE,
    qualified: false,
    reason: "REGISTRY_PUBLICATION_REQUIRED",
  },
  ...Object.entries({
    cursor: "cursor-plugin",
    "cursor-cli": "cursor-plugin",
    claude: "claude-code",
    "claude-code": "claude-code",
    codex: "codex-plugin",
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
  return id === "grok" ? grokAdapter : undefined;
}
