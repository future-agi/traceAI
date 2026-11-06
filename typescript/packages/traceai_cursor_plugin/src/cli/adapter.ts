import { cursorDiagnostics } from "../diagnostics.js";
import { readJson, type AdapterContext, type CoreAdapter, type Manifest } from "@traceai/agent-core";
import { inspect, unregister } from "../registration.js";
import { discoverCursorCli } from "./host.js";
import { CURSOR_CLI_VERSION, CLI_RELEASE_HOLD } from "./capabilities.js";

/** Both Cursor surfaces inspect one shared executable and ownership receipt.
 * Installation/activation stays held until an actual CLI hook capture is
 * qualified. A successful IDE registration must never silently satisfy this gate.
 * The inspector still reports the concrete host version and foreign duplicates.
 */
export const cursorCliAdapter: CoreAdapter = {
  id: "cursor-cli", version: CURSOR_CLI_VERSION, requiresWorker: false,
  diagnostics: cursorDiagnostics,
  async detect() {
    const host = await discoverCursorCli();
    return { supported: false, version: host.version, reason: host.reason };
  },
  async install() {
    return { registered: false, activated: false, trust: "unknown", detail: CLI_RELEASE_HOLD };
  },
  async inspect(context: AdapterContext) {
    const host = await discoverCursorCli();
    const hooks = await inspect(context);
    return { ...hooks, registered: false, activated: false, hostVersion: host.version,
      detail: !host.installed ? host.reason : hooks.registered
        ? "CURSOR_SHARED_HOOKS_PRESENT_CLI_UNQUALIFIED" : host.reason };
  },
  async uninstall(context: AdapterContext) {
    // A CLI uninstall must not remove the shared executable's IDE registration.
    const manifest = await readJson<Manifest>(context.config.paths.manifest);
    if (manifest?.registrations?.cursor?.status === "REGISTERED")
      return { registered: false, activated: false, detail: "CURSOR_IDE_SHARED_HOOKS_RETAINED" };
    return unregister(context);
  },
  async run() {
    // Running a second stream route alongside user/plugin/preview hooks would
    // evade the unresolved hook/stream source-alias gate. No public bypass flag.
    return 2;
  },
};
