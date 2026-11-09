import { constants, promises as fs } from "node:fs";
import { basename, delimiter, dirname, isAbsolute, join } from "node:path";
import { homedir } from "node:os";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import {
  contentHash,
  ensurePrivateDirectory,
  readJson,
  updateJson,
  type AdapterContext,
  type CoreAdapter,
} from "@traceai/agent-core";
import { EVENTS, TESTED_HOST, VERSION, supportedHost } from "./transcript.js";
const exec = promisify(execFile);
interface CommandHook {
  type: string;
  command?: string;
  timeout?: number;
  [key: string]: unknown;
}
interface HookGroup {
  matcher?: string;
  hooks: CommandHook[];
  [key: string]: unknown;
}
interface Settings {
  hooks?: Record<string, HookGroup[]>;
  env?: Record<string, string>;
  [key: string]: unknown;
}
interface Registration {
  schemaVersion: 1;
  command: string;
  digest: string;
  hostVersion: string;
  runtime: string;
  node: string;
}
const settingsPath = (context: AdapterContext): string =>
  join(dirname(context.config.paths.user), "settings.json");
const registrationPath = (context: AdapterContext): string =>
  join(context.config.paths.configDir, "claude-hooks.json");
const quote = (s: string): string => "'" + s.replace(/'/g, "'\\''") + "'";
function shape(settings: Settings | undefined): void {
  if (!settings) return;
  if (
    settings.hooks !== undefined &&
    (!settings.hooks ||
      typeof settings.hooks !== "object" ||
      Array.isArray(settings.hooks))
  )
    throw new Error("INVALID_HOST_HOOKS");
  for (const groups of Object.values(settings.hooks ?? {})) {
    if (
      !Array.isArray(groups) ||
      groups.some((g) => !g || !Array.isArray(g.hooks))
    )
      throw new Error("INVALID_HOST_HOOKS");
  }
}
export function nativeConflict(
  settings?: Settings,
  env: NodeJS.ProcessEnv = process.env,
): boolean {
  const effective = { ...env, ...settings?.env };
  return [
    "CLAUDE_CODE_ENHANCED_TELEMETRY_BETA",
    "ENABLE_ENHANCED_TELEMETRY_BETA",
    "ENABLE_BETA_TRACING_DETAILED",
  ].some((k) => ["true", "1"].includes(effective[k] ?? ""));
}
export async function detectClaude(): Promise<{
  supported: boolean;
  version?: string;
  reason?: string;
}> {
  try {
    const { stdout } = await exec("claude", ["--version"], {
      timeout: 5000,
      maxBuffer: 4096,
    });
    const version = /^(\d+\.\d+\.\d+)\b/.exec(stdout)?.[1];
    return {
      supported: supportedHost(version),
      version,
      reason: supportedHost(version)
        ? undefined
        : "UNQUALIFIED_TRANSCRIPT_SCHEMA",
    };
  } catch {
    return { supported: false, reason: "CLAUDE_NOT_FOUND" };
  }
}
async function persistRuntime(
  context: AdapterContext,
): Promise<{ runtime: string; digest: string }> {
  const bundle = await fs.readFile(join(__dirname, "hook.cjs"));
  if (bundle.length > 20 * 1024 * 1024) throw new Error("RUNTIME_LIMIT");
  const digest = contentHash(bundle.toString("utf8"));
  const data = process.env.XDG_DATA_HOME || join(homedir(), ".local", "share");
  const directory = join(
    data,
    "futureagi",
    "agents",
    "runtime",
    `claude-${VERSION}-${digest.slice(0, 16)}`,
  );
  await ensurePrivateDirectory(directory);
  const runtime = join(directory, "hook.cjs");
  let handle;
  try {
    handle = await fs.open(
      runtime,
      constants.O_CREAT |
        constants.O_EXCL |
        constants.O_WRONLY |
        constants.O_NOFOLLOW,
      0o600,
    );
    await handle.writeFile(bundle);
    await handle.sync();
  } catch (e) {
    if ((e as NodeJS.ErrnoException).code !== "EEXIST") throw e;
    const existing = await fs.open(
      runtime,
      constants.O_RDONLY | constants.O_NOFOLLOW,
    );
    try {
      const st = await existing.stat();
      if (
        !st.isFile() ||
        st.nlink !== 1 ||
        st.size !== bundle.length ||
        st.mode & 0o077 ||
        (process.getuid && st.uid !== process.getuid()) ||
        contentHash(await existing.readFile("utf8")) !== digest
      )
        throw new Error("RUNTIME_CHANGED");
    } finally {
      await existing.close();
    }
  } finally {
    await handle?.close();
  }
  return { runtime, digest };
}
/** Prefer a stable PATH symlink (Homebrew/nvm aliases) without copying Node. */
export async function interpreterPath(): Promise<string> {
  const executable = await fs.realpath(process.execPath);
  for (const directory of (process.env.PATH ?? "").split(delimiter)) {
    if (!isAbsolute(directory)) continue;
    const candidate = join(directory, "node");
    try {
      await fs.access(candidate, constants.X_OK);
      if ((await fs.realpath(candidate)) === executable) return candidate;
    } catch {
      /* Try another PATH entry. */
    }
  }
  return process.execPath;
}
async function pruneRuntime(registration?: Registration): Promise<void> {
  if (!registration || basename(registration.runtime) !== "hook.cjs") return;
  const directory = dirname(registration.runtime);
  if (!/^claude-\d+\.\d+\.\d+-[a-f0-9]{16}$/.test(basename(directory))) return;
  try {
    const st = await fs.lstat(directory);
    if (
      !st.isDirectory() ||
      st.isSymbolicLink() ||
      st.mode & 0o077 ||
      (process.getuid && st.uid !== process.getuid())
    )
      return;
    const file = await fs.lstat(registration.runtime);
    if (
      !file.isFile() ||
      file.isSymbolicLink() ||
      file.nlink !== 1 ||
      (process.getuid && file.uid !== process.getuid())
    )
      return;
    if (
      contentHash(await fs.readFile(registration.runtime, "utf8")) !==
      registration.digest
    )
      return;
    await fs.unlink(registration.runtime);
    await fs.rmdir(directory); // Nonempty directories are deliberately retained.
  } catch {
    /* Cleanup must not remove user edits or break registration. */
  }
}
export async function installHooks(context: AdapterContext): Promise<void> {
  const old = await readJson<Registration>(registrationPath(context));
  const settings = await readJson<Settings>(settingsPath(context), false);
  shape(settings);
  if (nativeConflict(settings)) throw new Error("NATIVE_TRACING_CONFLICT");
  const runtime = await persistRuntime(context);
  const node = await interpreterPath();
  const command = `${quote(node)} ${quote(runtime.runtime)}`;
  await updateJson<Settings>(
    settingsPath(context),
    (current) => {
      shape(current);
      if (nativeConflict(current)) throw new Error("NATIVE_TRACING_CONFLICT");
      const hooks = { ...current?.hooks };
      for (const event of EVENTS) {
        const groups = hooks[event] ?? [];
        // A previously installed exact command is ours; anything else remains unchanged.
        const retained = groups
          .map((g) => ({
            ...g,
            hooks: g.hooks.filter(
              (h) =>
                !(h.type === "command" && old && h.command === old.command),
            ),
          }))
          .filter((g) => g.hooks.length);
        if (
          retained.some((g) =>
            g.hooks.some(
              (h) =>
                h.command === command ||
                h.command?.includes("futureagi/agents/runtime/claude-"),
            ),
          )
        )
          throw new Error("DUPLICATE_REGISTRATION");
        hooks[event] = [
          ...retained,
          { hooks: [{ type: "command", command, timeout: 10 }] },
        ];
      }
      return { ...current, hooks };
    },
    false,
  );
  await updateJson<Registration>(registrationPath(context), () => ({
    schemaVersion: 1,
    ...runtime,
    node,
    command,
    hostVersion: TESTED_HOST,
  }));
  if (old?.runtime !== runtime.runtime) await pruneRuntime(old);
}
export async function inspectHooks(context: AdapterContext) {
  const registration = await readJson<Registration>(registrationPath(context));
  const settings = await readJson<Settings>(settingsPath(context), false);
  shape(settings);
  const host = await detectClaude();
  let duplicate = false;
  const registered =
    !!registration &&
    registration.schemaVersion === 1 &&
    host.supported &&
    settings?.disableAllHooks !== true &&
    !nativeConflict(settings) &&
    EVENTS.every((event) => {
      const count = (settings?.hooks?.[event] ?? [])
        .flatMap((g) => g.hooks)
        .filter(
          (h) => h.type === "command" && h.command === registration.command,
        ).length;
      if (count > 1) duplicate = true;
      return count === 1;
    });
  let runtimeIntact = false;
  if (registered && registration) {
    try {
      await fs.access(registration.node, constants.X_OK);
      const file = await fs.open(
        registration.runtime,
        constants.O_RDONLY | constants.O_NOFOLLOW,
      );
      try {
        const st = await file.stat();
        runtimeIntact =
          st.isFile() &&
          st.nlink === 1 &&
          st.size < 20 * 1024 * 1024 &&
          !(st.mode & 0o077) &&
          (!process.getuid || st.uid === process.getuid()) &&
          contentHash(await file.readFile("utf8")) === registration.digest;
      } finally {
        await file.close();
      }
    } catch {}
  }
  return {
    registered: registered && runtimeIntact,
    duplicate,
    hostVersion: host.version,
    trust: "unknown" as const,
    detail:
      settings?.disableAllHooks === true
        ? "HOST_HOOKS_DISABLED"
        : nativeConflict(settings)
          ? "NATIVE_TRACING_CONFLICT"
          : (host.reason ??
            (registered && !runtimeIntact
              ? "RUNTIME_MISSING_OR_CHANGED"
              : "USER_HOOKS_TRANSCRIPT_MODE")),
  };
}
export async function uninstallHooks(context: AdapterContext): Promise<void> {
  const registration = await readJson<Registration>(registrationPath(context));
  if (!registration) return;
  await updateJson<Settings>(
    settingsPath(context),
    (current) => {
      shape(current);
      if (!current) return {};
      const hooks = { ...current.hooks };
      for (const event of EVENTS) {
        hooks[event] = (hooks[event] ?? [])
          .map((g) => ({
            ...g,
            hooks: g.hooks.filter(
              (h) =>
                !(h.type === "command" && h.command === registration.command),
            ),
          }))
          .filter((g) => g.hooks.length);
        if (!hooks[event].length) delete hooks[event];
      }
      return { ...current, hooks };
    },
    false,
  );
  await pruneRuntime(registration);
  await fs.unlink(registrationPath(context));
}
export const claudeAdapter: CoreAdapter = {
  id: "claude",
  version: VERSION,
  requiresWorker: false,
  diagnostics: {
    HOST_HOOKS_DISABLED:
      "Claude Code disableAllHooks is enabled. Review that host setting and re-enable hooks to resume tracing; setup does not change it.",
  },
  detect: detectClaude,
  async install(context) {
    await installHooks(context);
    return inspectHooks(context);
  },
  inspect: inspectHooks,
  async uninstall(context) {
    await uninstallHooks(context);
    return { registered: false };
  },
};
