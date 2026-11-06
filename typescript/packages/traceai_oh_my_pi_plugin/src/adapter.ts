import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { dirname, join } from "node:path";
import { readdir } from "node:fs/promises";
import {
  readJson,
  updateJson,
  contentHash,
  type CoreAdapter,
  type AdapterContext,
  type AdapterInspection,
} from "@traceai/agent-core";
export const VERSION = "0.1.0"; // x-release-please-version
export const HOST_VERSION = "18.17.0";
export const PACKAGE_NAME = "@traceai/oh-my-pi-plugin";
export const PACKAGE_SOURCE = `npm:${PACKAGE_NAME}@${VERSION}`;
const exec = promisify(execFile);
/** Qualify the 18.x extension contract, not one auto-updating patch release. */
export function supportedHostVersion(version: string): boolean {
  const match = /^18\.(\d+)\.(\d+)$/.exec(version);
  return !!match && Number(match[1]) >= 17;
}
interface NativePlugin {
  name: string;
  version: string;
  enabled: boolean;
  path: string;
  manifest?: { extensions?: string[] };
}
interface Owned {
  version: string;
  path: string;
  hash: string;
}
const ownershipPath = (c: AdapterContext) =>
  join(c.config.paths.configDir, "oh-my-pi-registration.json");
async function command(c: AdapterContext, args: string[]): Promise<string> {
  const env: NodeJS.ProcessEnv = { ...process.env };
  // The host itself resolves profiles and plugin directories. Never pass credentials to its installer.
  delete env.FI_API_KEY;
  delete env.FI_SECRET_KEY;
  const r = await exec("omp", args, {
    cwd: c.projectDir,
    env,
    timeout: args[0] === "install" || args[1] === "uninstall" ? 120000 : 10000,
    maxBuffer: 512 * 1024,
  });
  return r.stdout;
}
async function installed(c: AdapterContext): Promise<NativePlugin[]> {
  const list = JSON.parse(await command(c, ["plugin", "list", "--json"]));
  return (Array.isArray(list.npm) ? list.npm : []).filter(
    (p: NativePlugin) => p.name === PACKAGE_NAME,
  );
}
async function inspect(c: AdapterContext): Promise<AdapterInspection> {
  const plugins = await installed(c);
  const legacy: string[] = [];
  const roots = [
    dirname(c.config.paths.user),
    c.projectDir && join(c.projectDir, ".omp"),
  ].filter((x): x is string => !!x);
  for (const root of roots) {
    for (const subdir of ["hooks/pre", "hooks/post", "extensions"]) {
      try {
        for (const file of await readdir(join(root, subdir)))
          if (/^futureagi-otlp\.(?:ts|js|mjs|cjs)$/.test(file))
            legacy.push(join(root, subdir, file));
      } catch (e) {
        if ((e as NodeJS.ErrnoException).code !== "ENOENT") throw e;
      }
    }
  }
  // omp list reads the installed manifest. Bun's package files can be hard-linked to its
  // cache: they are public package metadata, not protected credential/config files.
  const p = plugins[0];
  const override =
    c.projectDir && !c.config.issues?.includes("PROJECT_CONFIG_IGNORED")
      ? await readJson<{ disabled?: string[] }>(
          join(c.projectDir, ".omp/plugin-overrides.json"),
          false,
        )
      : undefined;
  const disabled =
    p?.enabled === false || override?.disabled?.includes(PACKAGE_NAME);
  return {
    registered:
      plugins.length === 1 &&
      legacy.length === 0 &&
      !disabled &&
      p.version === VERSION &&
      p.manifest?.extensions?.includes("./extension.mjs") === true,
    duplicate: plugins.length > 1 || legacy.length > 0,
    hostVersion: (await command(c, ["--version"])).trim().replace(/^omp\//, ""),
    trust: "confirmed", // OMP 18.17.0 does not gate extension loading on project trust.
    detail: legacy.length
      ? `REMOVE_LEGACY_COPIED_HOOK: ${legacy.join(", ")}`
      : disabled
        ? "PLUGIN_DISABLED"
        : "RESTART_OMP_NATIVE_NPM_PLUGIN_IS_USER_SCOPED",
  };
}
export const ohMyPiAdapter: CoreAdapter = {
  id: "oh-my-pi",
  version: VERSION,
  requiresWorker: false,
  async detect(c) {
    try {
      const version = (await command(c, ["--version"]))
        .trim()
        .replace(/^omp\//, "");
      return {
        supported: supportedHostVersion(version),
        version,
        reason: supportedHostVersion(version)
          ? undefined
          : "OMP_VERSION_NOT_QUALIFIED",
      };
    } catch {
      return { supported: false, reason: "OMP_NOT_FOUND" };
    }
  },
  inspect,
  async install(c) {
    const before = await inspect(c);
    if (before.registered || before.duplicate) return before;
    // Upgrade only an unchanged enabled registration we installed. A manual,
    // disabled, or edited plugin remains the user's decision, including on update.
    const current = await installed(c);
    if (current.length) {
      const p = current[0];
      const owner = await readJson<Owned>(ownershipPath(c));
      if (current.length !== 1 || before.detail === "PLUGIN_DISABLED" ||
          !owner || owner.version !== p.version || owner.path !== p.path ||
          owner.hash !== contentHash(JSON.stringify(p)))
        return {
          ...before,
          detail: "EXISTING_PLUGIN_REQUIRES_MANUAL_UPDATE_OR_ENABLE",
        };
    }
    await command(c, ["install", PACKAGE_SOURCE, "--json"]);
    const after = await inspect(c);
    if (after.registered) {
      const p = (await installed(c))[0];
      await updateJson<Owned>(ownershipPath(c), () => ({
        version: p.version,
        path: p.path,
        hash: contentHash(JSON.stringify(p)),
      }));
    }
    return after;
  },
  async uninstall(c) {
    const owner = await readJson<Owned>(ownershipPath(c));
    const p = (await installed(c))[0];
    if (
      !owner ||
      !p ||
      owner.path !== p.path ||
      owner.version !== p.version ||
      owner.hash !== contentHash(JSON.stringify(p))
    )
      return {
        ...(await inspect(c)),
        detail: "NOT_OWNED_OR_CHANGED_REMOVE_WITH_OMP",
      };
    await command(c, ["plugin", "uninstall", PACKAGE_NAME, "--json"]);
    return inspect(c);
  },
};
