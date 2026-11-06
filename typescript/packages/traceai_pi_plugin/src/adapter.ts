import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { dirname, join, normalize } from "node:path";
import { minimatch } from "minimatch";
import { readdir } from "node:fs/promises";
import {
  readJson,
  readProtected,
  updateJson,
  contentHash,
  type CoreAdapter,
  type AdapterContext,
  type AdapterInspection,
} from "@traceai/agent-core";
import { VERSION } from "./version";
const exec = promisify(execFile);
export const PI_VERSION = "1.5.0";
/** Native request/settlement API introduced in 1.5; tolerate compatible host updates. */
export const supportedPiVersion = (version: string): boolean => {
  const match = /^1\.(\d+)\.(\d+)$/.exec(version);
  return !!match && match[0] === version && Number(match[1]) >= 5;
};
export const PACKAGE_SOURCE = `npm:@traceai/pi-plugin@${VERSION}`;
interface PackageFilter {
  source: string;
  autoload?: boolean;
  extensions?: string[];
}
interface Settings {
  packages?: Array<string | PackageFilter>;
  extensions?: string[];
}
/** Match Pi 1.5's package resource selection for our one native entry point. */
export function extensionEnabled(filter: PackageFilter, packageRoot: string): boolean {
  const patterns = filter.extensions;
  if (patterns === undefined) return filter.autoload !== false;
  if (!Array.isArray(patterns) || !patterns.every((p) => typeof p === "string")) return false;
  const paths = ["extension.mjs", join(packageRoot, "extension.mjs").replace(/\\/g, "/")];
  const match = (pattern: string, exact = false) => {
    const normalized = normalize(pattern).replace(/\\/g, "/");
    return paths.some((path) => exact ? path === normalized : minimatch(path, normalized));
  };
  if (filter.autoload === false) {
    // With autoload off the host applies matching deltas in source order.
    let enabled = false;
    for (const pattern of patterns) {
      const prefix = pattern[0];
      const override = ["+", "-", "!"].includes(prefix);
      if (match(override ? pattern.slice(1) : pattern, prefix === "+" || prefix === "-"))
        enabled = prefix !== "-" && prefix !== "!";
    }
    return enabled;
  }
  if (!patterns.length) return false;
  const includes = patterns.filter((p) => !/^[+!-]/.test(p));
  let enabled = !includes.length || includes.some((p) => match(p));
  if (patterns.some((p) => p.startsWith("!") && match(p.slice(1)))) enabled = false;
  if (patterns.some((p) => p.startsWith("+") && match(p.slice(1), true))) enabled = true;
  if (patterns.some((p) => p.startsWith("-") && match(p.slice(1), true))) enabled = false;
  return enabled;
}
interface Owned {
  source: string;
  scope: "user" | "project";
  settings: string;
  entryHash: string;
}
const sourceOf = (p: string | { source: string }) =>
  typeof p === "string" ? p : p.source;
const ourSource = (s: string) =>
  /^npm:@traceai\/pi-plugin(?:@[^\s]+)?$/.test(s);
function locations(c: AdapterContext) {
  return {
    user: join(dirname(c.config.paths.user), "settings.json"),
    project: c.config.paths.project
      ? join(dirname(c.config.paths.project), "settings.json")
      : undefined,
    owned: join(
      c.config.paths.configDir,
      `pi-registration-${c.scope === "project" ? c.config.paths.projectBinding : "user"}.json`,
    ),
  };
}
async function command(c: AdapterContext, args: string[]): Promise<string> {
  const env: NodeJS.ProcessEnv = {
    ...process.env,
    PI_CODING_AGENT_DIR: dirname(c.config.paths.user),
  };
  // npm installers do not need the tracing credential pair.
  delete env.FI_API_KEY;
  delete env.FI_SECRET_KEY;
  try {
    const result = await exec("pi", args, {
      env,
      cwd: c.projectDir,
      timeout: args[0] === "--version" ? 5000 : 120000,
      maxBuffer: 256 * 1024,
    });
    return result.stdout;
  } catch (error) {
    // Respect native project trust; never silently add --approve to the installer.
    if (
      (error as { stderr?: string }).stderr?.includes("Project is not trusted.")
    )
      throw new Error("TRUST_REQUIRED");
    throw error;
  }
}
async function inspect(c: AdapterContext): Promise<AdapterInspection> {
  const paths = locations(c);
  const scopes = [paths.user, paths.project].filter((v): v is string => !!v);
  let count = 0,
    disabled = false,
    installed = false,
    legacy = false;
  for (const path of scopes) {
    if (
      path === paths.project &&
      c.config.issues.includes("PROJECT_CONFIG_IGNORED")
    )
      continue; // Status still reports the ignored layer through core config issues.
    const value = await readJson<Settings>(path, false);
    for (const p of value?.packages ?? [])
      if (ourSource(sourceOf(p))) {
        count++;
        const native = await readJson<{
          version?: string;
          pi?: { extensions?: string[] };
        }>(
          join(
            dirname(path),
            "npm",
            "node_modules",
            "@traceai",
            "pi-plugin",
            "package.json",
          ),
          false,
        );
        if (
          native?.version === VERSION &&
          native.pi?.extensions?.includes("./extension.mjs")
        )
          installed = true;
        if (
          typeof p !== "string" &&
          !extensionEnabled(p, join(dirname(path), "npm/node_modules/@traceai/pi-plugin"))
        )
          disabled = true;
      }
    // A manually copied observer and the native extension would duplicate tracing.
    if (value?.extensions?.some((p) => /futureagi|traceai.*pi/i.test(p)))
      legacy = true;
    try {
      const files = await readdir(join(dirname(path), "extensions"));
      if (files.some((p) => /^futureagi-otlp\.(?:ts|js|mjs|cjs)$/.test(p)))
        legacy = true;
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
    }
  }
  let version: string | undefined;
  try {
    version = (await command(c, ["--version"])).trim();
  } catch {
    /* not installed */
  }
  return {
    registered: count === 1 && installed && !disabled && !legacy,
    duplicate: count > 1 || legacy,
    hostVersion: version,
    issues: [
      ...(!version ? ["PI_NOT_FOUND"] : !supportedPiVersion(version) ? ["PI_VERSION_NOT_QUALIFIED"] : []),
      ...(disabled ? ["EXTENSION_DISABLED"] : []),
    ],
    trust: c.scope === "project" ? "unknown" : "confirmed",
    detail: disabled
      ? "EXTENSION_DISABLED"
      : c.scope === "project"
        ? "PROJECT_TRUST_IS_MANAGED_BY_PI"
        : "RESTART_OR_RELOAD_PI",
  };
}
export const piAdapter: CoreAdapter = {
  id: "pi",
  version: VERSION,
  requiresWorker: false,
  diagnostics: {
    PI_NOT_FOUND: "Install Pi and make its pi executable available on PATH, then retry.",
    PI_VERSION_NOT_QUALIFIED: "Use a supported Pi release (1.5 or later on the 1.x line).",
    EXTENSION_DISABLED: "Enable @traceai/pi-plugin's extension.mjs in Pi package settings; setup preserves explicit autoload and extension exclusions.",
    RESTART_OR_RELOAD_PI: "Restart Pi or reload extensions to activate tracing.",
    PROJECT_TRUST_IS_MANAGED_BY_PI: "Review and approve the project through Pi's native trust flow, then reload.",
    TRUST_REQUIRED: "Review and approve the project through Pi's native trust flow, then retry setup.",
    NOT_OWNED_REMOVE_WITH_PI: "Remove this manually installed plugin with Pi's native package manager.",
    REGISTRATION_CHANGED: "The package registration was edited; review it in Pi before removing it.",
  },
  async detect(c) {
    try {
      const version = (await command(c, ["--version"])).trim();
      return {
        supported: supportedPiVersion(version),
        version,
        reason: supportedPiVersion(version)
          ? undefined
          : "PI_VERSION_NOT_QUALIFIED",
      };
    } catch {
      return { supported: false, reason: "PI_NOT_FOUND" };
    }
  },
  inspect,
  async install(c) {
    const before = await inspect(c);
    if (before.duplicate || before.issues?.includes("EXTENSION_DISABLED")) return before;
    if (before.registered) return before; // do not take ownership of a pre-existing native install
    const paths = locations(c),
      target = c.scope === "project" ? paths.project : paths.user;
    if (!target) throw new Error("PROJECT_DIRECTORY_REQUIRED");
    // Refuse symlinks / non-owner files before handing editing to Pi's own installer.
    await readProtected(target, false);
    await command(c, [
      "install",
      PACKAGE_SOURCE,
      ...(c.scope === "project" ? ["-l"] : []),
    ]);
    const after = await inspect(c);
    if (after.registered) {
      const saved = await readJson<Settings>(target, false);
      const entry = saved?.packages?.find(
        (p) => sourceOf(p) === PACKAGE_SOURCE,
      );
      if (entry)
        await updateJson<Owned>(paths.owned, () => ({
          source: PACKAGE_SOURCE,
          scope: c.scope,
          settings: target,
          entryHash: contentHash(JSON.stringify(entry)),
        }));
    }
    return after;
  },
  async uninstall(c) {
    const paths = locations(c),
      owner = await readJson<Owned>(paths.owned);
    if (!owner || owner.scope !== c.scope)
      return { ...(await inspect(c)), detail: "NOT_OWNED_REMOVE_WITH_PI" };
    const expected = c.scope === "project" ? paths.project : paths.user;
    if (owner.settings !== expected || owner.source !== PACKAGE_SOURCE)
      throw new Error("REGISTRATION_CHANGED");
    const current = await readJson<Settings>(owner.settings, false);
    const entry = current?.packages?.find((p) => sourceOf(p) === owner.source);
    if (!entry || contentHash(JSON.stringify(entry)) !== owner.entryHash)
      return { ...(await inspect(c)), detail: "REGISTRATION_CHANGED" };
    await command(c, [
      "remove",
      owner.source,
      ...(c.scope === "project" ? ["-l"] : []),
    ]);
    return inspect(c);
  },
};
