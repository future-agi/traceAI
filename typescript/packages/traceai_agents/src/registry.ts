import { hermesAdapter } from "@traceai/hermes-agent";
import type { CoreAdapter } from "@traceai/agent-core";
import { codexAdapter, HOST_VERSION } from "@traceai/codex-plugin";
import { opencodeV1Adapter } from "./opencode-v1.js";
import { antigravityAdapter } from "@traceai/antigravity-plugin";
import { copilotAdapter, HOST_VERSION as COPILOT_HOST_VERSION } from "@traceai/copilot";
import { openclawAdapter } from "@traceai/openclaw";
import { cursorAdapter } from "@traceai/cursor-plugin";
import { cursorCliAdapter } from "@traceai/cursor-plugin/cli";
import { grokAdapter, HOST_RANGE as GROK_HOST_RANGE } from "@traceai/grok-plugin";
import { opencodeAdapter, OPENCODE_HOST, OPENCODE_QUALIFIED } from "./adapters/opencode.js";
import { claudeAdapter, CAPABILITIES, TESTED_HOST as CLAUDE_TESTED_HOST } from "@traceai/claude-code";
import { gooseAdapter } from "@traceai/goose";
import { piAdapter } from "@traceai/pi-plugin/adapter";
import { ohMyPiAdapter } from "@traceai/oh-my-pi-plugin/adapter";

// Release metadata must be readable without installing held/private packages.
// Keep these pinned evidence/hold values in sync with the adapter exports.
const EDITOR_HOLD = "VSCODE_GUI_AND_TRANSPORT_QUALIFICATION_REQUIRED";
const KIRO_IDE_HOLD = "KIRO_IDE_GUI_AND_PAYLOAD_QUALIFICATION_REQUIRED";
const AUGMENT_IDE_HOLD = "AUGMENT_IDE_GUI_QUALIFICATION_REQUIRED";
const GEMINI_TESTED_HOST = "0.67.0";

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
  /** Held surfaces reject activation before credentials, network or host changes. */
  setupHold?: string;
}
/** Qualification describes real-host evidence, not npm availability or Cloud readback. */
const explicitAdapters: AdapterRelease[] = [
  { id: "hermes", packageName: "@traceai/hermes-agent", version: "0.1.0", mechanism: "hermes plugins install", hostRange: "git:90d6b98fd86d20981ee06ddd5b7d93e4320f26a4 (CLI)", qualified: true, reason: "CLI_QUALIFIED_RELEASE_PENDING" },
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
    hostRange: "agy 1.x (Linux/macOS CLI; tested 1.5.2; macOS App 2.28.2 with agy 1.4.2)",
    qualified: true,
    reason: "CLI_AND_MAC_APP_2_28_2_CLOUD_READBACK_QUALIFIED",
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
    id: "openclaw", packageName: "@traceai/openclaw", version: "0.1.0",
    mechanism: "official diagnostics-otel with in-process normalization; explicit local launcher",
    hostRange: ">=2026.10.5 <2027.0.0", qualified: true,
    reason: "LOCAL_AGENT_QUALIFIED_GATEWAY_NOT_SUPPORTED",
  },
  {
    id: "muse", packageName: "@traceai/muse-plugin", version: "0.1.0",
    mechanism: "native argv hooks + explicit approval", hostRange: "1.4.14-R7309.1 (Linux)",
    qualified: false, reason: "MUSE_TRANSCRIPT_AND_CHILD_ANCESTRY_UNQUALIFIED",
  },
  {
    id: "gemini", packageName: "@traceai/gemini-cli", version: "0.1.0",
    mechanism: "exclusive native hooks", hostRange: GEMINI_TESTED_HOST,
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
    id: "claude",
    packageName: "@traceai/claude-code",
    version: "0.1.0",
    mechanism: "exclusive-transcript-hooks",
    hostRange: CLAUDE_TESTED_HOST,
    qualified: true,
    reason: CAPABILITIES.qualification,
  },
  {
    id: "goose",
    packageName: "@traceai/goose",
    version: "0.1.0",
    mechanism: "native hooks and saved provider-response records",
    hostRange: "1.54.0",
    qualified: true,
    reason: "QUALIFIED_GOOSE_CLI_1_54_0",
  },
  {
    id: "pi",
    packageName: "@traceai/pi-plugin",
    version: "0.1.0",
    mechanism: "pi install",
    hostRange: ">=1.5.0 <2.0.0",
    qualified: true,
    reason: "PI_1_5_0_NATIVE_HOST_QUALIFIED",
  },
  {
    id: "opencode",
    packageName: "@traceai/opencode-plugin",
    version: "0.1.0",
    mechanism: "opencode plugin add",
    hostRange: OPENCODE_HOST,
    qualified: OPENCODE_QUALIFIED,
    reason: OPENCODE_QUALIFIED ? "PINNED_HOST_QUALIFIED" : "OPENCODE_V2_QUALIFICATION_REQUIRED",
  },
  {
    id: "opencode-v1",
    packageName: "@traceai/opencode-v1-plugin",
    version: "0.1.0",
    mechanism: "configuration",
    hostRange: ">=1.19.0 <2.0.0",
    qualified: true,
    reason: "REGISTRY_PUBLICATION_REQUIRED",
  },
  {
    id: "oh-my-pi",
    packageName: "@traceai/oh-my-pi-plugin",
    version: "0.1.0",
    mechanism: "omp install",
    hostRange: ">=18.17.0 <19.0.0",
    qualified: true,
    reason: "OMP_18_17_0_NATIVE_PACKAGE_E2B",
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
    mechanism: "native Copilot OTLP via Future AGI VS Code extension",
    qualified: false,
    reason: EDITOR_HOLD,
  },
  {
    id: "grok",
    packageName: "@traceai/grok-plugin",
    version: "0.1.0",
    mechanism: "grok plugin install",
    hostRange: GROK_HOST_RANGE,
    qualified: true,
    reason: "REGISTRY_PUBLICATION_REQUIRED",
  },
  {
    id: "cursor", packageName: "@traceai/cursor-plugin", version: "0.1.0",
    mechanism: "persistent user hooks; no local-plugin policy changes",
    qualified: true, hostRange: "3.x hook schema (3.24.18, 3.26.13 observed)",
    reason: "CURSOR_3_26_13_MAC_CLOUD_READBACK_QUALIFIED",
  },
  {
    id: "cursor-cli", packageName: "@traceai/cursor-plugin", version: "0.1.0",
    mechanism: "shared user hooks with deferred transcript finisher; print stream pending qualification",
    hostRange: "2026.11.05 CLI (macOS)",
    qualified: true, reason: "CURSOR_CLI_MAC_CLOUD_READBACK_QUALIFIED",
  },
  {
    id: "cursor-cloud", packageName: "@traceai/cursor-plugin", version: "0.1.0",
    mechanism: "environment install and repository command hooks; injected credentials only",
    qualified: false, reason: "AUTHORIZED_CLOUD_RUN_AND_NPM_AVAILABILITY_REQUIRED",
  },
];
const explicitIds = new Set(explicitAdapters.map((a) => a.id));
export const adapterRegistry: readonly AdapterRelease[] = [
  ...explicitAdapters,
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
    "devin-local": "devin-plugin",
  })
    .filter(([id]) => !explicitIds.has(id))
    .map(([id, name]) => ({
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
/** Qualified public packages are installed with the CLI. Held/private packages
 * are optional, resolved only from these fixed package specifiers, never from
 * project configuration. Availability is separate from real-host qualification.
 */
export function loadAdapter(id: string): CoreAdapter | undefined {
  if (id === "pi") return piAdapter;
  if (id === "oh-my-pi") return ohMyPiAdapter;
  if (id === "goose") return gooseAdapter;
  if (id === "claude") return claudeAdapter;
  if (id === "opencode") return opencodeAdapter;
  if (id === "codex") return codexAdapter;
  if (id === "opencode-v1") return opencodeV1Adapter;
  if (id === "antigravity") return antigravityAdapter;
  if (id === "copilot") return copilotAdapter;
  if (id === "hermes") return hermesAdapter;
  if (id === "openclaw") return openclawAdapter;
  if (id === "grok") return grokAdapter;
  if (id === "cursor") return cursorAdapter;
  if (id === "cursor-cli") return cursorCliAdapter;
  try {
    switch (id) {
      case "copilot-vscode":
        return (require("@traceai/copilot/vscode") as { vscodeAdapter: CoreAdapter }).vscodeAdapter;
      case "muse":
        return (require("@traceai/muse-plugin") as { museAdapter: CoreAdapter }).museAdapter;
      case "gemini":
        return (require("@traceai/gemini-cli") as { geminiAdapter: CoreAdapter }).geminiAdapter;
      case "kiro-cli":
        return (require("@traceai/kiro-plugin") as { kiroAdapter: CoreAdapter }).kiroAdapter;
      case "kiro-ide":
        return (require("@traceai/kiro-plugin/ide") as { kiroIdeAdapter: CoreAdapter }).kiroIdeAdapter;
      case "augment":
        return (require("@traceai/augment-plugin") as { augmentAdapter: CoreAdapter }).augmentAdapter;
      case "augment-ide":
        return (require("@traceai/augment-plugin") as { augmentIdeAdapter: CoreAdapter }).augmentIdeAdapter;
      case "devin-cascade":
        return (require("@traceai/devin-cascade") as { cascadeAdapter: CoreAdapter }).cascadeAdapter;
      case "cursor-cloud":
        return (require("@traceai/cursor-plugin/cloud") as { cursorCloudAdapter: CoreAdapter }).cursorCloudAdapter;
      default: return undefined;
    }
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== "MODULE_NOT_FOUND") throw error;
    return undefined;
  }
}
