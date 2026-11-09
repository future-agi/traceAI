import { cursorDiagnostics } from "../diagnostics.js";
import { type CoreAdapter } from "@traceai/agent-core";
import { supportedHostVersion } from "../events.js";
import { installRuntime, register, inspect, unregister } from "../registration.js";
import { discoverCursorCli } from "./host.js";
import { CURSOR_CLI_VERSION } from "./capabilities.js";

/** CLI and IDE share one hook registration, configuration and capture identity.
 * Only the shared hook route is qualified; the independent print-stream route
 * still cannot be launched through this adapter.
 */
export const cursorCliAdapter: CoreAdapter = {
  id: "cursor-cli", version: CURSOR_CLI_VERSION, requiresWorker: false,
  diagnostics: cursorDiagnostics,
  async detect() {
    const host = await discoverCursorCli();
    const supported = host.installed && supportedHostVersion(host.version);
    return { supported, version: host.version,
      reason: supported ? undefined : host.reason };
  },
  async install(context) {
    const host = await cursorCliAdapter.detect(context);
    if (!host.supported) throw new Error(host.reason ?? "CURSOR_CLI_NOT_FOUND");
    const previous = await inspect(context);
    if (previous.duplicate) throw new Error("CURSOR_DUPLICATE_REGISTRATION");
    return register(context, await installRuntime(context));
  },
  async inspect(context) {
    const host = await cursorCliAdapter.detect(context);
    const hooks = await inspect(context);
    return { ...hooks, registered: hooks.registered && host.supported,
      hostVersion: host.version, detail: host.reason ?? hooks.detail };
  },
  // Both names control the same installation, rather than leaving enabled
  // capture behind after the user explicitly removes its CLI alias.
  uninstall: unregister,
  async run() {
    // A second stream observer could duplicate the shared-hook source.
    return 2;
  },
};
