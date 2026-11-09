import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { dirname, join, resolve } from "node:path";
import { readdir } from "node:fs/promises";
import { applyEdits, modify, parse, type ParseError } from "jsonc-parser";
import {
  contentHash, readProtected, writeProtected, readJson, updateJson,
  type CoreAdapter, type AdapterContext, type AdapterInspection,
} from "@traceai/agent-core";

const PACKAGE = "@traceai/opencode-v1-plugin";
const SPEC = `${PACKAGE}@0.1.0`;
const HOST = ">=1.19.0 <2.0.0";
// Auto-updates within the qualified V1 hook line retain shape validation in the observer.
const supported = (version: string): boolean => /^1\.(?:19|[2-9]\d|[1-9]\d{2,})\.\d+$/.test(version);
const exec = promisify(execFile);
type Ownership = { schemaVersion: 1; entries: Record<string, string> };
const spec = (item: unknown): string | undefined => typeof item === "string" ? item : Array.isArray(item) && typeof item[0] === "string" ? item[0] : undefined;
const ours = (item: unknown): boolean => { const s = spec(item); return s === PACKAGE || s?.startsWith(`${PACKAGE}@`) === true; };
const otherVersion = (item: unknown): boolean => { const s = spec(item); return s === "@traceai/opencode-plugin" || s?.startsWith("@traceai/opencode-plugin@") === true; };
function decode(text: string | undefined): { plugin?: unknown[] } {
  const errors: ParseError[] = [];
  const value = parse(text ?? "{}", errors, { allowTrailingComma: true });
  if (errors.length || !value || typeof value !== "object" || Array.isArray(value) || (value.plugin !== undefined && !Array.isArray(value.plugin))) throw new Error("INVALID_OPENCODE_CONFIG");
  return value;
}
function paths(ctx: AdapterContext): { files: string[]; target: string } {
  if (process.env.OPENCODE_CONFIG_CONTENT) throw new Error("INLINE_OPENCODE_CONFIG_UNSUPPORTED");
  const user = dirname(ctx.config.paths.user);
  const project = ctx.projectDir && !ctx.config.issues.includes("PROJECT_CONFIG_IGNORED") ? resolve(ctx.projectDir) : undefined;
  if (ctx.scope === "project" && !project) throw new Error("PROJECT_DIRECTORY_REQUIRED");
  const custom = process.env.OPENCODE_CONFIG ? resolve(process.env.OPENCODE_CONFIG) : undefined;
  const extra = process.env.OPENCODE_CONFIG_DIR ? resolve(process.env.OPENCODE_CONFIG_DIR) : undefined;
  const files = [join(user, "opencode.json"), join(user, "opencode.jsonc"), ...(extra ? [join(extra, "opencode.json"), join(extra, "opencode.jsonc")] : []), ...(custom ? [custom] : []), ...(project ? [join(project, "opencode.json"), join(project, "opencode.jsonc"), join(project, ".opencode", "opencode.json"), join(project, ".opencode", "opencode.jsonc")] : [])];
  return { files: [...new Set(files)], target: ctx.scope === "project" ? join(project!, "opencode.json") : custom ?? join(extra ?? user, "opencode.json") };
}
async function inspect(ctx: AdapterContext): Promise<AdapterInspection> {
  const found: string[] = [];
  let conflict = false;
  const { files } = paths(ctx);
  for (const file of files) {
    const list = decode(await readProtected(file, false)).plugin ?? [];
    for (const item of list) { if (ours(item)) found.push(spec(item)!); if (otherVersion(item)) conflict = true; }
  }
  // Do not run alongside the earlier copied observer, which used mutable session roots.
  const directories = new Set(files.map(file => join(dirname(file), "plugins")));
  if (ctx.projectDir && !ctx.config.issues.includes("PROJECT_CONFIG_IGNORED")) directories.add(join(resolve(ctx.projectDir), ".opencode", "plugins"));
  for (const directory of directories) {
    try { if ((await readdir(directory)).some(name => /futureagi.*\.[cm]?[jt]s$/i.test(name))) conflict = true; }
    catch (error) { if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error; }
  }
  const detected = await detect();
  return { registered: found.length === 1 && found[0] === SPEC && detected.supported && !conflict, duplicate: found.length > 1 || conflict, hostVersion: detected.version, trust: "unknown", detail: detected.supported ? "Restart OpenCode to activate the configured plugin." : `Requires OpenCode ${HOST} V1.` };
}
async function detect(): Promise<{ supported: boolean; version?: string; reason?: string }> {
  try {
    const { stdout } = await exec("opencode", ["--version"], { timeout: 5000, maxBuffer: 4096, windowsHide: true });
    const version = stdout.trim();
    return { supported: supported(version), version: /^\d+\.\d+\.\d+$/.test(version) ? version : undefined, reason: supported(version) ? undefined : "OPENCODE_V1_1_19_OR_NEWER_REQUIRED" };
  } catch { return { supported: false, reason: "OPENCODE_NOT_FOUND" }; }
}
async function edit(file: string, change: (data: { plugin?: unknown[] }, text: string) => string): Promise<void> {
  for (let retry = 0; retry < 3; retry++) {
    const previous = await readProtected(file, false);
    const next = change(decode(previous), previous ?? "{}\n");
    if (next === previous) return;
    try { await writeProtected(file, next, previous === undefined ? null : contentHash(previous), false); return; }
    catch (error) { if ((error as Error).message !== "CONCURRENT_SAVE" || retry === 2) throw error; }
  }
}
const patch = (text: string, path: (string | number)[], value: unknown) => applyEdits(text, modify(text, path, value, { formattingOptions: { insertSpaces: true, tabSize: 2 } }));
const ownershipPath = (ctx: AdapterContext) => join(ctx.config.paths.configDir, "opencode-v1-registration.json");
export const opencodeV1Adapter: CoreAdapter = {
  id: "opencode-v1", version: "0.1.0", detect, inspect,
  async install(ctx) {
    const before = await inspect(ctx);
    if (!(await detect()).supported || before.duplicate) return before;
    if (before.registered) return before;
    let { target } = paths(ctx);
    // Preserve the existing .jsonc file rather than creating a competing .json file.
    if (target.endsWith("opencode.json") && await readProtected(`${target}c`, false) !== undefined) target += "c";
    let added = false;
    await edit(target, (data, text) => {
      if ((data.plugin ?? []).some(item => ours(item) || otherVersion(item))) throw new Error("EXISTING_OPENCODE_PLUGIN_REQUIRES_RECONCILIATION");
      added = true;
      return data.plugin ? patch(text, ["plugin", data.plugin.length], SPEC) : patch(text, ["plugin"], [SPEC]);
    });
    if (added) await updateJson<Ownership>(ownershipPath(ctx), previous => ({ schemaVersion: 1, entries: { ...previous?.entries, [target]: contentHash(JSON.stringify(SPEC)) } }));
    return inspect(ctx);
  },
  async uninstall(ctx) {
    const ownership = await readJson<Ownership>(ownershipPath(ctx));
    const eligible = new Set(paths(ctx).files);
    for (const [file, hash] of Object.entries(ownership?.entries ?? {})) {
      if (!eligible.has(file)) continue;
      await edit(file, (data, text) => {
        const indexes = (data.plugin ?? []).flatMap((item, i) => ours(item) && contentHash(JSON.stringify(item)) === hash ? [i] : []);
        for (const index of indexes.reverse()) text = patch(text, ["plugin", index], undefined);
        return text;
      });
      await updateJson<Ownership>(ownershipPath(ctx), previous => {
        const entries = { ...previous?.entries }; delete entries[file]; return { schemaVersion: 1, entries };
      });
    }
    return inspect(ctx);
  },
};
