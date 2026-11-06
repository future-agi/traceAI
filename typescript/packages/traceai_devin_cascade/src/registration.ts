import { constants, promises as fs } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { homedir } from "node:os";
import { createHash } from "node:crypto";
import { applyEdits, modify, parse, type ParseError } from "jsonc-parser";
import {
  contentHash, ensurePrivateDirectory, readJson, readProtected, writeProtected,
  type AdapterContext, type AdapterInspection,
} from "@traceai/agent-core";
import { isRecord } from "./transcript";

export const VERSION = "0.1.0";
export const EVENTS = ["pre_user_prompt", "post_cascade_response_with_transcript"] as const;
export const MARKER = "--futureagi-cascade";
interface Receipt { schemaVersion: 1; target: string; command: string; version: string }
const quote = (s: string): string => `'${s.replace(/'/g, `'"'"'`)}'`;
function document(text: string | undefined): Record<string, unknown> {
  const errors: ParseError[] = [];
  const value: unknown = parse(text ?? "{}", errors, { allowTrailingComma: true });
  if (errors.length || !isRecord(value) || (value.hooks !== undefined && !isRecord(value.hooks)))
    throw new Error("INVALID_HOOK_CONFIG");
  for (const entries of Object.values((value.hooks ?? {}) as Record<string, unknown>))
    if (!Array.isArray(entries)) throw new Error("INVALID_HOOK_CONFIG");
  return value;
}
const active = (v: Record<string, unknown>): boolean => Object.values((v.hooks ?? {}) as Record<string, unknown>)
  .some(entries => Array.isArray(entries) && entries.length > 0);
export async function hookTarget(context: AdapterContext): Promise<string> {
  if (context.scope === "user") return join(dirname(context.config.paths.user), "hooks.json");
  if (!context.projectDir) throw new Error("PROJECT_DIRECTORY_REQUIRED");
  const modern = join(context.projectDir, ".devin", "hooks.json"), legacy = join(context.projectDir, ".windsurf", "hooks.json");
  // Do not create a modern file that would silently shadow the user's legacy hooks.
  if (active(document(await readProtected(modern, false)))) return modern;
  if (active(document(await readProtected(legacy, false)))) return legacy;
  return modern;
}
/** JSONC patching keeps comments and unrelated events, and refuses edited owned entries. */
export function mergeHooks(text: string | undefined, command: string, previous?: string, remove = false): string {
  let output = text ?? "{\n  \"hooks\": {}\n}\n";
  const doc = document(text);
  const hooks = (doc.hooks ?? {}) as Record<string, unknown[]>;
  for (const event of EVENTS) {
    const existing = hooks[event] ?? [];
    for (const entry of existing) {
      if (!isRecord(entry)) throw new Error("INVALID_HOOK_CONFIG");
      if (typeof entry.command === "string" && entry.command.includes(MARKER) &&
          (entry.command !== command && entry.command !== previous || entry.show_output !== false ||
           Object.keys(entry).some(key => !["command", "show_output"].includes(key))))
        throw new Error("OWNED_HOOK_MODIFIED");
    }
    const remaining = existing.filter(entry => !isRecord(entry) ||
      (entry.command !== command && (previous === undefined || entry.command !== previous)));
    if (!remove) remaining.push({ command, show_output: false });
    // On uninstall, never create keys in a file that had no owned hook.
    if (remove && remaining.length === existing.length) continue;
    output = applyEdits(output, modify(output, ["hooks", event], remaining.length ? remaining : undefined,
      { formattingOptions: { insertSpaces: true, tabSize: 2, eol: "\n" } }));
  }
  return output;
}
async function reconcile(path: string, command: string, previous?: string, remove = false): Promise<void> {
  for (let attempt = 0; attempt < 3; attempt++) {
    const old = await readProtected(path, false);
    if (remove && old === undefined) return;
    const updated = mergeHooks(old, command, previous, remove);
    if (updated === old) return;
    try { await writeProtected(path, updated, old === undefined ? null : contentHash(old), false); return; }
    catch (e) { if ((e as Error).message !== "CONCURRENT_SAVE" || attempt === 2) throw e; }
  }
}
async function stageRuntime(base: string): Promise<string> {
  // Only our packaged bundle is read, never an event/project-selected executable.
  const bytes = await fs.readFile(join(__dirname, "hook.cjs"));
  if (bytes.length > 8 * 1024 * 1024) throw new Error("RUNTIME_LIMIT");
  const hash = createHash("sha256").update(bytes).digest("hex");
  const dir = join(base, VERSION, hash);
  await ensurePrivateDirectory(dir);
  const path = join(dir, "hook.cjs");
  let output;
  try { output = await fs.open(path, constants.O_CREAT | constants.O_EXCL | constants.O_WRONLY | constants.O_NOFOLLOW, 0o600); }
  catch (e) { if ((e as NodeJS.ErrnoException).code !== "EEXIST") throw e; }
  if (output) {
    try { await output.writeFile(bytes); await output.sync(); } finally { await output.close(); }
  }
  const input = await fs.open(path, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK);
  try {
    const st = await input.stat();
    if (!st.isFile() || st.nlink !== 1 || st.size !== bytes.length || st.mode & 0o077 || (process.getuid && st.uid !== process.getuid()))
      throw new Error("UNSAFE_RUNTIME");
    if (createHash("sha256").update(await input.readFile()).digest("hex") !== hash) throw new Error("RUNTIME_CHANGED");
  } finally { await input.close(); }
  return path;
}
const receiptPath = (context: AdapterContext): string => join(context.config.paths.configDir, `cascade-registration${context.scope === "project" ? `-${context.config.paths.projectBinding}` : ""}.json`);
async function checkReceipt(context: AdapterContext): Promise<Receipt | undefined> {
  const value = await readJson<Receipt>(receiptPath(context));
  if (value && (value.schemaVersion !== 1 || value.target !== await hookTarget(context) || typeof value.command !== "string" || !value.command.includes(MARKER)))
    throw new Error("REGISTRATION_SCOPE_CHANGED");
  return value;
}
/** Low-level installer used by the adapter ONLY after host qualification. Exported for contract tests. */
export async function installHooks(context: AdapterContext, runtimeBase?: string): Promise<AdapterInspection> {
  const target = await hookTarget(context), receipt = await checkReceipt(context);
  // The two installed events are the sole owned hooks. Never touch enterprise policy files.
  const sibling = context.scope === "project" ? join(dirname(context.config.paths.user), "hooks.json") :
    context.projectDir ? await hookTarget({ ...context, scope: "project" }) : undefined;
  if (sibling && (await readProtected(sibling, false))?.includes(MARKER)) throw new Error("DUPLICATE_REGISTRATION");
  const runtime = await stageRuntime(runtimeBase ?? join(process.env.XDG_DATA_HOME || join(homedir(), ".local", "share"), "futureagi", "agents", "cascade-runtime"));
  const node = await fs.realpath(process.execPath);
  if (node.includes("/_npx/")) throw new Error("EPHEMERAL_NODE_RUNTIME");
  // Bind the config directory explicitly so GUI launches do not require a shell's FI env vars.
  const stateHome = dirname(dirname(context.config.paths.stateDir));
  const command = `FI_AGENT_CONFIG_DIR=${quote(context.config.paths.configDir)} XDG_STATE_HOME=${quote(stateHome)} ${quote(node)} ${quote(runtime)} ${MARKER}`;
  await reconcile(target, command, receipt?.command);
  await writeProtected(receiptPath(context), JSON.stringify({ schemaVersion: 1, target, command, version: VERSION }),
    receipt ? contentHash((await readProtected(receiptPath(context)))!) : null);
  return inspectHooks(context);
}
export async function inspectHooks(context: AdapterContext): Promise<AdapterInspection> {
  const receipt = await checkReceipt(context);
  if (!receipt) return { registered: false, detail: "CASCADE_GUI_QUALIFICATION_REQUIRED", trust: "unknown" };
  const doc = document(await readProtected(receipt.target, false));
  const hooks = (doc.hooks ?? {}) as Record<string, unknown[]>;
  let duplicate = false;
  const registered = EVENTS.every(event => {
    const matches = (hooks[event] ?? []).filter(row => isRecord(row) && row.command === receipt.command);
    if (matches.length > 1) duplicate = true;
    return matches.length === 1;
  });
  return { registered, duplicate, trust: "unknown", detail: "Restart the editor; workspace trust must be granted in the host, not by this adapter." };
}
export async function uninstallHooks(context: AdapterContext): Promise<AdapterInspection> {
  const receipt = await checkReceipt(context);
  if (receipt) {
    await reconcile(receipt.target, receipt.command, undefined, true);
    await fs.unlink(receiptPath(context));
  }
  return { registered: false, trust: "unknown" };
}
