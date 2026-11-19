import { constants, promises as fs } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { createHash, randomBytes } from "node:crypto";
import { parse, modify, applyEdits, type ParseError } from "jsonc-parser";
import {
  contentHash, readProtected, writeProtected, readJson, ensurePrivateDirectory,
  safeParents, type CoreAdapter, type AdapterContext, type AdapterInspection,
} from "@traceai/agent-core";

export const VERSION = "0.1.0"; // x-release-please-version
export const HOST_VERSION = "0.37.0";
export const EVENTS = ["SessionStart", "PostToolUse", "Stop", "SessionEnd"] as const;
const exec = promisify(execFile);
type Group = { hooks: Array<{ type: string; command: string; timeout?: number }>; metadata?: Record<string, boolean>; matcher?: string };
type Settings = { hooks?: Record<string, Group[]>; [key: string]: unknown };
interface Ownership { settings: string; command: string; groups: Record<string, Group>; files: Record<string, string> }
export interface Registration { agent?: "augment" | "augment-ide"; agentDir: string; configDir: string; stateHome: string }

/** Match Auggie's direct-spawn / word-split / shell branches; do not pass raw paths to a shell. */
export function scriptCommand(path: string): string {
  if (!path.startsWith("/") || /[\x00-\x1f]/.test(path)) throw new Error("UNSAFE_RUNTIME_PATH");
  if (/[|&;><$`]/.test(path)) return "'" + path.replace(/'/g, "'\\''") + "'";
  // Force Auggie's word splitter even for quoted filenames without spaces.
  if (!/^[a-zA-Z0-9_./:-]+$/.test(path)) {
    if (path.includes('"') && path.includes("'")) throw new Error("UNSUPPORTED_RUNTIME_PATH_QUOTES");
    return (path.includes('"') ? "'" + path + "'" : '"' + path + '"') + " ";
  }
  return path;
}
function location(c: AdapterContext): { settings: string; runtime: string; owned: string } {
  if (!["augment", "augment-ide"].includes(c.config.agent)) throw new Error("INVALID_AUGMENT_AGENT");
  const settings = c.scope === "project"
    ? join(c.projectDir ?? (() => { throw new Error("PROJECT_DIRECTORY_REQUIRED"); })(), ".augment", "settings.local.json")
    : join(dirname(c.config.paths.user), "settings.json");
  // Stable distribution outside the customer's repository and npm's ephemeral cache.
  const binding = contentHash(settings).slice(0, 20);
  const runtime = join(c.config.paths.configDir, "runtime", c.config.agent, VERSION, binding);
  return { settings, runtime, owned: join(c.config.paths.configDir, "registrations", c.config.agent, `${binding}.json`) };
}
function settingsDocument(text: string | undefined): Settings {
  const errors: ParseError[] = [];
  const value = parse(text ?? "{}", errors, { allowTrailingComma: true });
  if (errors.length || !value || typeof value !== "object" || Array.isArray(value)) throw new Error("INVALID_AUGMENT_SETTINGS");
  if (value.hooks !== undefined && (!value.hooks || typeof value.hooks !== "object" || Array.isArray(value.hooks))) throw new Error("INVALID_AUGMENT_HOOKS");
  for (const groups of Object.values(value.hooks ?? {})) {
    if (!Array.isArray(groups) || groups.some((g: Group) => !g || !Array.isArray(g.hooks))) throw new Error("INVALID_AUGMENT_HOOKS");
  }
  return value;
}
function groups(command: string, capture: boolean): Record<string, Group> {
  return Object.fromEntries(EVENTS.map((event) => [event, {
    ...(event === "PostToolUse" ? { matcher: ".*" } : {}),
    hooks: [{ type: "command", command, timeout: 7000 }], // Auggie uses MILLISECONDS.
    metadata: { includeConversationData: event === "Stop" && capture, includeUserContext: false, includeMCPMetadata: false },
  }]));
}
const same = (a: unknown, b: unknown): boolean => JSON.stringify(a) === JSON.stringify(b);
function managed(g: Group): boolean {
  return g.hooks.some((h) => typeof h.command === "string" && /futureagi-hook\.(sh|ps1|cmd)/.test(h.command));
}
async function reconcile(path: string, previous: Ownership | undefined, next?: Record<string, Group>): Promise<void> {
  for (let attempt = 0; attempt < 3; attempt++) {
    const raw = await readProtected(path, false);
    const doc = settingsDocument(raw);
    let text = raw ?? "{}\n";
    for (const event of EVENTS) {
      const entries = doc.hooks?.[event] ?? [];
      const owned = previous?.groups[event];
      if (entries.some((g) => managed(g) && !same(g, owned))) throw new Error("AUGMENT_HOOK_CONFLICT");
      const options = { formattingOptions: { insertSpaces: true, tabSize: 2, eol: "\n" } };
      // Edit only owned array members, retaining comments in unrelated entries.
      for (let index = entries.length - 1; index >= 0; index--) {
        if (owned && same(entries[index], owned))
          text = applyEdits(text, modify(text, ["hooks", event, index], undefined, options));
      }
      if (next) {
        const remaining = settingsDocument(text).hooks?.[event];
        text = applyEdits(text, remaining
          ? modify(text, ["hooks", event, -1], next[event], { ...options, isArrayInsertion: true })
          : modify(text, ["hooks", event], [next[event]], options));
      }
    }
    try {
      await writeProtected(path, text, raw === undefined ? null : contentHash(raw), false);
      return;
    } catch (e) { if ((e as Error).message !== "CONCURRENT_SAVE" || attempt === 2) throw e; }
  }
}
async function noOtherScope(c: AdapterContext, target: string): Promise<void> {
  const candidates = [join(dirname(c.config.paths.user), "settings.json")];
  if (c.projectDir) candidates.push(join(c.projectDir, ".augment", "settings.json"), join(c.projectDir, ".augment", "settings.local.json"));
  for (const path of candidates) {
    if (resolve(path) === resolve(target)) continue;
    const doc = settingsDocument(await readProtected(path, false));
    if (Object.values(doc.hooks ?? {}).some((items) => items.some(managed))) throw new Error("DUPLICATE_AUGMENT_REGISTRATION");
  }
}
async function installRuntime(c: AdapterContext, runtime: string): Promise<Record<string, string>> {
  await ensurePrivateDirectory(runtime);
  const source = resolve(__dirname, "..");
  const hashes: Record<string, string> = {};
  for (const relative of ["runtime/hook.cjs", "bin/futureagi-hook.sh", "bin/futureagi-hook.ps1", "bin/futureagi-hook.cmd"]) {
    const data = await fs.readFile(join(source, relative));
    if (data.length > 16 * 1024 * 1024) throw new Error("OVERSIZE_RUNTIME");
    const to = join(runtime, relative);
    await ensurePrivateDirectory(dirname(to));
    const tmp = to + "." + randomBytes(12).toString("hex");
    const handle = await fs.open(tmp, constants.O_CREAT | constants.O_EXCL | constants.O_WRONLY | constants.O_NOFOLLOW, relative.endsWith(".sh") ? 0o700 : 0o600);
    try { await handle.writeFile(data); } finally { await handle.close(); }
    // Do not follow a substituted link on update.
    const existing = await fs.lstat(to).catch((e: NodeJS.ErrnoException) => { if (e.code !== "ENOENT") throw e; return undefined; });
    if (existing && (!existing.isFile() || existing.nlink !== 1 || existing.uid !== process.getuid?.())) { await fs.unlink(tmp); throw new Error("UNSAFE_RUNTIME"); }
    await fs.rename(tmp, to);
    hashes[relative] = createHash("sha256").update(data).digest("hex");
  }
  await writeProtected(join(runtime, "bin", "node-path"), process.execPath + "\n");
  const registration: Registration = {
    agent: c.config.agent as Registration["agent"],
    agentDir: dirname(c.config.paths.user), configDir: c.config.paths.configDir,
    stateHome: dirname(dirname(c.config.paths.stateDir)),
  };
  await writeProtected(join(runtime, "bin", "registration.json"), JSON.stringify(registration));
  for (const rel of ["bin/node-path", "bin/registration.json"])
    hashes[rel] = contentHash((await readProtected(join(runtime, rel)))!);
  return hashes;
}
async function host(): Promise<{ supported: boolean; version?: string; reason?: string }> {
  if (process.platform === "win32") return { supported: false, reason: "WINDOWS_ACL_NOT_QUALIFIED" };
  try {
    const result = await exec("auggie", ["--version"], { timeout: 5000, maxBuffer: 4096, env: { ...process.env, AUGMENT_DISABLE_AUTO_UPDATE: "1" } });
    const version = result.stdout.match(/\b(\d+\.\d+\.\d+)\b/)?.[1];
    return { supported: version === HOST_VERSION, version, reason: version === HOST_VERSION ? undefined : "AUGGIE_VERSION_NOT_QUALIFIED" };
  } catch { return { supported: false, reason: "AUGGIE_NOT_FOUND" }; }
}
/** Shared native hook plumbing. Surface adapters own host qualification, never the mapper. */
export const augmentHookRegistration: Pick<CoreAdapter, "install" | "inspect" | "uninstall"> = {
  async install(c) {
    const p = location(c);
    await noOtherScope(c, p.settings);
    const previous = await readJson<Ownership>(p.owned);
    // Validate ownership before changing even the package-owned runtime.
    const doc = settingsDocument(await readProtected(p.settings, false));
    for (const event of EVENTS) {
      if ((doc.hooks?.[event] ?? []).some((g) => managed(g) && !same(g, previous?.groups[event]))) throw new Error("AUGMENT_HOOK_CONFLICT");
    }
    const files = await installRuntime(c, p.runtime);
    const command = scriptCommand(join(p.runtime, "bin", "futureagi-hook.sh"));
    const next = groups(command, c.config.captureContent);
    await reconcile(p.settings, previous, next);
    await writeProtected(p.owned, JSON.stringify({ settings: p.settings, command, groups: next, files } satisfies Ownership));
    return this.inspect(c);
  },
  async inspect(c): Promise<AdapterInspection> {
    try {
      const p = location(c), owner = await readJson<Ownership>(p.owned);
      const doc = settingsDocument(await readProtected(p.settings, false));
      await noOtherScope(c, p.settings);
      const registered = !!owner && EVENTS.every((e) => (doc.hooks?.[e] ?? []).filter((g) => same(g, owner.groups[e])).length === 1);
      if (owner) for (const [rel, hash] of Object.entries(owner.files)) {
        if (!/^(bin|runtime)\/[a-z0-9.-]+$/.test(rel)) throw new Error("UNSAFE_RUNTIME_MANIFEST");
        const path = join(p.runtime, rel);
        await safeParents(dirname(path));
        const h = await fs.open(path, constants.O_RDONLY | constants.O_NOFOLLOW);
        try {
          const s = await h.stat();
          if (!s.isFile() || s.nlink !== 1 || s.mode & 0o077 || s.size > 16 * 1024 * 1024 || s.uid !== process.getuid?.() || createHash("sha256").update(await h.readFile()).digest("hex") !== hash) throw new Error("RUNTIME_CHANGED");
        } finally { await h.close(); }
      }
      return { registered, trust: "unknown", detail: "Shared hook registration only; it does not identify the originating host or establish editor activation." };
    } catch (e) {
      return { registered: false, duplicate: (e as Error).message === "DUPLICATE_AUGMENT_REGISTRATION", detail: "AUGMENT_REGISTRATION_NEEDS_ATTENTION" };
    }
  },
  async uninstall(c) {
    const p = location(c), owner = await readJson<Ownership>(p.owned);
    if (owner) {
      await reconcile(p.settings, owner);
      await fs.unlink(p.owned);
    }
    return { registered: false, detail: "Owned hooks removed; credentials and unrelated hooks preserved." };
  },
};

export const augmentAdapter: CoreAdapter = {
  id: "augment", version: VERSION, requiresWorker: false, detect: host,
  async install(context) {
    const detected = await host();
    if (!detected.supported) return { registered: false, hostVersion: detected.version, detail: detected.reason };
    await augmentHookRegistration.install(context);
    return this.inspect(context);
  },
  async inspect(context) {
    const shared = await augmentHookRegistration.inspect(context), detected = await host();
    return { ...shared, registered: shared.registered && detected.supported, hostVersion: detected.version,
      detail: "Registration only; real authenticated Auggie execution and product readback are not yet qualified. Stop-summary/tool coverage, not model requests or token/cost accounting." };
  },
  uninstall: (context) => augmentHookRegistration.uninstall(context),
};
