import { execFile, spawn } from "node:child_process";
import { dirname, isAbsolute, join, resolve } from "node:path";
import { homedir } from "node:os";
import { readJson, readProtected, contentHash, updateJson, type CoreAdapter, type AdapterContext, type AdapterInspection } from "@traceai/agent-core";

export const VERSION = "0.1.0"; // x-release-please-version
export const PYTHON_VERSION = "0.1.1";
export const NATIVE_SOURCE = "future-agi/traceAI/python/frameworks/hermes-agent/hermes-plugin/futureagi";
export const TESTED_HOST_COMMIT = "90d6b98fd86d20981ee06ddd5b7d93e4320f26a4";

function query(args: string[], env: NodeJS.ProcessEnv = process.env): Promise<string> {
  return new Promise((ok, fail) => {
    execFile("hermes", args, { env, timeout: 30000, maxBuffer: 256 * 1024, encoding: "utf8" }, (error, stdout) => {
      if (error) fail(new Error("HERMES_COMMAND_FAILED"));
      else ok(stdout.replace(/\u001b\[[0-9;]*m/g, ""));
    });
  });
}

/** Ask Hermes, rather than guessing the sticky active profile or inspecting .env. */
export async function discoverHermesDirectory(env: NodeJS.ProcessEnv = process.env): Promise<string> {
  const output = env.HERMES_HOME || (await query(["profile"], env)).match(/^Path:\s+(.+)$/m)?.[1]?.trim();
  if (!output) throw new Error("HERMES_PROFILE_DISCOVERY_FAILED");
  const path = output.startsWith("~/") ? join(env.HOME || homedir(), output.slice(2)) : output;
  if (!isAbsolute(path) || /[\x00-\x1f]/.test(path) || path.includes("$")) throw new Error("HERMES_PROFILE_PATH_INVALID");
  return resolve(path);
}

function environment(context: AdapterContext): NodeJS.ProcessEnv {
  // Pin the profile already discovered before setup. A later sticky-profile
  // change must not install into a different profile from the one configured.
  return { ...process.env, HERMES_HOME: dirname(context.config.paths.user) };
}
function native(context: AdapterContext, args: string[]): Promise<number> {
  return new Promise((ok) => {
    // No --yes-deps / --force / capability overrides: the native prompt is the
    // user's consent boundary. In non-TTY mode Hermes may leave it disabled.
    const child = spawn("hermes", args, { env: environment(context), stdio: "inherit", shell: false });
    child.once("error", () => ok(1));
    child.once("exit", (code) => ok(code ?? 1));
  });
}
interface PluginRow { name: string; version: string; status: string; source: string }
async function plugins(context: AdapterContext): Promise<PluginRow[]> {
  const raw = await query(["plugins", "list", "--json"], environment(context));
  const start = raw.search(/^\[/m);
  if (start < 0 && raw.includes("No plugins installed.")) return [];
  const rows: unknown = JSON.parse(raw.slice(start));
  if (!Array.isArray(rows)) throw new Error("HERMES_PLUGIN_STATUS_INVALID");
  return rows.filter((r): r is PluginRow => r && typeof r === "object" && r.name === "futureagi");
}
async function inspect(context: AdapterContext): Promise<AdapterInspection> {
  const rows = await plugins(context);
  const hostVersion = (await query(["--version"], environment(context))).split("\n")[0].slice(0, 160);
  const ready = rows.length === 1 && rows[0].status === "enabled" && rows[0].version === PYTHON_VERSION;
  return { registered: ready, hostVersion, duplicate: rows.length > 1, trust: ready ? "confirmed" : "required",
    detail: ready ? "Native Python observer enabled. Restart an existing Hermes gateway when idle; new CLI clients reuse its launch environment. No Node runtime required."
      : rows.length ? "Check Hermes dependency consent, plugin version and enablement; run hermes plugins enable futureagi."
      : "Native Future AGI plugin is not installed." };
}
interface Ownership { schemaVersion: 1; profiles: Record<string, { source: string; version: string; digest: string }> }
function ownership(context: AdapterContext): string { return join(context.config.paths.configDir, "hermes-install.json"); }
async function pluginDigest(context: AdapterContext): Promise<string> {
  const directory = join(dirname(context.config.paths.user), "plugins", "futureagi");
  const manifest = await readProtected(join(directory, "plugin.yaml"), false);
  const wrapper = await readProtected(join(directory, "__init__.py"), false);
  const dependencies = await readProtected(join(directory, "pyproject.toml"), false);
  if (manifest === undefined || wrapper === undefined || dependencies === undefined) throw new Error("HERMES_PLUGIN_MISSING");
  return contentHash(JSON.stringify([manifest, wrapper, dependencies]));
}


/** Explicit embedding/test seam for a reviewed native distribution source.
 * The customer CLI always uses NATIVE_SOURCE; config/events cannot select code.
 */
export function createHermesAdapter(nativeSource = NATIVE_SOURCE): CoreAdapter {
return {
  id: "hermes", version: VERSION, requiresWorker: false,
  async detect(context) {
    if (process.platform === "win32") return { supported: false, reason: "WINDOWS_ACL_UNSUPPORTED" };
    try {
      const help = await query(["plugins", "install", "--help"], environment(context));
      const listing = await query(["plugins", "list", "--help"], environment(context));
      return { supported: help.includes("--enable") && listing.includes("--json") };
    } catch { return { supported: false, reason: "HERMES_NATIVE_PLUGINS_REQUIRED" }; }
  },
  async install(context) {
    const rows = await plugins(context);
    if (rows.length) return inspect(context); // never overwrite or adopt someone else's install
    const code = await native(context, ["plugins", "install", nativeSource, "--enable"]);
    // Ownership records only what this installer actually introduced, not every
    // entry named futureagi. Repeating setup leaves all unrelated plugins alone.
    if ((await plugins(context)).length === 1) {
      const digest = await pluginDigest(context);
      await updateJson<Ownership>(ownership(context), (old) => ({ schemaVersion: 1, profiles: {
        ...old?.profiles, [dirname(context.config.paths.user)]: { source: nativeSource, version: PYTHON_VERSION, digest },
      } }));
    }
    if (code !== 0) return { registered: false, trust: "required", detail: "Native installation did not complete. No consent was bypassed." };
    return inspect(context);
  },
  inspect,
  async update(context) {
    const home = dirname(context.config.paths.user);
    const owned = (await readJson<Ownership>(ownership(context)))?.profiles[home];
    if (!owned || owned.source !== nativeSource || owned.digest !== await pluginDigest(context))
      return { registered: false, detail: "Update through Hermes: installation is unowned or locally changed." };
    const code = await native(context, ["plugins", "update", "futureagi"]);
    if (code !== 0) return { registered: false, trust: "required", detail: "Native update did not complete." };
    const digest = await pluginDigest(context);
    await updateJson<Ownership>(ownership(context), old => ({ schemaVersion: 1, profiles: {
      ...old?.profiles, [home]: { ...owned, digest },
    } }));
    return inspect(context);
  },
  async uninstall(context) {
    const home = dirname(context.config.paths.user);
    const owned = await readJson<Ownership>(ownership(context));
    const rows = await plugins(context);
    if (owned?.profiles[home]?.source !== nativeSource || rows.length !== 1 || rows[0].version !== owned.profiles[home].version || owned.profiles[home].digest !== await pluginDigest(context))
      return { registered: rows.length > 0, detail: "Not removed: native installation is not owned by this setup." };
    await native(context, ["plugins", "remove", "futureagi"]);
    const remaining = await plugins(context);
    if (!remaining.length) await updateJson<Ownership>(ownership(context), (old) => {
      const profiles = { ...old?.profiles }; delete profiles[home]; return { schemaVersion: 1, profiles };
    });
    return { registered: remaining.length > 0 };
  },
  async run(context, args) { return native(context, ["chat", ...args]); },
};
}
export const hermesAdapter = createHermesAdapter();
