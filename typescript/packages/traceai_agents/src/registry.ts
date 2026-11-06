import type { CoreAdapter } from "@traceai/agent-core";
import { museAdapter } from "@traceai/muse-plugin";
import { geminiAdapter, TESTED_HOST } from "@traceai/gemini-cli";
import { kiroAdapter } from "@traceai/kiro-plugin";
import { kiroIdeAdapter, IDE_HOLD as KIRO_IDE_HOLD } from "@traceai/kiro-plugin/ide";
import { augmentAdapter, augmentIdeAdapter, IDE_HOLD as AUGMENT_IDE_HOLD } from "@traceai/augment-plugin";
import { cascadeAdapter } from "@traceai/devin-cascade";

export interface AdapterRelease {
  id: string;
  packageName: string;
  version: string;
  mechanism: string;
  hostRange?: string;
  qualified: boolean;
  reason: string;
  /** Held surfaces reject activation before credentials, network or host changes. */
  setupHold?: string;
}
/** Proposed names only. Adapter owners must supply a tested host range before enabling a release. */
export const adapterRegistry: readonly AdapterRelease[] = [
  {
    id: "muse", packageName: "@traceai/muse-plugin", version: "0.1.0",
    mechanism: "native argv hooks + explicit approval", hostRange: "1.4.14-R7309.1 (Linux)",
    qualified: false, reason: "MUSE_TRANSCRIPT_AND_CHILD_ANCESTRY_UNQUALIFIED",
  },
  {
    id: "gemini", packageName: "@traceai/gemini-cli", version: "0.1.0",
    mechanism: "exclusive native hooks", hostRange: TESTED_HOST,
    qualified: false, reason: "GEMINI_MODEL_TRANSPORT_QUALIFICATION_REQUIRED",
  },
  {
    id: "kiro-cli", packageName: "@traceai/kiro-plugin", version: "0.1.0",
    mechanism: "V3 standalone hooks (user scope, packaged executable)", hostRange: "3.0.0",
    qualified: false, reason: "KIRO_AUTHENTICATED_V3_TURN_AND_REGISTRY_QUALIFICATION_REQUIRED",
  },
  {
    id: "kiro-ide", packageName: "@traceai/kiro-plugin", version: "0.1.0",
    mechanism: "workspace v1 hooks (IDE prototype, no copied emitter)",
    qualified: false, reason: KIRO_IDE_HOLD, setupHold: KIRO_IDE_HOLD,
  },
  {
    id: "augment", packageName: "@traceai/augment-plugin", version: "0.1.0",
    mechanism: "maintained native hooks", hostRange: "0.37.0",
    qualified: false, reason: "AUTHENTICATED_AUGGIE_RUN_REQUIRED_NO_GENERIC_MODEL_MOCK",
  },
  {
    id: "augment-ide", packageName: "@traceai/augment-plugin", version: "0.1.0",
    mechanism: "shared user hooks; separately qualified VS Code and IntelliJ profiles",
    qualified: false, reason: AUGMENT_IDE_HOLD, setupHold: AUGMENT_IDE_HOLD,
  },
  {
    id: "devin-cascade", packageName: "@traceai/devin-cascade", version: "0.1.0",
    mechanism: "Cascade transcript hooks (not Devin Local)", qualified: false,
    reason: "CASCADE_GUI_QUALIFICATION_REQUIRED", setupHold: "CASCADE_GUI_QUALIFICATION_REQUIRED",
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
  ...Object.entries({
    cursor: "cursor-plugin",
    "cursor-cli": "cursor-plugin",
    claude: "claude-code",
    "claude-code": "claude-code",
    codex: "codex-plugin",
    grok: "grok-plugin",
    antigravity: "antigravity-plugin",
    copilot: "copilot",
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
  switch (id) {
    case "muse": return museAdapter;
    case "gemini": return geminiAdapter;
    case "kiro-cli": return kiroAdapter;
    case "kiro-ide": return kiroIdeAdapter;
    case "augment": return augmentAdapter;
    case "augment-ide": return augmentIdeAdapter;
    case "devin-cascade": return cascadeAdapter;
    default: return undefined;
  }
}
