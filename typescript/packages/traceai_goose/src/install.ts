import { constants, promises as fs } from "node:fs";
import { join, isAbsolute } from "node:path";
import { execFile, spawn } from "node:child_process";
import { promisify } from "node:util";
import {
  contentHash,
  ensurePrivateDirectory,
  readJson,
  readProtected,
  readSettings,
  updateJson,
  writeProtected,
  type AdapterContext,
  type CoreAdapter,
} from "@traceai/agent-core";
import {
  EVENTS,
  TESTED_HOST,
  VERSION,
  goosePaths,
  nativeConflict,
} from "./paths.js";

import {
  registrationPath,
  registrationApplies,
  type Registration,
} from "./registration.js";

const exec = promisify(execFile);
const quote = (s: string) => "'" + s.replace(/'/g, "'\\''") + "'";
export function supportedGooseVersion(version: string | undefined) {
  const match = /^1\.(\d+)\.\d+$/.exec(version ?? "");
  return !!match && Number(match[1]) >= 54;
}
/** Keep a stable PATH symlink when it points at the interpreter running setup.
 * Never copy Node: the first execution of a new copy is slow on macOS. */
export async function interpreterPath(
  execPath = process.execPath,
  search = process.env.PATH ?? "",
) {
  const actual = await fs.realpath(execPath);
  let fallback = execPath;
  for (const dir of search.split(":")) {
    if (!isAbsolute(dir)) continue;
    const candidate = join(dir, "node");
    try {
      if ((await fs.realpath(candidate)) === actual) {
        await fs.access(candidate, constants.X_OK);
        if (candidate !== actual && !candidate.includes("/_npx/"))
          return candidate;
        fallback = candidate;
      }
    } catch {
      /* not this interpreter */
    }
  }
  return fallback;
}
export async function detectGoose() {
  try {
    if (!["linux", "darwin"].includes(process.platform))
      return { supported: false, reason: "UNSUPPORTED_PLATFORM" };
    const { stdout } = await exec("goose", ["--version"], {
      timeout: 3000,
      maxBuffer: 4096,
    });
    const version = /\b(\d+\.\d+\.\d+)\b/.exec(stdout)?.[1];
    return {
      supported: supportedGooseVersion(version),
      version,
      reason: !supportedGooseVersion(version)
        ? "UNQUALIFIED_GOOSE_SCHEMA"
        : version === TESTED_HOST
          ? undefined
          : "COMPATIBLE_GOOSE_VERSION",
    };
  } catch {
    return { supported: false, reason: "GOOSE_NOT_FOUND" };
  }
}
async function runtime(c: AdapterContext) {
  const bytes = await fs.readFile(join(__dirname, "hook.cjs"));
  if (bytes.length > 16 * 1024 * 1024) throw new Error("RUNTIME_LIMIT");
  const digest = contentHash(bytes.toString("utf8"));
  const dir = join(
    c.config.paths.configDir,
    "runtime",
    `goose-${VERSION}-${digest.slice(0, 16)}`,
  );
  await ensurePrivateDirectory(dir);
  const path = join(dir, "hook.cjs");
  let handle;
  try {
    handle = await fs.open(
      path,
      constants.O_WRONLY |
        constants.O_CREAT |
        constants.O_EXCL |
        constants.O_NOFOLLOW,
      0o600,
    );
    await handle.writeFile(bytes);
    await handle.sync();
  } catch (e) {
    if ((e as NodeJS.ErrnoException).code !== "EEXIST") throw e;
    if (!(await runtimeIntact(path, digest)))
      throw new Error("RUNTIME_CHANGED");
  } finally {
    await handle?.close();
  }
  return { runtime: path, digest };
}
async function runtimeIntact(path: string, digest: string) {
  const fd = await fs.open(path, constants.O_RDONLY | constants.O_NOFOLLOW);
  try {
    const st = await fd.stat();
    return (
      st.isFile() &&
      st.nlink === 1 &&
      !(st.mode & 0o077) &&
      st.size <= 16 * 1024 * 1024 &&
      (!process.getuid || st.uid === process.getuid()) &&
      contentHash(await fd.readFile("utf8")).startsWith(digest)
    );
  } finally {
    await fd.close();
  }
}
export async function pruneRuntimes(configDir: string, keep?: string) {
  const root = join(configDir, "runtime");
  for (const name of await fs.readdir(root).catch(() => [] as string[])) {
    const match = /^goose-[0-9]+\.[0-9]+\.[0-9]+-([a-f0-9]{16})$/.exec(name);
    if (!match) continue;
    const dir = join(root, name),
      file = join(dir, "hook.cjs");
    if (file === keep) continue;
    try {
      await ensurePrivateDirectory(dir);
      // Never remove modified or unrelated runtime contents.
      if (!(await runtimeIntact(file, match[1]))) continue;
      if ((await fs.readdir(dir)).some((entry) => entry !== "hook.cjs"))
        continue;
      await fs.unlink(file);
      await fs.rmdir(dir);
    } catch {
      /* a live update or modified file is not ours to delete */
    }
  }
}
async function disabledByHost(projectDir = process.cwd()) {
  for (const path of [
    join(goosePaths().config, "settings.json"),
    join(projectDir, ".config", "goose", "settings.json"),
  ]) {
    try {
      const settings = await readJson<{ disabledPlugins?: string[] }>(
        path,
        false,
      );
      if (settings?.disabledPlugins?.includes("futureagi-goose")) return true;
    } catch (error) {
      // Project settings on unsafe ancestors are ignored, just like the core
      // project layer. User settings remain protected and fail closed.
      if (
        path === join(goosePaths().config, "settings.json") ||
        !(error instanceof Error) ||
        !error.message.startsWith("UNSAFE_")
      )
        throw error;
    }
  }
  return false;
}
export async function inspectGoose(c: AdapterContext) {
  const host = await detectGoose();
  const old = await readJson<Registration>(registrationPath(c.config));
  const plugin = goosePaths().plugin;
  const hooks = await readProtected(join(plugin, "hooks", "hooks.json"));
  const manifest = await readProtected(join(plugin, "plugin.json"));
  const conflict = await nativeConflict();
  const disabled = await disabledByHost(c.projectDir);
  let intact = false;
  try {
    if (old) {
      await fs.access(old.node, constants.X_OK);
      intact = await runtimeIntact(old.runtime, old.digest);
    }
  } catch {
    /* report unhealthy, never execute it */
  }
  return {
    registered:
      !!old &&
      registrationApplies(old, c.config) &&
      intact &&
      host.supported &&
      !conflict &&
      !disabled &&
      hooks === old.hooks &&
      manifest === old.manifest,
    hostVersion: host.version,
    trust: "confirmed" as const,
    detail: conflict
      ? "NATIVE_TRACING_CONFLICT"
      : disabled
        ? "PLUGIN_DISABLED_BY_GOOSE"
        : !registrationApplies(old, c.config)
          ? "PROJECT_NOT_REGISTERED"
          : old && !intact
            ? "RUNTIME_UNAVAILABLE"
            : (host.reason ?? "HOOKS_AND_RECORDED_REQUESTS"),
  };
}
export async function installGoose(c: AdapterContext) {
  if (
    c.scope === "user" &&
    (await readSettings(c.config.paths.user))?.enabled !== true
  )
    throw new Error("SETUP_REQUIRED");
  if (!(await detectGoose()).supported)
    throw new Error("UNQUALIFIED_GOOSE_SCHEMA");
  if (await nativeConflict()) throw new Error("NATIVE_TRACING_CONFLICT");
  if (await disabledByHost(c.projectDir))
    throw new Error("PLUGIN_DISABLED_BY_GOOSE");
  if (c.scope === "project" && !c.config.paths.projectBinding)
    throw new Error("PROJECT_DIRECTORY_REQUIRED");
  const plugin = goosePaths().plugin;
  const prior = await readJson<Registration>(registrationPath(c.config));
  const manifestPath = join(plugin, "plugin.json"),
    hooksPath = join(plugin, "hooks", "hooks.json");
  const manifest = await readProtected(manifestPath),
    hooks = await readProtected(hooksPath);
  if (
    (manifest !== undefined || hooks !== undefined) &&
    (!prior || manifest !== prior.manifest || hooks !== prior.hooks)
  )
    throw new Error("PLUGIN_OWNERSHIP_CONFLICT");
  await ensurePrivateDirectory(plugin);
  const installed = await runtime(c);
  const node = await interpreterPath();
  const command = `${quote(node)} --disable-warning=ExperimentalWarning ${quote(installed.runtime)} ${quote(c.config.paths.configDir)}`;
  const manifestValue = {
    name: "futureagi-goose",
    version: VERSION,
    description: "Future AGI session, generation and tool tracing",
  };
  const hooksValue = {
    hooks: Object.fromEntries(
      EVENTS.map((e) => [
        e,
        [
          {
            hooks: [
              { type: "command", command, timeout: 10, on_failure: "allow" },
            ],
          },
        ],
      ]),
    ),
  };
  await writeProtected(
    manifestPath,
    JSON.stringify(manifestValue, null, 2) + "\n",
    manifest === undefined ? null : contentHash(manifest),
  );
  await writeProtected(
    hooksPath,
    JSON.stringify(hooksValue, null, 2) + "\n",
    hooks === undefined ? null : contentHash(hooks),
  );
  await updateJson<Registration>(registrationPath(c.config), () => ({
    schemaVersion: 1,
    userScope: prior?.userScope === true || c.scope === "user",
    projectBindings: [
      ...new Set([
        ...(prior?.projectBindings ?? []),
        ...(c.scope === "project" ? [c.config.paths.projectBinding!] : []),
      ]),
    ],
    ...installed,
    node,
    command,
    hooks: JSON.stringify(hooksValue, null, 2) + "\n",
    manifest: JSON.stringify(manifestValue, null, 2) + "\n",
  }));
  await pruneRuntimes(c.config.paths.configDir, installed.runtime);
  return inspectGoose(c);
}
export const gooseAdapter: CoreAdapter = {
  id: "goose",
  version: VERSION,
  requiresWorker: false,
  diagnostics: {
    RUNTIME_UNAVAILABLE: "Run setup again with an installed, supported Node interpreter to restore the owned runtime.",
    PROJECT_NOT_REGISTERED: "Run setup goose in this project with --scope project, or explicitly choose user-scoped setup.",
    PLUGIN_DISABLED_BY_GOOSE: "Review Goose's disabledPlugins setting and enable the Future AGI plugin before retrying.",
    UNQUALIFIED_GOOSE_SCHEMA: "Use a supported stable Goose 1.x version from 1.54, or update this integration.",
    GOOSE_NOT_FOUND: "Install Goose CLI and make it available on the setup command's PATH.",
    UNSUPPORTED_PLATFORM: "Use this adapter on Linux or macOS.",
  },
  setupErrors: {
    PLUGIN_OWNERSHIP_CONFLICT: "Review modified plugin files before retrying; existing content was preserved.",
    RUNTIME_CHANGED: "Review the modified runtime before reinstalling; it was not overwritten.",
    SETUP_REQUIRED: "Run setup goose before updating this integration.",
    PROJECT_DIRECTORY_REQUIRED: "Select the project directory when configuring a project-scoped installation.",
  },
  detect: detectGoose,
  install: installGoose,
  inspect: inspectGoose,
  async uninstall(c) {
    const old = await readJson<Registration>(registrationPath(c.config));
    if (!old) return { registered: false };
    const dir = goosePaths().plugin;
    // Never remove modified files or someone else's hooks.
    for (const [path, expected] of [
      [join(dir, "hooks", "hooks.json"), old.hooks],
      [join(dir, "plugin.json"), old.manifest],
    ]) {
      if ((await readProtected(path)) !== expected)
        throw new Error("PLUGIN_OWNERSHIP_CONFLICT");
    }
    await fs.unlink(join(dir, "hooks", "hooks.json"));
    await fs.unlink(join(dir, "plugin.json"));
    await fs.unlink(registrationPath(c.config));
    await pruneRuntimes(c.config.paths.configDir);
    return { registered: false };
  },
  async run(_c, args) {
    return new Promise<number>((resolve) => {
      const child = spawn("goose", args, { stdio: "inherit" });
      child.on("error", () => resolve(127));
      child.on("exit", (code) => resolve(code ?? 1));
    });
  },
};
