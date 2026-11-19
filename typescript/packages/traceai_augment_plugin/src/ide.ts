import { constants, promises as fs } from "node:fs";
import { homedir } from "node:os";
import { dirname, isAbsolute, join, resolve } from "node:path";
import { strFromU8, unzipSync } from "fflate";
import { safeParents, type CoreAdapter, type AdapterContext, type AdapterInspection } from "@traceai/agent-core";
import { augmentHookRegistration, VERSION, EVENTS } from "./adapter";

export const IDE_HOLD = "AUGMENT_IDE_GUI_QUALIFICATION_REQUIRED";
export type Editor = "vscode" | "intellij";
export interface EditorDiscoveryOptions {
  home?: string;
  platform?: NodeJS.Platform;
  env?: NodeJS.ProcessEnv;
  /** Explicit diagnostic overrides, never taken from hook payloads or project configuration. */
  vscodeExtensionsDir?: string;
  intellijPluginsDirs?: string[];
}
export interface EditorInstallation {
  editor: Editor;
  pluginId: string;
  version: string;
  manifest: string;
}
export interface EditorDiscovery {
  installations: EditorInstallation[];
  issues: string[];
}
export const editorProfiles = {
  vscode: { pluginId: "augment.vscode-augment", marketplace: "https://marketplace.visualstudio.com/items?itemName=augment.vscode-augment" },
  intellij: { pluginId: "com.augmentcode", marketplace: "https://plugins.jetbrains.com/plugin/24072-augment" },
} as const;

// Installation metadata proves presence, NOT enablement, trust, hook execution or model coverage.
// This allowlist intentionally stays empty until each editor has its own real GUI session evidence.
const qualifiedVersions: Readonly<Record<Editor, readonly string[]>> = { vscode: [], intellij: [] };
const validVersion = (v: unknown): v is string => typeof v === "string" && /^\d+\.\d+\.\d+(?:-[a-zA-Z0-9.-]+)?$/.test(v) && v.length < 80;
const missing = (e: unknown): boolean => ["ENOENT", "ENOTDIR"].includes((e as NodeJS.ErrnoException).code ?? "");
async function directories(path: string): Promise<string[]> {
  try {
    await safeParents(path);
    const entries = await fs.readdir(path, { withFileTypes: true });
    if (entries.length > 1024) throw new Error("IDE_DISCOVERY_LIMIT");
    return entries.filter((e) => e.isDirectory()).map((e) => join(path, e.name)).sort();
  } catch (e) { if (missing(e)) return []; throw e; }
}
async function readBounded(path: string, max: number): Promise<Buffer> {
  await safeParents(dirname(path));
  const file = await fs.open(path, constants.O_RDONLY | constants.O_NOFOLLOW);
  try {
    const stat = await file.stat();
    if (!stat.isFile() || stat.nlink !== 1 || stat.size > max) throw new Error("UNSAFE_IDE_MANIFEST");
    // Read at most max+1 even if another process grows the file after stat().
    const out = Buffer.alloc(Math.min(stat.size + 1, max + 1));
    let size = 0;
    while (size < out.length) {
      const { bytesRead } = await file.read(out, size, out.length - size, size);
      if (!bytesRead) break;
      size += bytesRead;
    }
    if (size > stat.size || size > max) throw new Error("IDE_MANIFEST_CHANGED");
    return out.subarray(0, size);
  } finally { await file.close(); }
}
function intellijVersion(xml: string): string | undefined {
  if (xml.length > 65536 || /<!DOCTYPE|<!ENTITY/i.test(xml)) return undefined;
  // Read only fixed identity fields; never interpret XML entities or extension implementation names.
  const ids = [...xml.matchAll(/<id>\s*([^<>]+?)\s*<\/id>/g)];
  const versions = [...xml.matchAll(/<version>\s*([^<>]+?)\s*<\/version>/g)];
  if (ids.length !== 1 || ids[0][1] !== editorProfiles.intellij.pluginId || versions.length !== 1) return undefined;
  return validVersion(versions[0][1]) ? versions[0][1] : undefined;
}
async function intellijManifests(pluginDir: string): Promise<EditorInstallation[]> {
  const found: EditorInstallation[] = [];
  const add = (xml: string, manifest: string): void => {
    const version = intellijVersion(xml);
    if (version) found.push({ editor: "intellij", pluginId: editorProfiles.intellij.pluginId, version, manifest });
  };
  const loose = join(pluginDir, "META-INF", "plugin.xml");
  try { add((await readBounded(loose, 65536)).toString("utf8"), loose); }
  catch (e) { if (!missing(e)) throw e; }
  const lib = join(pluginDir, "lib");
  let jars: string[];
  try {
    await safeParents(lib);
    const entries = await fs.readdir(lib, { withFileTypes: true });
    if (entries.length > 128) throw new Error("IDE_DISCOVERY_LIMIT");
    jars = entries.filter((e) => e.isFile() && e.name.endsWith(".jar")).map((e) => join(lib, e.name));
  } catch (e) { if (missing(e)) return found; throw e; }
  for (const jar of jars) {
    const bytes = await readBounded(jar, 32 * 1024 * 1024);
    const archive = unzipSync(bytes, { filter: (entry) => entry.name === "META-INF/plugin.xml" && entry.originalSize <= 65536 });
    const xml = archive["META-INF/plugin.xml"];
    if (xml) add(strFromU8(xml), `${jar}!/META-INF/plugin.xml`);
  }
  return found;
}

/** Bounded metadata-only discovery. Never starts an IDE, reads auth, or equates auggie with an editor. */
export async function discoverAugmentEditors(options: EditorDiscoveryOptions = {}): Promise<EditorDiscovery> {
  const home = resolve(options.home ?? homedir()), env = options.env ?? process.env;
  const platform = options.platform ?? process.platform;
  const result: EditorDiscovery = { installations: [], issues: [] };
  if (platform === "win32") return { ...result, issues: ["WINDOWS_ACL_NOT_QUALIFIED"] };
  if (platform !== "linux" && platform !== "darwin") return { ...result, issues: ["EDITOR_PLATFORM_NOT_QUALIFIED"] };
  for (const value of [options.vscodeExtensionsDir, ...(options.intellijPluginsDirs ?? [])])
    if (value !== undefined && !isAbsolute(value)) throw new Error("IDE_DISCOVERY_PATH_MUST_BE_ABSOLUTE");
  try {
    const root = options.vscodeExtensionsDir ?? join(home, ".vscode", "extensions");
    for (const dir of await directories(root)) {
      if (!/[/\\]augment\.vscode-augment-\d/.test(dir)) continue; // Exclude nightly/forks.
      const manifest = join(dir, "package.json");
      const doc = JSON.parse((await readBounded(manifest, 1024 * 1024)).toString("utf8"));
      if (doc.publisher === "augment" && doc.name === "vscode-augment" && validVersion(doc.version))
        result.installations.push({ editor: "vscode", pluginId: editorProfiles.vscode.pluginId, version: doc.version, manifest });
    }
  } catch { result.issues.push("VSCODE_DISCOVERY_INCOMPLETE"); }
  try {
    const base = platform === "darwin" ? join(home, "Library", "Application Support", "JetBrains")
      : join(env.XDG_DATA_HOME || join(home, ".local", "share"), "JetBrains");
    const roots = options.intellijPluginsDirs ?? (await directories(base))
      .filter((dir) => /[/\\](IntelliJIdea|IdeaIC)\d{4}\.\d/.test(dir)).map((dir) => join(dir, "plugins"));
    if (roots.length > 32) throw new Error("IDE_DISCOVERY_LIMIT");
    for (const root of roots) {
      for (const dir of await directories(root)) {
        // Only inspect Augment's distribution, not arbitrary installed plugins or their settings.
        if (!/[/\\](augment(?:[-_.][a-zA-Z0-9_.-]+)?|com\.augmentcode)$/i.test(dir)) continue;
        result.installations.push(...await intellijManifests(dir));
      }
    }
  } catch { result.issues.push("INTELLIJ_DISCOVERY_INCOMPLETE"); }
  return result;
}
export function ideRegistrationProfile(context: AdapterContext): { settings: string; events: typeof EVENTS; scope: "user" } {
  if (context.scope !== "user") throw new Error("AUGMENT_IDE_PROJECT_HOOKS_NOT_QUALIFIED");
  // User hooks are explicitly documented for BOTH editors. Do not infer project hook support from CLI.
  return { settings: join(dirname(context.config.paths.user), "settings.json"), events: EVENTS, scope: "user" };
}
function discoveryOptions(context: AdapterContext): EditorDiscoveryOptions {
  // configPaths resolves this from the user's home, never an event-supplied workspace root.
  return { home: dirname(dirname(context.config.paths.user)) };
}
async function detect(context: AdapterContext): ReturnType<CoreAdapter["detect"]> {
  ideRegistrationProfile(context);
  const found = await discoverAugmentEditors(discoveryOptions(context));
  const approved = found.installations.length > 0 && !found.issues.length && found.installations.every((i) => qualifiedVersions[i.editor].includes(i.version));
  return { supported: approved, reason: approved ? undefined : IDE_HOLD };
}
export const augmentIdeAdapter: CoreAdapter = {
  id: "augment-ide", version: VERSION, requiresWorker: false,
  diagnostics: { [IDE_HOLD]: "VS Code and IntelliJ each need an independently qualified GUI hook session; CLI evidence is not editor qualification." },
  detect,
  async install(context) {
    ideRegistrationProfile(context);
    if (!(await detect(context)).supported) throw new Error(IDE_HOLD);
    return augmentHookRegistration.install(context); // One shared hook/runtime implementation, no separate exporter.
  },
  async inspect(context): Promise<AdapterInspection> {
    const discovery = await discoverAugmentEditors(discoveryOptions(context));
    const shared = await augmentHookRegistration.inspect(context);
    const versions = discovery.installations.map((i) => `${i.editor}@${i.version}`).join(", ");
    return {
      registered: false, activated: false, trust: "unknown", duplicate: shared.duplicate,
      detail: IDE_HOLD, hostVersion: versions || undefined, issues: discovery.issues,
    };
  },
  async uninstall(context) {
    ideRegistrationProfile(context);
    return augmentHookRegistration.uninstall(context);
  },
};
