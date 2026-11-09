import { cursorDiagnostics } from "./diagnostics.js";
import { promises as fs } from "node:fs";
import { basename, join, isAbsolute } from "node:path";
import { homedir } from "node:os";
import {
  readJson, readProtected, updateJson, disable, contentHash, removeProtected,
  type AdapterContext, type AdapterInspection, type CoreAdapter,
} from "@traceai/agent-core";
import { EVENTS, VERSION } from "./events.js";
import { editHostFile, installRuntime, mergeHooks, type Hooks } from "./registration.js";

/** Separate hosted surface, never qualified by a local IDE/CLI replay. */
export const cloudQualification = Object.freeze({
  live: false,
  sources: ["https://cursor.com/docs/hooks", "https://cursor.com/docs/cloud-agent/setup"],
  blockers: ["AUTHORIZED_CLOUD_RUN_REQUIRED", "CLOUD_HOOK_MATRIX_CONFLICT", "NPM_AVAILABILITY_REQUIRED"],
  unavailable: ["early-read-only-activity", "model-requests", "token-usage", "cost", "editor-session-events"],
});
export const CLOUD_EVENTS = EVENTS.filter((e) => e !== "sessionEnd");
interface Receipt {
  schemaVersion: 1;
  hooksFile: string;
  runtime: string;
  owned: Hooks[];
  ownedFileHash?: string;
}
function paths(c: AdapterContext): { hooks: string; receipt: string } {
  if (c.config.agent !== "cursor-cloud" || !c.projectDir || !isAbsolute(c.projectDir) || !c.config.paths.projectBinding)
    throw new Error("CLOUD_PROJECT_SCOPE_REQUIRED");
  return {
    hooks: join(c.projectDir, ".cursor", "hooks.json"),
    receipt: join(c.config.paths.configDir, `cursor-cloud-${c.config.paths.projectBinding}-registration.json`),
  };
}
const same = (a: unknown, b: unknown) => JSON.stringify(a) === JSON.stringify(b);
function validateReceipt(receipt: Receipt | undefined, hooks: string): void {
  if (receipt && (receipt.schemaVersion !== 1 || receipt.hooksFile !== hooks || !Array.isArray(receipt.owned)))
    throw new Error("CURSOR_OWNERSHIP_CONFLICT");
}
export function cloudCommands(runtime: string): Hooks {
  const name = basename(runtime);
  if (!/^cursor-cloud-[a-zA-Z0-9.-]+$/.test(name)) throw new Error("INVALID_RUNTIME_PATH");
  // Resolve inside each Cloud environment, not to the machine which generated the repository hooks.
  const root = `\${XDG_DATA_HOME:-$HOME/.local/share}/futureagi/agents/runtime/${name}`;
  return Object.fromEntries(CLOUD_EVENTS.map((event) => [event, {
    command: `"${root}/node" "${root}/hook.cjs" --marker fi-cursor-cloud-v1 --event ${event}`,
    timeout: 10,
    failClosed: false,
  }]));
}
/** Package/environment preparation, also used by contract tests. Does not enroll an account,
 * modify environment.json, grant trust, commit hooks, or claim that a Cloud run is working.
 */
export async function prepareCloudEnvironment(c: AdapterContext, home = homedir()): Promise<AdapterInspection> {
  if (c.scope !== "project") throw new Error("CLOUD_PROJECT_SCOPE_REQUIRED");
  const { hooks, receipt: receiptPath } = paths(c);
  const receipt = await readJson<Receipt>(receiptPath);
  validateReceipt(receipt, hooks);
  const runtime = await installRuntime(c, { entry: "cloud-hook", prefix: "cursor-cloud", home });
  const desired = cloudCommands(runtime);
  const known = receipt?.owned ?? [];
  mergeHooks(await readProtected(hooks, false), known, desired, CLOUD_EVENTS);
  const owned = [...known.filter((h) => !same(h, desired)), desired];
  if (owned.length > 8) throw new Error("CURSOR_OWNERSHIP_LIMIT");
  await updateJson<Receipt>(receiptPath, (previous) => {
    if (!same(receipt, previous)) throw new Error("CONCURRENT_SAVE");
    return { schemaVersion: 1, hooksFile: hooks, runtime, owned };
  });
  let ownedFileHash: string | undefined;
  await editHostFile(hooks, (before) => {
    const next = mergeHooks(before, owned, desired, CLOUD_EVENTS);
    if (before === undefined || (receipt?.ownedFileHash && contentHash(before) === receipt.ownedFileHash))
      ownedFileHash = contentHash(next);
    return next;
  });
  await updateJson<Receipt>(receiptPath, () => ({
    schemaVersion: 1, hooksFile: hooks, runtime, owned: [desired], ownedFileHash,
  }));
  return inspectCloud(c);
}
export async function inspectCloud(c: AdapterContext): Promise<AdapterInspection> {
  const { hooks, receipt: receiptPath } = paths(c);
  const receipt = await readJson<Receipt>(receiptPath);
  if (!receipt) return { registered: false, activated: false, trust: "unknown", detail: "CLOUD_ENVIRONMENT_NOT_PREPARED" };
  validateReceipt(receipt, hooks);
  const raw = await readProtected(hooks, false);
  if (!raw) return { registered: false, detail: "PROJECT_HOOKS_MISSING" };
  let duplicate = false;
  try { mergeHooks(raw, receipt.owned, undefined, CLOUD_EVENTS); } catch { duplicate = true; }
  // mergeHooks validates JSONC and ownership. Parsing its comment-preserving result needs the same parser.
  const { parse } = await import("jsonc-parser");
  const doc = parse(raw);
  const desired = cloudCommands(receipt.runtime);
  let runtimePresent = true;
  for (const name of ["node", "hook.cjs", "cloud-runtime.cjs"]) {
    try {
      const stat = await fs.lstat(join(receipt.runtime, name));
      if (!stat.isFile() || stat.isSymbolicLink() || stat.nlink !== 1 || stat.mode & 0o022 || (process.getuid && stat.uid !== process.getuid()))
        runtimePresent = false;
    } catch { runtimePresent = false; }
  }
  const registered = runtimePresent && !duplicate && CLOUD_EVENTS.every((event) =>
    doc?.hooks?.[event]?.filter((h: unknown) => same(h, desired[event])).length === 1);
  return {
    registered, activated: false, duplicate, trust: "unknown",
    detail: registered ? "PROJECT_HOOKS_PREPARED_CLOUD_DEPLOYMENT_UNVERIFIED" : "PROJECT_HOOKS_OR_RUNTIME_MISSING",
  };
}
export async function removeCloudHooks(c: AdapterContext): Promise<AdapterInspection> {
  const { hooks, receipt: receiptPath } = paths(c);
  const raw = await readProtected(receiptPath);
  const receipt: Receipt | undefined = raw === undefined ? undefined : JSON.parse(raw);
  validateReceipt(receipt, hooks);
  if (receipt) {
    await editHostFile(hooks, (before) => {
      if (before === undefined) return undefined;
      const next = mergeHooks(before, receipt.owned, undefined, CLOUD_EVENTS);
      return receipt.ownedFileHash === contentHash(before) ? undefined : next;
    });
    await removeProtected(receiptPath, contentHash(raw!));
  }
  // Runtime bundles can be shared by other project bindings; never delete them here.
  return { registered: false, activated: false };
}
export const cursorCloudAdapter: CoreAdapter = {
  id: "cursor-cloud", version: VERSION, requiresWorker: false,
  diagnostics: cursorDiagnostics,
  async detect() { return { supported: false, reason: "AUTHORIZED_CLOUD_RUN_REQUIRED" }; },
  async install() { throw new Error("AUTHORIZED_CLOUD_RUN_REQUIRED"); },
  inspect: inspectCloud,
  async uninstall(c) {
    await disable({ agent: "cursor-cloud", env: { ...process.env, FI_AGENT_CONFIG_DIR: c.config.paths.configDir } });
    return removeCloudHooks(c);
  },
};
