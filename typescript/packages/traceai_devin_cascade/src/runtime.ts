import { deliveryStatePath } from "@traceai/agent-core/config";
import { acquireWriteLock } from "@traceai/agent-core/file-lock";
import { promises as fs } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import { createHash } from "node:crypto";
import {
  collectionEnabled, createTurnTracer, destinationId, ensurePrivateDirectory,
  readJson, resolveConfig, updateJson, type ResolveOptions, type DeliveryState,
} from "@traceai/agent-core";
import { digest, parseHook, readSnapshot, settledTranscript, transcriptPath, type Cursor, type ParsedTurn } from "./transcript";

export const AGENT = "devin-cascade";
const MAX_SESSIONS = 512, EXPIRY = 24 * 60 * 60 * 1000;
interface Session {
  schemaVersion: 1;
  cursor?: Cursor;
  active?: { execution: string; prompt: string; baseline?: string; start?: number };
  claimed: string[];
}
function sourceTime(value: unknown): number | undefined {
  if (typeof value !== "string") return undefined;
  const ms = Date.parse(value);
  return Number.isFinite(ms) && ms > 0 && ms <= Date.now() + 60000 ? ms : undefined;
}
/** Metadata-only retention; no transcript, prompt, tool I/O or host path is persisted. */
async function boundState(dir: string): Promise<void> {
  const entries = await fs.readdir(dir);
  if (entries.length > MAX_SESSIONS * 3) throw new Error("STATE_LIMIT");
  let count = 0;
  for (const name of entries) {
    if (!/^[a-f0-9]{64}\.json$/.test(name)) continue;
    const path = join(dir, name), st = await fs.lstat(path);
    if (!st.isFile() || st.isSymbolicLink() || st.nlink !== 1 || st.mode & 0o077 || (process.getuid && st.uid !== process.getuid()))
      throw new Error("UNSAFE_STATE");
    if (Date.now() - st.mtimeMs > EXPIRY) await fs.unlink(path);
    else count++;
  }
  if (count >= MAX_SESSIONS) throw new Error("STATE_LIMIT");
}
export async function handleHook(raw: unknown, options: Omit<ResolveOptions, "agent"> = {}): Promise<string> {
  const event = parseHook(raw);
  const config = await resolveConfig({ ...options, agent: AGENT });
  if (!collectionEnabled(config) || !config.destinationApproved || !config.credentials) return "DISABLED";
  const home = options.home ?? homedir();
  const key = config.identityKey ?? createHash("sha256").update(JSON.stringify(config.credentials)).digest("hex");
  const path = transcriptPath(home, event);
  const namespace = `${destinationId(config.endpoint, config.projectName)}:${config.generation}`;
  const sessionKey = digest(key, `${namespace}:${event.trajectory_id}`);
  const execution = digest(key, event.execution_id);
  const directory = join(config.paths.stateDir, "cascade-sessions");
  await ensurePrivateDirectory(directory);
  const statePath = join(directory, `${sessionKey}.json`);
  if (!(await readJson<Session>(statePath))) await boundState(directory);
  const lock = await acquireWriteLock(`${statePath}.event`, { lockWaitMs: 1500 }).catch(() => undefined);
  if (!lock) return "CONCURRENT_HOOK_DROPPED";
  let parsed: ParsedTurn | undefined, start: number | undefined;
  try {
    const state = (await readJson<Session>(statePath)) ?? { schemaVersion: 1 as const, claimed: [] };
    if (state.schemaVersion !== 1 || !Array.isArray(state.claimed)) throw new Error("INVALID_STATE");
    if (state.claimed.includes(execution)) return "DUPLICATE";
    if (event.agent_action_name === "pre_user_prompt") {
      if (typeof event.tool_info.user_prompt !== "string") throw new Error("PROMPT_SCHEMA");
      if (state.active?.execution === execution) return "DUPLICATE";
      const before = await readSnapshot(path);
      state.active = { execution, prompt: digest(key, event.tool_info.user_prompt),
        baseline: before ? digest(key, before) : undefined, start: sourceTime(event.timestamp) };
      await updateJson(statePath, () => state);
      await updateJson<DeliveryState>(deliveryStatePath(config), old => ({
        pending: 0, exported: 0, dropped: 0, ...old, activatedAt: old?.activatedAt ?? new Date().toISOString(),
      }));
      return "PROMPT_OBSERVED";
    }
    // An old asynchronous response must never be attached to a newer user prompt.
    if (state.active?.execution !== execution) throw new Error("PROMPT_BOUNDARY_MISSING");
    parsed = await settledTranscript(path, key, state.cursor, state.active.baseline);
    if (digest(key, parsed.input ?? "") !== state.active.prompt) throw new Error("PROMPT_MISMATCH");
    start = state.active.start;
    // Claim BEFORE transport. An ambiguous/failed export is not retried as a fresh trace.
    await lock.assertHeld();
    state.cursor = parsed.cursor;
    state.active = undefined;
    state.claimed = [...state.claimed.slice(-63), execution];
    await updateJson(statePath, () => state);
  } finally {
    await lock.release();
  }
  if (!parsed || !collectionEnabled(config)) return "DISABLED";
  const tracer = createTurnTracer({ config });
  try {
    const turn = tracer.startTurn({ sessionId: event.trajectory_id, turnId: event.execution_id,
      input: parsed.input, startTime: start, usagePlan: "unknown", timing: "reconstructed", gaps: parsed.gaps });
    for (const step of parsed.steps) {
      turn.child({ id: step.id, kind: step.kind, name: step.name, input: step.input,
        fidelity: "reconstructed-message", timing: "reconstructed" }).end({ output: step.output, outcome: step.outcome });
    }
    // No per-step source timings exist in the documented transcript schema.
    turn.end({ output: parsed.output, outcome: parsed.outcome });
    await tracer.flush();
    return tracer.state.dropped ? "EXPORT_DROPPED" : parsed.gaps[0] ?? "EXPORTED";
  } finally { await tracer.shutdown(); }
}

export async function recordFailure(code: string, options: Omit<ResolveOptions, "agent"> = {}): Promise<void> {
  try {
    const config = await resolveConfig({ ...options, agent: AGENT });
    if (!collectionEnabled(config)) return;
    const safe = /^[A-Z_]{1,64}$/.test(code) ? code : "HOOK_FAILED";
    await updateJson<DeliveryState>(deliveryStatePath(config), old => ({
      pending: 0, exported: old?.exported ?? 0, dropped: (old?.dropped ?? 0) + 1,
      activatedAt: old?.activatedAt ?? new Date().toISOString(), lastAcceptedAt: old?.lastAcceptedAt, lastError: safe,
    }));
  } catch { /* Telemetry must never affect the host. */ }
}
