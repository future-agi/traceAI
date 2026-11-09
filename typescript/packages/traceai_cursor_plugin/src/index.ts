import { cursorDiagnostics } from "./diagnostics.js";
import { readFile } from "node:fs/promises";
import { disable, type CoreAdapter } from "@traceai/agent-core";
import {
  FIXTURE_HOST_VERSION,
  VERSION,
  supportedHostVersion,
} from "./events.js";
import {
  installRuntime,
  register,
  inspect,
  unregister,
} from "./registration.js";

export const qualification = {
  host: "Cursor IDE",
  fixtureVersion: FIXTURE_HOST_VERSION,
  live: true,
  observedVersions: ["3.24.18", "3.26.13"],
  route: "finalized-standard-otlp-v1",
  blockers: ["NPM_AVAILABILITY_REQUIRED"],
} as const;
export const cursorAdapter: CoreAdapter = {
  id: "cursor",
  version: VERSION,
  requiresWorker: false,
  diagnostics: cursorDiagnostics,
  detect: () => detectHost(),
  async install(context) {
    // Update uses the same host gate as setup; never run a host binary to detect it.
    const host = await cursorAdapter.detect(context);
    if (!host.supported) throw new Error("UNSUPPORTED_HOST_VERSION");
    const previous = await inspect(context);
    if (previous.duplicate) throw new Error("CURSOR_DUPLICATE_REGISTRATION");
    return register(context, await installRuntime(context));
  },
  inspect,
  async uninstall(context) {
    await disable({
      agent: "cursor",
      env: {
        ...process.env,
        FI_AGENT_CONFIG_DIR: context.config.paths.configDir,
      },
    });
    return unregister(context);
  },
};
export { CursorObserver } from "./observer.js";
export { TranscriptReader, correlate } from "./transcript.js";
export { normalizeEvent, EVENTS } from "./events.js";
export { hook as processCursorHook } from "./hook.js";
export { appendEvent, routeId } from "./state.js";
export {
  commands,
  register,
  unregister,
  inspect,
  installRuntime,
} from "./registration.js";

/** Host package metadata is read as data only, including under root:admin 775. */
export async function detectHost(candidates?: string[]) {
  const paths =
    candidates ??
    (process.platform === "darwin"
      ? ["/Applications/Cursor.app/Contents/Resources/app/package.json"]
      : [
          "/usr/share/cursor/resources/app/package.json",
          "/opt/Cursor/resources/app/package.json",
        ]);
  for (const path of paths) {
    try {
      // Host package version is data, not executable code or protected config.
      const raw = await readFile(path, "utf8");
      const version = raw ? JSON.parse(raw).version : undefined;
      if (typeof version === "string" && version.length < 64)
        return {
          supported: supportedHostVersion(version),
          version,
          reason: supportedHostVersion(version)
            ? undefined
            : "UNKNOWN_CURSOR_VERSION",
        };
    } catch {
      /* Do not run arbitrary PATH executables or guess a version. */
    }
  }
  return { supported: false, reason: "CURSOR_IDE_NOT_FOUND_OR_UNQUALIFIED" };
}
