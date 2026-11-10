import { promises as fs } from "node:fs";
import { dirname, isAbsolute, join, resolve } from "node:path";
import { randomBytes } from "node:crypto";
import { contentHash, ensurePrivateDirectory, readJson, readProtected, updateJson, writeProtected, removeProtected,
  type AdapterContext, type AdapterInspection } from "@traceai/agent-core";
import { IDE_EVENTS } from "./events.js";

export const IDE_VERSION = "0.1.0";
export const IDE_HOLD = "KIRO_IDE_GUI_AND_PAYLOAD_QUALIFICATION_REQUIRED";
const MARKER = "fi-kiro-ide-v1";
const quote = (s: string): string => `'${s.replace(/'/g, `'\\''`)}'`;
interface Receipt { schema: 1; file: string; hashes: string[]; runtime: string }
function location(c: AdapterContext): { file: string; receipt: string } {
  if (!c.projectDir || !c.config.paths.projectBinding || !c.config.paths.project) throw new Error("KIRO_IDE_PROJECT_DIRECTORY_REQUIRED");
  const file = join(dirname(c.config.paths.project), "hooks", "futureagi-ide.json");
  return { file, receipt: join(c.config.paths.configDir, `kiro-ide-${c.config.paths.projectBinding}.json`) };
}
export function ideManifest(runtime: string, configDir: string, projectDir: string): string {
  for (const p of [runtime, configDir, projectDir])
    if (!isAbsolute(p) || /[\r\n\0]/.test(p)) throw new Error("INVALID_HOOK_PATH");
  return JSON.stringify({ version: "v1", hooks: IDE_EVENTS.map(trigger => ({
    name: `Future AGI IDE ${trigger}`, trigger, enabled: true, timeout: trigger === "Stop" ? 8 : 2,
    action: { type: "command", command: [join(runtime, "node"), join(runtime, "ide-hook.cjs"),
      "--marker", MARKER, "--config-dir", configDir, "--project-dir", projectDir, "--event", trigger]
      .map(quote).join(" ") + " >/dev/null 2>&1 || :" },
  })) }, null, 2) + "\n";
}
/** Private immutable distribution copy, not a customer-maintained script or ephemeral npx path. */
export async function installIdeRuntime(c: AdapterContext): Promise<string> {
  const source = resolve(__dirname, "..", "ide-hook.cjs");
  const bundle = await fs.readFile(source);
  if (bundle.length > 16 * 1024 * 1024) throw new Error("RUNTIME_SIZE_LIMIT");
  // Do not copy Node: macOS scans a newly copied executable on its first invocation.
  // Prefer an existing stable PATH alias that resolves to the interpreter running setup.
  let node = process.execPath;
  const realNode = await fs.realpath(process.execPath);
  for (const dir of (process.env.PATH ?? "").split(":").filter(isAbsolute)) {
    const candidate = join(dir, "node");
    if (!candidate.includes("/_npx/") && await fs.realpath(candidate).catch(() => undefined) === realNode) {
      node = candidate; break;
    }
  }
  if (node.includes("/_npx/")) throw new Error("EPHEMERAL_NODE_RUNTIME");
  const digest = contentHash(bundle.toString() + node).slice(0, 24);
  const home = dirname(dirname(c.config.paths.user));
  const root = join(process.env.XDG_DATA_HOME || join(home, ".local", "share"), "futureagi", "agents", "runtime");
  await ensurePrivateDirectory(root);
  const target = join(root, `kiro-ide-${IDE_VERSION}-${digest}`);
  try {
    const st = await fs.lstat(target);
    if (!st.isDirectory() || st.isSymbolicLink() || st.mode & 0o077 || (process.getuid && st.uid !== process.getuid())) throw new Error("UNSAFE_RUNTIME");
    for (const [name, bytes] of [["ide-hook.cjs", bundle]] as const) {
      const path = join(target, name), stat = await fs.lstat(path);
      if (!stat.isFile() || stat.isSymbolicLink() || stat.nlink !== 1 || stat.mode & 0o077 ||
        (process.getuid && stat.uid !== process.getuid()) || !(await fs.readFile(path)).equals(bytes)) throw new Error("RUNTIME_CHANGED");
    }
    if (await fs.readlink(join(target, "node")) !== node || await fs.realpath(join(target, "node")) !== realNode)
      throw new Error("RUNTIME_CHANGED");
    return target;
  } catch (e) { if ((e as NodeJS.ErrnoException).code !== "ENOENT") throw e; }
  const temporary = target + "." + randomBytes(10).toString("hex");
  await fs.mkdir(temporary, { mode: 0o700 });
  try {
    await fs.symlink(node, join(temporary, "node"));
    await fs.writeFile(join(temporary, "ide-hook.cjs"), bundle, { mode: 0o600, flag: "wx" });
    const legal = await fs.readFile(source + ".LEGAL.txt").catch(() => undefined);
    if (legal) await fs.writeFile(join(temporary, "ide-hook.cjs.LEGAL.txt"), legal, { mode: 0o600, flag: "wx" });
    await fs.rename(temporary, target);
    return target;
  } finally { await fs.rm(temporary, { force: true, recursive: true }); }
}
async function duplicates(c: AdapterContext, file: string): Promise<boolean> {
  const dirs = [dirname(file), join(dirname(c.config.paths.user), "hooks")];
  for (const dir of new Set(dirs)) {
    const names = await fs.readdir(dir).catch((e: NodeJS.ErrnoException) => { if (e.code === "ENOENT") return []; throw e; });
    if (names.length > 256) throw new Error("HOOK_DIRECTORY_LIMIT");
    for (const name of names) {
      const path = join(dir, name);
      if (path === file || !/\.(json|kiro\.hook)$/.test(name)) continue;
      const raw = await readProtected(path, false);
      if (raw && /fi-kiro|fi-hook-to-otlp|@traceai\/kiro-plugin|futureagi/i.test(raw)) return true;
    }
  }
  return false;
}
/** Internal installer is contract-tested, but adapter.install remains held until GUI qualification. */
export async function registerIde(c: AdapterContext, runtime: string): Promise<AdapterInspection> {
  const { file, receipt } = location(c);
  const before = await readProtected(file), old = await readJson<Receipt>(receipt);
  if (old && (old.schema !== 1 || old.file !== file || !Array.isArray(old.hashes))) throw new Error("OWNERSHIP_CONFLICT");
  if (before !== undefined && !old?.hashes.includes(contentHash(before))) throw new Error("OWNED_HOOK_MODIFIED");
  if (await duplicates(c, file)) throw new Error("DUPLICATE_KIRO_REGISTRATION");
  const text = ideManifest(runtime, c.config.paths.configDir, dirname(dirname(c.config.paths.project!)));
  const hashes = [...new Set([...(old?.hashes ?? []), contentHash(text)])];
  if (hashes.length > 8) throw new Error("OWNERSHIP_LIMIT");
  // Journal exact ownership first, so a partial failure can be retried without adopting foreign hooks.
  await updateJson<Receipt>(receipt, previous => {
    if (JSON.stringify(previous) !== JSON.stringify(old)) throw new Error("CONCURRENT_SAVE");
    return { schema: 1, file, hashes, runtime };
  });
  if (text !== before) await writeProtected(file, text, before === undefined ? null : contentHash(before));
  return inspectIde(c);
}
export async function inspectIde(c: AdapterContext): Promise<AdapterInspection> {
  const base: AdapterInspection = { registered: false, activated: false, trust: "required", detail: IDE_HOLD };
  if (!c.projectDir || !c.config.paths.project) return { ...base, detail: "KIRO_IDE_PROJECT_DIRECTORY_REQUIRED" };
  const { file, receipt } = location(c);
  const saved = await readJson<Receipt>(receipt), raw = await readProtected(file);
  if (!saved || saved.schema !== 1 || saved.file !== file || !raw || !saved.hashes.includes(contentHash(raw))) return base;
  const duplicate = await duplicates(c, file);
  const current = ideManifest(saved.runtime, c.config.paths.configDir, dirname(dirname(c.config.paths.project)));
  return { ...base, registered: !duplicate && raw === current, duplicate };
}
export async function unregisterIde(c: AdapterContext): Promise<AdapterInspection> {
  const { file, receipt } = location(c), saved = await readJson<Receipt>(receipt);
  const before = await readProtected(file);
  if (before === undefined) return { registered: false, detail: IDE_HOLD };
  if (!saved || saved.schema !== 1 || saved.file !== file || !saved.hashes.includes(contentHash(before))) throw new Error("OWNED_HOOK_MODIFIED");
  await removeProtected(file, contentHash(before));
  // Keep private ownership ledger for idempotent reinstall. No unrelated hooks or permissions touched.
  return { registered: false, activated: false, detail: IDE_HOLD };
}
