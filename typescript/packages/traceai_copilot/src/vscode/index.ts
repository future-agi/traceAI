import type { AdapterInspection, CoreAdapter } from "@traceai/agent-core";
import { inspectEditor, type DiscoveryOptions } from "./discovery.js";
import { EDITOR_HOLD } from "./settings.js";

export { inspectEditor, inspectSettings, discoverSettings } from "./discovery.js";
export { EDITOR_HOLD, qualifiedEditorVersions } from "./settings.js";
export type { Harness } from "./settings.js";
export type { EditorInspection, EditorLocation, DiscoveryOptions } from "./discovery.js";

/** No daemon, extension, settings writes or model/provider change. The installer deliberately
 * stays closed until a real editor surface and a credential-safe delivery route are qualified.
 * CLI normalization belongs to the Copilot CLI adapter; no duplicate span instrumentation here.
 */
export function createVscodeAdapter(options: DiscoveryOptions = {}): CoreAdapter {
  async function inspect(projectDir?: string): Promise<AdapterInspection> {
    try {
      const result = await inspectEditor({ ...options, projectDir: projectDir ?? options.projectDir });
      return {
        registered: false, activated: false, trust: "unknown",
        duplicate: result.issues.includes("VSCODE_MULTIPLE_TELEMETRY_HARNESSES"),
        detail: EDITOR_HOLD, issues: result.issues,
      };
    } catch (error) {
      // File paths and user-supplied setting/header values never become diagnostics.
      const code = error instanceof Error && /^[A-Z][A-Z0-9_]{0,80}$/.test(error.message)
        ? error.message : "VSCODE_SETTINGS_INSPECTION_FAILED";
      return { registered: false, activated: false, trust: "unknown", detail: EDITOR_HOLD, issues: [code] };
    }
  }
  return {
    id: "copilot-vscode", version: "0.1.0", requiresWorker: false,
    diagnostics: { [EDITOR_HOLD]: "Editor tracing is unavailable until the GUI and credential-safe transport are qualified. Native settings were not changed." },
    async detect() { return { supported: false, reason: EDITOR_HOLD }; },
    async install(context) { return inspect(context.projectDir); },
    async inspect(context) { return inspect(context.projectDir); },
    async uninstall() {
      // No shipping version has installed anything. Do not claim ownership of native settings.
      return { registered: false, activated: false, detail: "VSCODE_NO_OWNED_REGISTRATION" };
    },
  };
}
export const vscodeAdapter = createVscodeAdapter();
