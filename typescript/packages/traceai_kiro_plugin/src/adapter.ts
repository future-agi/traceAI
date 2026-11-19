import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { constants, promises as fs } from "node:fs";
import { dirname, join, resolve } from "node:path";
import {
  contentHash, ensurePrivateDirectory, readJson, readProtected, safeParents, updateJson, writeProtected,
  type CoreAdapter, type AdapterContext, type AdapterInspection,
} from "@traceai/agent-core";
import { EVENTS } from "./events.js";

export const VERSION = "0.1.0"; // x-release-please-version
export const HOST_VERSION = "3.0.0";
const exec = promisify(execFile);
const quote = (v: string): string => `'${v.replace(/'/g, `'\\''`)}'`;
interface Receipt { version: 1; hookPath: string; hookHash: string; runtimePath: string; runtimeHash: string; nodePath: string; }
const receiptPath = (c: AdapterContext): string => join(c.config.paths.configDir, "kiro-plugin.json");
const hookPath = (c: AdapterContext): string => join(dirname(c.config.paths.user), "hooks", "futureagi-kiro-cli.json");
const runtimePath = (c: AdapterContext): string => join(c.config.paths.configDir, "runtime", `kiro-${VERSION}`, "hook.cjs");
const marker = /fi-hook-to-otlp|@traceai\/kiro|futureagi-kiro|fi-kiro-hook|kiro-[0-9.]+\/hook\.cjs/;

/** Only inspect bounded hook/profile configuration, never transcripts or auth caches. */
async function duplicate(context: AdapterContext): Promise<boolean> {
  const dirs = new Set([dirname(context.config.paths.user), join(resolve(context.projectDir ?? process.cwd()), ".kiro")]);
  for (const dir of dirs) {
    for (const kind of ["hooks", "agents"]) {
      const root = join(dir, kind);
      let names: string[];
      try { await safeParents(root); names = await fs.readdir(root); }
      catch (e) { if ((e as NodeJS.ErrnoException).code === "ENOENT") continue; throw e; }
      if (names.length > 256) throw new Error("HOOK_INVENTORY_LIMIT");
      for (const name of names) {
        if (!/\.(json|md)$/.test(name)) continue;
        const file = join(root, name);
        if (file === hookPath(context)) continue;
        const value = await readProtected(file, false);
        if (!value) continue;
        const hooks = kind === "agents" && name.endsWith(".json") ? JSON.stringify(JSON.parse(value).hooks ?? []) : value;
        if (marker.test(hooks)) return true;
      }
    }
  }
  return false;
}
async function readRuntime(path: string): Promise<Buffer> {
  await safeParents(dirname(path));
  const file = await fs.open(path, constants.O_RDONLY | constants.O_NOFOLLOW);
  try {
    const st = await file.stat();
    if (!st.isFile() || st.nlink !== 1 || st.size > 8 * 1024 * 1024 || (process.getuid && st.uid !== process.getuid())) throw new Error("UNSAFE_RUNTIME");
    return await file.readFile();
  } finally { await file.close(); }
}
function manifest(node: string, runtime: string, configDir: string, agentDir: string): string {
  return JSON.stringify({ version: "v1", hooks: EVENTS.map(trigger => ({
    name: `Future AGI ${trigger}`, description: "Observe lifecycle and tools without changing agent context",
    trigger, enabled: true, timeout: 7,
    action: { type: "command", command: `(${quote(node)} ${quote(runtime)} ${quote(trigger)} ${quote(configDir)} ${quote(agentDir)} >/dev/null 2>&1) || true` },
  })) }, null, 2) + "\n";
}
async function inspect(context: AdapterContext): Promise<AdapterInspection> {
  const receipt = await readJson<Receipt>(receiptPath(context));
  const extra = await duplicate(context);
  if (!receipt) return { registered: false, duplicate: extra, detail: "SETUP_REQUIRED" };
  if (receipt.hookPath !== hookPath(context) || receipt.runtimePath !== runtimePath(context)) return { registered: false, detail: "OWNERSHIP_REVIEW_REQUIRED" };
  const hooks = await readProtected(receipt.hookPath);
  let validRuntime = false;
  try { validRuntime = contentHash((await readRuntime(receipt.runtimePath)).toString("utf8")) === receipt.runtimeHash; } catch {}
  const registered = !!hooks && contentHash(hooks) === receipt.hookHash && validRuntime && !extra;
  return { registered, duplicate: extra, trust: "unknown", detail: registered
    ? "RESTART_KIRO_V3; lifecycle/tool hooks only; host activation and account validation pending"
    : "REGISTRATION_MODIFIED_OR_MISSING" };
}
export const kiroAdapter: CoreAdapter = {
  id: "kiro-cli", version: VERSION, requiresWorker: false,
  async detect() {
    try {
      const { stdout } = await exec("kiro-cli", ["--version"], { timeout: 5000, maxBuffer: 16384 });
      const version = /kiro-cli (\d+\.\d+\.\d+)/.exec(stdout)?.[1];
      return { supported: version === HOST_VERSION, version, reason: version === HOST_VERSION ? undefined : "KIRO_V3_VERSION_UNQUALIFIED" };
    } catch { return { supported: false, reason: "KIRO_NOT_FOUND" }; }
  },
  async install(context) {
    // Register exactly once globally. Project data overrides do not need project executable hooks.
    if (context.scope !== "user") return { registered: false, detail: "USE_USER_HOOKS_AND_PROJECT_CONFIG_OVERRIDE" };
    const host = await this.detect(context);
    if (!host.supported) return { registered: false, hostVersion: host.version, detail: host.reason };
    if (await duplicate(context)) return { registered: false, duplicate: true, detail: "REMOVE_DUPLICATE_FUTUREAGI_HOOKS" };
    const receipt = await readJson<Receipt>(receiptPath(context));
    const target = hookPath(context), old = await readProtected(target);
    if (old !== undefined && (!receipt || receipt.hookPath !== target || receipt.hookHash !== contentHash(old))) throw new Error("HOOK_NOT_OWNED");
    const runtime = runtimePath(context);
    const bytes = await readRuntime(join(__dirname, "hook.cjs"));
    const hash = contentHash(bytes.toString("utf8"));
    await ensurePrivateDirectory(dirname(runtime));
    let previousRuntime: Buffer | undefined;
    try { previousRuntime = await readRuntime(runtime); } catch (e) { if ((e as NodeJS.ErrnoException).code !== "ENOENT") throw e; }
    if (previousRuntime && contentHash(previousRuntime.toString("utf8")) !== hash &&
      (!receipt || receipt.runtimePath !== runtime || contentHash(previousRuntime.toString("utf8")) !== receipt.runtimeHash)) throw new Error("RUNTIME_NOT_OWNED");
    // The bundle is the exact packaged distribution, not source copied from documentation or an npx cache path.
    if (!previousRuntime || contentHash(previousRuntime.toString("utf8")) !== hash) {
      const tmp = runtime + ".install";
      const file = await fs.open(tmp, constants.O_CREAT | constants.O_EXCL | constants.O_WRONLY | constants.O_NOFOLLOW, 0o600);
      try { await file.writeFile(bytes); await file.sync(); } finally { await file.close(); }
      await fs.rename(tmp, runtime);
    }
    const node = await fs.realpath(process.execPath);
    const text = manifest(node, runtime, context.config.paths.configDir, dirname(context.config.paths.user));
    await writeProtected(target, text, old === undefined ? null : contentHash(old));
    await updateJson<Receipt>(receiptPath(context), () => ({ version: 1, hookPath: target,
      hookHash: contentHash(text), runtimePath: runtime, runtimeHash: hash, nodePath: node }));
    return inspect(context);
  },
  inspect,
  async uninstall(context) {
    const receipt = await readJson<Receipt>(receiptPath(context));
    if (!receipt) return { registered: false, detail: "NOT_INSTALLED" };
    if (receipt.hookPath !== hookPath(context) || receipt.runtimePath !== runtimePath(context)) return { registered: true, detail: "OWNERSHIP_REVIEW_REQUIRED" };
    const text = await readProtected(receipt.hookPath);
    if (text !== undefined && contentHash(text) !== receipt.hookHash) return { registered: true, detail: "HOOK_MODIFIED_PRESERVED" };
    if (text !== undefined) await fs.unlink(receipt.hookPath);
    // Retain the inert runtime/config for safe in-flight hooks and a future reinstall. Credentials are shared.
    return { registered: false, activated: false, detail: "HOOK_REMOVED_SHARED_PROFILE_RETAINED" };
  },
};
