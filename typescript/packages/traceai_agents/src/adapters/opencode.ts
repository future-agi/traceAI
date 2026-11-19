import { execFile } from "node:child_process";
import { constants } from "node:fs";
import { open, realpath, lstat } from "node:fs/promises";
import { dirname, join } from "node:path";
import { parse, type ParseError } from "jsonc-parser";
import { readJson, updateJson, safeParents, type AdapterContext, type AdapterInspection, type CoreAdapter } from "@traceai/agent-core";

interface Ownership { schemaVersion: 1; target?: string; directory?: string }
const ownershipPath = (context: AdapterContext): string => join(context.config.paths.configDir, "opencode-registration.json");

export const OPENCODE_PACKAGE = "@traceai/opencode-plugin@0.1.0";
export const OPENCODE_HOST = "2.2.5";
export function supportedHost(version: string): boolean {
  const match = /^v?2\.(\d+)\.(\d+)$/.exec(version);
  return !!match && (Number(match[1]) > 2 || (Number(match[1]) === 2 && Number(match[2]) >= 5));
}
async function hostHealth(): Promise<{ hostVersion?: string; issues: string[] }> {
  try {
    const hostVersion = (await command(["--version"])).trim().replace(/^opencode\s+v?/, "");
    return { hostVersion, issues: supportedHost(hostVersion) ? [] : ["UNSUPPORTED_HOST_VERSION"] };
  } catch {
    return { issues: ["HOST_NOT_FOUND"] };
  }
}
// Pinned native package, model/tool, retry, child-session, reload and disable tests run in E2B.
export const OPENCODE_QUALIFIED = true;

function command(args: string[]): Promise<string> {
  const env = { ...process.env };
  delete env.FI_API_KEY;
  delete env.FI_SECRET_KEY;
  return new Promise((resolve, reject) => {
    execFile("opencode", args, { env, timeout: 120_000, maxBuffer: 256 * 1024, windowsHide: true }, (error, stdout) => {
      if (error) reject(new Error("OPENCODE_COMMAND_FAILED"));
      else resolve(stdout);
    });
  });
}
async function entries(path: string): Promise<unknown[]> {
  let fd;
  try {
    fd = await open(path, constants.O_RDONLY | constants.O_NOFOLLOW);
    const st = await fd.stat();
    if (!st.isFile() || st.nlink !== 1 || st.size > 1024 * 1024 || (st.mode & 0o022) ||
        (process.getuid && st.uid !== process.getuid()) || await realpath(path) !== path)
      throw new Error("UNSAFE_OPENCODE_CONFIG");
    const errors: ParseError[] = [];
    const data = parse(await fd.readFile("utf8"), errors, { allowTrailingComma: true });
    if (errors.length || !data || typeof data !== "object" || (data.plugins && !Array.isArray(data.plugins)))
      throw new Error("INVALID_OPENCODE_CONFIG");
    return data.plugins ?? [];
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") return [];
    throw error;
  } finally { await fd?.close(); }
}
function spec(entry: unknown): string | undefined {
  if (typeof entry === "string") return entry;
  if (entry && typeof entry === "object" && "package" in entry && typeof entry.package === "string") return entry.package;
}
async function inspect(context: AdapterContext): Promise<AdapterInspection> {
  const directory = dirname(context.config.paths.user);
  const paths = [join(directory, "opencode.json"), join(directory, "opencode.jsonc")];
  if (context.projectDir) for (const base of [context.projectDir, join(context.projectDir, ".opencode")])
    paths.push(join(base, "opencode.json"), join(base, "opencode.jsonc"));
  const configured = (await Promise.all(paths.map(entries))).flat().map(spec).filter((s): s is string => !!s);
  const owned = configured.filter(s => s === "@traceai/opencode-plugin" || s.startsWith("@traceai/opencode-plugin@"));
  const disabled = configured.includes("-futureagi.tracing") || configured.includes("-futureagi.*") || configured.includes("-*");
  const health = await hostHealth();
  return { registered: owned.length === 1 && !disabled, duplicate: owned.length > 1, ...health,
    trust: "unknown", detail: disabled ? "DISABLED_IN_HOST" : "RESTART_OR_RELOAD_REQUIRED" };
}
export const opencodeAdapter: CoreAdapter = {
  id: "opencode", version: "0.1.0",
  async detect(context) {
    const { hostVersion: version, issues } = await hostHealth();
    if (issues.length) return { supported: false, version, reason: issues[0] };
    if (context.scope !== "user") return { supported: false, version, reason: "NATIVE_INSTALL_IS_USER_SCOPED" };
    const directory = (await command(["debug", "paths", "config"])).trim();
    if (directory !== dirname(context.config.paths.user)) return { supported: false, version, reason: "HOST_CONFIG_DIRECTORY_MISMATCH" };
    return { supported: OPENCODE_QUALIFIED, version, reason: OPENCODE_QUALIFIED ? undefined : "OPENCODE_V2_QUALIFICATION_REQUIRED" };
  },
  async install(context) {
    if (!OPENCODE_QUALIFIED) throw new Error("OPENCODE_V2_QUALIFICATION_REQUIRED");
    if (!(await opencodeAdapter.detect(context)).supported) throw new Error("UNSUPPORTED_OPENCODE_HOST");
    const before = await inspect(context);
    if (before.duplicate) throw new Error("DUPLICATE_REGISTRATION");
    if (!before.registered) {
      const directory = dirname(context.config.paths.user);
      // Native plugin settings contain no credentials and OpenCode creates this directory as 0755.
      await safeParents(directory, true);
      if (process.getuid && (await lstat(directory)).uid !== process.getuid()) throw new Error("UNSAFE_OPENCODE_CONFIG");
      await command(["plugin", "add", OPENCODE_PACKAGE]);
      if ((await inspect(context)).registered)
        await updateJson<Ownership>(ownershipPath(context), () => ({ schemaVersion: 1, target: OPENCODE_PACKAGE, directory }));
    }
    return inspect(context);
  },
  inspect,
  async uninstall(context) {
    // A pre-existing native installation never becomes owned just because setup observes it.
    const directory = dirname(context.config.paths.user);
    const owned = await readJson<Ownership>(ownershipPath(context));
    if (owned?.target !== OPENCODE_PACKAGE || owned.directory !== directory) return inspect(context);
    const configured = (await Promise.all(["opencode.json", "opencode.jsonc"].map(f => entries(join(directory, f))))).flat();
    if (configured.some(e => spec(e) === OPENCODE_PACKAGE)) await command(["plugin", "remove", OPENCODE_PACKAGE]);
    await updateJson<Ownership>(ownershipPath(context), () => ({ schemaVersion: 1 }));
    return inspect(context);
  },
};
