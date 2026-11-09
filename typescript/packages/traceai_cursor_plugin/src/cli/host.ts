import { constants } from "node:fs";
import { access, realpath, stat } from "node:fs/promises";
import { delimiter, isAbsolute, join } from "node:path";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { INSPECTED_CLI_VERSION, CLI_RELEASE_HOLD } from "./capabilities.js";
const exec = promisify(execFile);

export interface CursorCliHost {
  path?: string;
  version?: string;
  installed: boolean;
  inspectedVersion: boolean;
  qualified: false;
  reason: string;
}
/** Discovery executes only version/help, never login/status or a paid generation.
 * No shell, no credential flags, no home auth-cache reads. The executable is
 * resolved from the caller's PATH (or a setup-owned absolute path), never a hook payload.
 */
export async function discoverCursorCli(options: {
  env?: NodeJS.ProcessEnv; executable?: string;
} = {}): Promise<CursorCliHost> {
  const env = { ...(options.env ?? process.env) };
  for (const name of Object.keys(env))
    if (/^(FI_(API_KEY|SECRET_KEY)|CURSOR_API_KEY|OPENAI_API_KEY|ANTHROPIC_API_KEY)$/.test(name)) delete env[name];
  const paths = options.executable ? [options.executable] :
    (env.PATH ?? "").split(delimiter).filter(isAbsolute)
      .flatMap(directory => [join(directory, "agent"), join(directory, "cursor-agent")]);
  const seen = new Set<string>();
  const deadline = Date.now() + 5000;
  let attempts = 0;
  for (const path of paths.slice(0, 128)) {
    if (Date.now() >= deadline || attempts >= 3) break;
    try {
      if (!isAbsolute(path)) continue;
      const absolute = await realpath(path);
      if (seen.has(absolute)) continue;
      seen.add(absolute);
      await access(absolute, constants.X_OK);
      if (!(await stat(absolute)).isFile()) continue;
      attempts++;
      const result = await exec(absolute, ["--version"], { env, timeout: Math.max(1, Math.min(2500, deadline - Date.now())), maxBuffer: 32768 });
      const version = result.stdout.trim();
      if (!/^\d{4}\.\d{2}\.\d{2}(?:-[a-zA-Z0-9]{4,32})?$/.test(version)) continue;
      if (Date.now() >= deadline) break;
      const help = await exec(absolute, ["--help"], { env, timeout: Math.max(1, Math.min(2500, deadline - Date.now())), maxBuffer: 65536 });
      if (!help.stdout.includes("Start the Cursor Agent") || !help.stdout.includes("--output-format")) continue;
      return { path: absolute, version, installed: true,
        inspectedVersion: version === INSPECTED_CLI_VERSION, qualified: false,
        reason: version === INSPECTED_CLI_VERSION ? CLI_RELEASE_HOLD : "CURSOR_CLI_VERSION_NOT_QUALIFIED" };
    } catch { /* Missing, stale, hung, or unrelated binaries must not break status. */ }
  }
  return { installed: false, inspectedVersion: false, qualified: false, reason: "CURSOR_CLI_NOT_FOUND" };
}
