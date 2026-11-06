import { execFile, spawn } from "node:child_process";
import { promisify } from "node:util";
import { promises as fs, constants } from "node:fs";
import { join, dirname, delimiter, isAbsolute, relative } from "node:path";
import { homedir } from "node:os";
import { createHash } from "node:crypto";
import {
  ensurePrivateDirectory,
  safeParents,
  writeProtected,
  readJson,
  type CoreAdapter,
  type AdapterContext,
  type AdapterInspection,
} from "@traceai/agent-core";
import { EVENTS } from "./events";
export const VERSION = "0.1.0"; // x-release-please-version
export const HOST_VERSION = "0.167.0";
export const PLUGIN_ID = "futureagi-tracing@futureagi-local";
const exec = promisify(execFile);
async function host(args: string[]): Promise<string> {
  try {
    return (
      await exec("codex", args, {
        timeout: 15000,
        maxBuffer: 1024 * 1024,
        env: process.env,
      })
    ).stdout;
  } catch {
    throw new Error("CODEX_COMMAND_FAILED");
  }
}
export interface HookMetadata {
  key: string;
  pluginId?: string;
  eventName: string;
  currentHash: string;
  trustStatus: string;
  enabled: boolean;
  command?: string;
}
/** Read-only native review state. Neither the installer nor status writes hook trust. */
export async function listHooks(cwd: string): Promise<HookMetadata[]> {
  return new Promise((resolve, reject) => {
    const child = spawn("codex", ["app-server"], {
      stdio: ["pipe", "pipe", "ignore"],
    });
    let data = "",
      done = false;
    const finish = (hooks?: HookMetadata[]) => {
      if (done) return;
      done = true;
      clearTimeout(timer);
      child.stdin.end();
      const stop = setTimeout(() => child.kill(), 1000);
      stop.unref();
      child.once("exit", () => clearTimeout(stop));
      hooks ? resolve(hooks) : reject(new Error("HOOK_REVIEW_UNAVAILABLE"));
    };
    const timer = setTimeout(() => finish(), 8000);
    child.on("error", () => finish());
    child.on("exit", () => finish());
    const send = (id: number, method: string, params: unknown) =>
      child.stdin.write(JSON.stringify({ id, method, params }) + "\n");
    child.stdout.on("data", (chunk) => {
      data += chunk;
      if (data.length > 1024 * 1024) {
        finish();
        return;
      }
      let i;
      while ((i = data.indexOf("\n")) >= 0) {
        const line = data.slice(0, i);
        data = data.slice(i + 1);
        try {
          const m = JSON.parse(line);
          if (m.id === 1) {
            if (m.error) {
              finish();
              return;
            }
            child.stdin.write(
              JSON.stringify({ method: "initialized", params: {} }) + "\n",
            );
            send(2, "hooks/list", { cwds: [cwd] });
          }
          if (m.id === 2) {
            if (m.error) {
              finish();
              return;
            }
            finish((m.result?.data ?? []).flatMap((e: any) => e.hooks ?? []));
          }
        } catch {
          /* ignore non-protocol noise */
        }
      }
    });
    send(1, "initialize", {
      clientInfo: { name: "futureagi-tracing", version: VERSION },
      capabilities: { experimentalApi: true },
    });
  });
}
interface Owner {
  root: string;
  digest: string;
  interpreter?: string;
}
export function supportedHostVersion(version?: string): boolean {
  const match = version?.match(/^0\.(\d+)\.(\d+)$/);
  return !!match && Number(match[1]) >= 167;
}
export async function interpreterPath(
  execPath = process.execPath,
  searchPath = process.env.PATH ?? "",
): Promise<string> {
  const target = await fs.realpath(execPath);
  for (const dir of searchPath.split(delimiter).filter(isAbsolute)) {
    const candidate = join(dir, "node");
    try {
      if ((await fs.realpath(candidate)) === target) {
        await fs.access(candidate, constants.X_OK);
        return candidate;
      }
    } catch {
      /* Try the next stable PATH entry. */
    }
  }
  return execPath;
}
function runtimeBase(): string {
  return join(
    process.env.XDG_DATA_HOME ?? join(homedir(), ".local", "share"),
    "futureagi",
    "agents",
    "runtime",
  );
}
async function pruneRuntime(owner: Owner): Promise<void> {
  const rel = relative(runtimeBase(), owner.root);
  if (!/^\d+\.\d+\.\d+\/codex-[a-f0-9]{16}$/.test(rel)) return;
  await safeParents(owner.root);
  const bytes = await fs.readFile(join(owner.root, "plugin", "hook.cjs"));
  if (createHash("sha256").update(bytes).digest("hex") !== owner.digest)
    throw new Error("RUNTIME_CONTENT_CHANGED");
  await fs.rm(owner.root, { recursive: true });
}
async function runtimeIssue(owner: Owner): Promise<string | undefined> {
  let handle;
  try {
    const rel = relative(runtimeBase(), owner.root);
    if (!/^\d+\.\d+\.\d+\/codex-[a-f0-9]{16}$/.test(rel))
      return "HOOK_RUNTIME_CHANGED";
    await safeParents(join(owner.root, "plugin"));
    handle = await fs.open(
      join(owner.root, "plugin", "hook.cjs"),
      constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK,
    );
    const st = await handle.stat();
    if (
      !st.isFile() ||
      st.nlink !== 1 ||
      st.mode & 0o077 ||
      (process.getuid && st.uid !== process.getuid()) ||
      st.size > 16 * 1024 * 1024
    )
      return "HOOK_RUNTIME_CHANGED";
    const bytes = Buffer.alloc(st.size + 1);
    const { bytesRead } = await handle.read(bytes, 0, bytes.length, 0);
    if (
      bytesRead !== st.size ||
      createHash("sha256")
        .update(bytes.subarray(0, bytesRead))
        .digest("hex") !== owner.digest
    )
      return "HOOK_RUNTIME_CHANGED";
    return undefined;
  } catch (e) {
    return (e as NodeJS.ErrnoException).code === "ENOENT"
      ? "HOOK_RUNTIME_MISSING"
      : "HOOK_RUNTIME_CHANGED";
  } finally {
    await handle?.close();
  }
}
const ownership = (c: AdapterContext) =>
  join(c.config.paths.configDir, "codex-installation.json");
async function inspect(context: AdapterContext): Promise<AdapterInspection> {
  const detected = await codexAdapter.detect(context);
  if (!detected.supported)
    return {
      registered: false,
      hostVersion: detected.version,
      detail: detected.reason,
    };
  const owner = await readJson<Owner>(ownership(context));
  if (!owner) return { registered: false, hostVersion: detected.version };
  const issue = await runtimeIssue(owner);
  if (issue)
    return {
      registered: false,
      hostVersion: detected.version,
      detail: issue,
      recovery:
        issue === "HOOK_RUNTIME_MISSING"
          ? "Run npx @traceai/agents setup codex to restore the hook runtime."
          : "Remove the changed hook.cjs file from the owned Codex runtime, then run npx @traceai/agents setup codex and review /hooks again.",
    };
  if (owner.interpreter) {
    try {
      await fs.access(owner.interpreter, constants.X_OK);
    } catch {
      return {
        registered: false,
        hostVersion: detected.version,
        detail: "NODE_INTERPRETER_MISSING",
      };
    }
  }
  const listing = JSON.parse(
    await host([
      "plugin",
      "list",
      "--marketplace",
      "futureagi-local",
      "--json",
    ]),
  );
  const items = (listing.installed ?? []).filter(
    (p: any) => p.pluginId === PLUGIN_ID,
  );
  const registered =
    items.length === 1 &&
    items[0].enabled === true &&
    items[0].version === VERSION;
  if (!registered)
    return {
      registered: false,
      duplicate: items.length > 1,
      hostVersion: detected.version,
    };
  const hooks = (await listHooks(context.projectDir ?? process.cwd())).filter(
    (h) => h.pluginId === PLUGIN_ID,
  );
  const reviewed =
    hooks.length === EVENTS.length &&
    hooks.every(
      (h) =>
        h.enabled &&
        ["trusted", "managed"].includes(h.trustStatus.toLowerCase()),
    );
  const gap = await readJson<{ code: string }>(
    join(context.config.paths.stateDir, "codex-gap.json"),
  );
  return {
    registered: true,
    hostVersion: detected.version,
    trust: reviewed ? "confirmed" : "required",
    detail: reviewed
      ? gap?.code === "UNKNOWN_HOOK_SHAPE"
        ? gap.code
        : "HOOKS_REVIEWED"
      : "Open Codex /hooks and review Future AGI definitions. Installation never grants trust.",
  };
}
async function immutableFile(path: string, bytes: Buffer): Promise<void> {
  await ensurePrivateDirectory(dirname(path));
  try {
    await fs.writeFile(path, bytes, {
      mode: 0o600,
      flag:
        constants.O_CREAT |
        constants.O_EXCL |
        constants.O_WRONLY |
        constants.O_NOFOLLOW,
    });
  } catch (e) {
    if ((e as NodeJS.ErrnoException).code !== "EEXIST") throw e;
    const st = await fs.lstat(path);
    if (
      !st.isFile() ||
      st.isSymbolicLink() ||
      st.nlink !== 1 ||
      st.mode & 0o077 ||
      st.uid !== process.getuid?.() ||
      !(await fs.readFile(path)).equals(bytes)
    )
      throw new Error("RUNTIME_CONTENT_CHANGED");
  }
}
export const codexAdapter: CoreAdapter = {
  id: "codex",
  version: VERSION,
  requiresWorker: false,
  async detect() {
    if (process.platform === "win32")
      return { supported: false, reason: "WINDOWS_ACL_UNSUPPORTED" };
    try {
      const v = (await host(["--version"]))
        .trim()
        .match(/^codex-cli (\S+)$/)?.[1];
      return {
        supported: supportedHostVersion(v),
        version: v,
        reason: supportedHostVersion(v)
          ? undefined
          : "CODEX_VERSION_NOT_QUALIFIED",
      };
    } catch {
      return { supported: false, reason: "CODEX_NOT_FOUND" };
    }
  },
  async install(context) {
    const detected = await this.detect(context);
    if (!detected.supported)
      return {
        registered: false,
        hostVersion: detected.version,
        detail: detected.reason,
      };
    if (context.scope !== "user")
      return {
        registered: false,
        detail: "Use user installation with a project configuration override.",
      };
    const bytes = await fs.readFile(join(__dirname, "hook.cjs"));
    const digest = createHash("sha256").update(bytes).digest("hex");
    const base =
      process.env.XDG_DATA_HOME ?? join(homedir(), ".local", "share");
    const root = join(
      base,
      "futureagi",
      "agents",
      "runtime",
      VERSION,
      "codex-" + digest.slice(0, 16),
    );
    const plugin = join(root, "plugin");
    await ensurePrivateDirectory(root);
    await ensurePrivateDirectory(plugin);
    await immutableFile(join(plugin, "hook.cjs"), bytes);
    await ensurePrivateDirectory(join(plugin, ".codex-plugin"));
    await ensurePrivateDirectory(join(plugin, "hooks"));
    await ensurePrivateDirectory(join(root, ".agents", "plugins"));
    const interpreter = await interpreterPath();
    const command = [interpreter, join(plugin, "hook.cjs")]
      .map((s) => "'" + s.replaceAll("'", "'\\''") + "'")
      .join(" ");
    const manifest = JSON.parse(
      await fs.readFile(
        join(__dirname, "..", "plugin", ".codex-plugin", "plugin.json"),
        "utf8",
      ),
    );
    if (manifest.name !== "futureagi-tracing" || manifest.version !== VERSION)
      throw new Error("INVALID_PLUGIN_MANIFEST");
    await writeProtected(
      join(plugin, ".codex-plugin", "plugin.json"),
      JSON.stringify(manifest),
    );
    await writeProtected(
      join(plugin, "hooks", "hooks.json"),
      JSON.stringify({
        hooks: Object.fromEntries(
          EVENTS.map((e) => [
            e,
            [
              {
                hooks: [
                  {
                    type: "command",
                    command,
                    timeout: ["SessionEnd", "Interrupt"].includes(e) ? 3 : 10,
                    ...(e === "Stop" ? { async: true } : {}),
                  },
                ],
              },
            ],
          ]),
        ),
      }),
    );
    await writeProtected(
      join(root, ".agents", "plugins", "marketplace.json"),
      JSON.stringify({
        name: "futureagi-local",
        plugins: [
          { name: "futureagi-tracing", source: "./plugin", version: VERSION },
        ],
      }),
    );
    const old = await readJson<Owner>(ownership(context));
    if (old && old.root !== root) {
      const previous = await readJson<{
        name: string;
        plugins: { name: string; source: string }[];
      }>(join(old.root, ".agents", "plugins", "marketplace.json"));
      if (
        previous?.name !== "futureagi-local" ||
        previous.plugins?.length !== 1 ||
        previous.plugins[0].name !== "futureagi-tracing" ||
        previous.plugins[0].source !== "./plugin"
      )
        throw new Error("INSTALLATION_OWNERSHIP_CHANGED");
      await host(["plugin", "remove", PLUGIN_ID, "--json"]);
      await host([
        "plugin",
        "marketplace",
        "remove",
        "futureagi-local",
        "--json",
      ]);
    }
    // Native CLI owns TOML editing and preserves unrelated plugins/comments. No trust or provider edits.
    await host(["plugin", "marketplace", "add", root, "--json"]);
    await host(["plugin", "add", PLUGIN_ID, "--json"]);
    await writeProtected(
      ownership(context),
      JSON.stringify({ root, digest, interpreter }),
    );
    if (old && old.root !== root) await pruneRuntime(old);
    await fs
      .unlink(join(context.config.paths.stateDir, "codex-gap.json"))
      .catch(() => {});
    return inspect(context);
  },
  inspect,
  async uninstall(context) {
    const owner = await readJson<Owner>(ownership(context));
    if (!owner) return { registered: false };
    const marketplace = await readJson<{
      name: string;
      plugins: { name: string; source: string }[];
    }>(join(owner.root, ".agents", "plugins", "marketplace.json"));
    if (
      marketplace?.name !== "futureagi-local" ||
      marketplace.plugins?.length !== 1 ||
      marketplace.plugins[0].name !== "futureagi-tracing" ||
      marketplace.plugins[0].source !== "./plugin"
    )
      throw new Error("INSTALLATION_OWNERSHIP_CHANGED");
    await host(["plugin", "remove", PLUGIN_ID, "--json"]);
    await host([
      "plugin",
      "marketplace",
      "remove",
      "futureagi-local",
      "--json",
    ]);
    await pruneRuntime(owner);
    await fs.unlink(ownership(context));
    return { registered: false, detail: "UNINSTALLED" };
  },
  async run(_context, args) {
    return new Promise((resolve) => {
      const child = spawn("codex", args, { stdio: "inherit" });
      child.on("error", () => resolve(2));
      child.on("exit", (code) => resolve(code ?? 1));
    });
  },
};
