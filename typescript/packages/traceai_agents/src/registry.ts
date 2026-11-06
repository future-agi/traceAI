import { hermesAdapter } from "@traceai/hermes-agent";
import type { CoreAdapter } from "@traceai/agent-core";
import { codexAdapter, HOST_VERSION } from "@traceai/codex-plugin";
import { opencodeV1Adapter } from "./opencode-v1.js";
import { antigravityAdapter } from "@traceai/antigravity-plugin";
import { copilotAdapter, HOST_VERSION as COPILOT_HOST_VERSION } from "@traceai/copilot";
import { vscodeAdapter, EDITOR_HOLD } from "@traceai/copilot/vscode";
import { openclawAdapter } from "@traceai/openclaw";
import { grokAdapter, HOST_RANGE } from "@traceai/grok-plugin";
import { museAdapter } from "@traceai/muse-plugin";
import { geminiAdapter, TESTED_HOST } from "@traceai/gemini-cli";
import { kiroAdapter } from "@traceai/kiro-plugin";
import { kiroIdeAdapter, IDE_HOLD as KIRO_IDE_HOLD } from "@traceai/kiro-plugin/ide";
import { augmentAdapter, augmentIdeAdapter, IDE_HOLD as AUGMENT_IDE_HOLD } from "@traceai/augment-plugin";
import { cascadeAdapter } from "@traceai/devin-cascade";
import { opencodeAdapter, OPENCODE_HOST, OPENCODE_QUALIFIED } from "./adapters/opencode.js";
import { claudeAdapter, CAPABILITIES, TESTED_HOST } from "@traceai/claude-code";
import { gooseAdapter } from "@traceai/goose";
import { piAdapter } from "@traceai/pi-plugin/adapter";
import { ohMyPiAdapter } from "@traceai/oh-my-pi-plugin/adapter";

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
export const adapterRegistry: readonly AdapterRelease[] = [
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
    hostRange: "1.x (Linux CLI; tested 1.5.2)",
    qualified: true,
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
    id: "claude",
    packageName: "@traceai/claude-code",
    version: "0.1.0",
    mechanism: "exclusive-transcript-hooks",
    hostRange: TESTED_HOST,
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
    qualified: false,
    reason: "REGISTRY_PUBLICATION_REQUIRED",
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
    mechanism: "native VS Code settings (harness-specific)",
    qualified: false,
    reason: EDITOR_HOLD,
    setupBlocker: EDITOR_HOLD,
  },
  {
    id: "grok",
    packageName: "@traceai/grok-plugin",
    version: "0.1.0",
    mechanism: "grok plugin install",
    hostRange: HOST_RANGE,
    qualified: true,
    reason: "REGISTRY_PUBLICATION_REQUIRED",
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
  if (id === "pi") return piAdapter;
  if (id === "oh-my-pi") return ohMyPiAdapter;
  if (id === "goose") return gooseAdapter;
  if (id === "claude") return claudeAdapter;
  if (id === "opencode") return opencodeAdapter;
  if (id === "codex") return codexAdapter;
  if (id === "opencode-v1") return opencodeV1Adapter;
  if (id === "antigravity") return antigravityAdapter;
  if (id === "copilot") return copilotAdapter;
  if (id === "copilot-vscode") return vscodeAdapter;
  if (id === "hermes") return hermesAdapter;
  if (id === "openclaw") return openclawAdapter;
  if (id === "grok") return grokAdapter;
  switch (id) {
    case "muse": return museAdapter;
    case "gemini": return geminiAdapter;
    case "kiro-cli": return kiroAdapter;
    case "kiro-ide": return kiroIdeAdapter;
    case "augment": return augmentAdapter;
    case "augment-ide": return augmentIdeAdapter;
    case "devin-cascade": return cascadeAdapter;
    default: return undefined;
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
