import { constants, promises as fs } from "node:fs";
import { join, resolve, dirname } from "node:path";
import { createHmac } from "node:crypto";
import { setTimeout as delay } from "node:timers/promises";
import { safeParents, type TurnOutcome } from "@traceai/agent-core";

export const MAX_TRANSCRIPT_BYTES = 4 * 1024 * 1024;
export const MAX_STEPS = 200;
export const isRecord = (v: unknown): v is Record<string, unknown> =>
  !!v && typeof v === "object" && !Array.isArray(v);
export const digest = (key: string, value: string | Buffer): string =>
  createHmac("sha256", key).update(value).digest("hex");
export interface HookEvent {
  agent_action_name: "pre_user_prompt" | "post_cascade_response_with_transcript";
  trajectory_id: string;
  execution_id: string;
  timestamp?: string;
  tool_info: Record<string, unknown>;
}
export function parseHook(value: unknown): HookEvent {
  if (!isRecord(value) || !["pre_user_prompt", "post_cascade_response_with_transcript"].includes(String(value.agent_action_name)) ||
      ![value.trajectory_id, value.execution_id].every(v => typeof v === "string" && /^[a-zA-Z0-9_-]{1,128}$/.test(v)) ||
      !isRecord(value.tool_info)) throw new Error("INVALID_HOOK");
  return value as unknown as HookEvent;
}
/** A hook can name only its own host-created transcript, never a project or credential file. */
export function transcriptPath(home: string, event: HookEvent): string {
  const expected = join(home, ".windsurf", "transcripts", `${event.trajectory_id}.jsonl`);
  const supplied = event.tool_info.transcript_path;
  if (supplied !== undefined && (typeof supplied !== "string" || resolve(supplied) !== resolve(expected)))
    throw new Error("UNTRUSTED_TRANSCRIPT_PATH");
  return expected;
}
export async function readSnapshot(path: string): Promise<Buffer | undefined> {
  try {
    await safeParents(dirname(path));
    const file = await fs.open(path, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK);
    try {
      const st = await file.stat();
      if (!st.isFile() || st.nlink !== 1 || st.mode & 0o077 || (process.getuid && st.uid !== process.getuid()))
        throw new Error("UNSAFE_TRANSCRIPT");
      if (st.size > MAX_TRANSCRIPT_BYTES) throw new Error("TRANSCRIPT_LIMIT");
      const bytes = Buffer.alloc(MAX_TRANSCRIPT_BYTES + 1);
      let used = 0;
      while (used < bytes.length) {
        const { bytesRead } = await file.read(bytes, used, bytes.length - used, used);
        if (!bytesRead) break;
        used += bytesRead;
      }
      const after = await file.stat();
      if (used > MAX_TRANSCRIPT_BYTES) throw new Error("TRANSCRIPT_LIMIT");
      if (st.size !== after.size || st.mtimeMs !== after.mtimeMs || used !== after.size)
        throw new Error("TRANSCRIPT_CHANGING");
      return bytes.subarray(0, used);
    } finally { await file.close(); }
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") return undefined;
    throw error;
  }
}
export interface Cursor { offset: number; prefix: string; turns: number }
export interface Step { id: string; kind: "generation" | "tool"; name: string; input?: string; output?: string; outcome: TurnOutcome }
export interface ParsedTurn {
  ordinal: number; input?: string; output?: string; steps: Step[];
  outcome: TurnOutcome; cursor: Cursor; gaps: string[];
}
const text = (v: unknown): string | undefined => typeof v === "string" ? v : undefined;
function outcome(status: unknown): TurnOutcome {
  return status === "done" ? "success" : status === "error" ? "error" : status === "interrupted" ? "interrupted" : "incomplete";
}
/** Only the three step types in the official JSONL example have schema-backed mappings.
 * Planner responses are reconstructed generations, NOT native model requests. No invented usage/cost.
 */
export function parseSnapshot(bytes: Buffer, key: string, cursor?: Cursor): ParsedTurn {
  if (!bytes.length || bytes[bytes.length - 1] !== 10) throw new Error("TRANSCRIPT_PARTIAL");
  if (bytes.length > MAX_TRANSCRIPT_BYTES) throw new Error("TRANSCRIPT_LIMIT");
  if (cursor && (!Number.isSafeInteger(cursor.offset) || cursor.offset < 0 || bytes.length < cursor.offset ||
      digest(key, bytes.subarray(0, cursor.offset)) !== cursor.prefix)) throw new Error("TRANSCRIPT_REWRITTEN");
  if (cursor?.offset === bytes.length) throw new Error("TRANSCRIPT_STALE");
  const source = new TextDecoder("utf-8", { fatal: true }).decode(bytes);
  const lines = source.split("\n");
  if (lines.length > 10000) throw new Error("TRANSCRIPT_LIMIT");
  let ordinal = 0, start = -1, byteOffset = 0;
  const rows: Record<string, unknown>[] = [];
  const offsets: number[] = [];
  for (const line of lines.slice(0, -1)) {
    if (Buffer.byteLength(line) > 256 * 1024) throw new Error("TRANSCRIPT_LINE_LIMIT");
    let row: unknown;
    try { row = JSON.parse(line); } catch { throw new Error("TRANSCRIPT_MALFORMED"); }
    if (!isRecord(row) || typeof row.type !== "string") throw new Error("TRANSCRIPT_SCHEMA");
    offsets.push(byteOffset);
    byteOffset += Buffer.byteLength(line) + 1;
    rows.push(row);
    if (row.type === "user_input") { ordinal++; start = rows.length - 1; }
  }
  if (start < 0) throw new Error("PROMPT_MISSING");
  // Never extend a previously finalized turn or replay history when installing mid-conversation.
  if (cursor && (ordinal <= cursor.turns || offsets[start] < cursor.offset)) throw new Error("LATE_TRANSCRIPT_DATA");
  if (rows.length - start > MAX_STEPS) throw new Error("TURN_LIMIT");
  const user = rows[start].user_input;
  if (!isRecord(user) || typeof user.user_response !== "string") throw new Error("PROMPT_SCHEMA");
  const gaps: string[] = [];
  if (cursor && ordinal > cursor.turns + 1) gaps.push("MISSED_TURNS");
  const steps: Step[] = [];
  let output: string | undefined;
  let result: TurnOutcome = outcome(rows[start].status);
  for (let i = start + 1; i < rows.length; i++) {
    const row = rows[i];
    const status = outcome(row.status);
    if (status === "incomplete") throw new Error("TRANSCRIPT_PARTIAL");
    if (status !== "success") result = status;
    if (row.type === "planner_response" && isRecord(row.planner_response) && typeof row.planner_response.response === "string") {
      output = row.planner_response.response;
      steps.push({ id: `step-${i}`, kind: "generation", name: "cascade.planner_response", input: JSON.stringify({ prompt: user.user_response, context: steps.slice(-3).map(step => ({ kind: step.kind, input: step.kind === "tool" ? step.input : undefined, output: step.output })) }), output, outcome: status });
    } else if (row.type === "code_action" && isRecord(row.code_action)) {
      const input = { path: text(row.code_action.path), new_content: text(row.code_action.new_content) };
      steps.push({ id: `step-${i}`, kind: "tool", name: "cascade.code_action", input: JSON.stringify(input), outcome: status });
    } else { gaps.push("UNKNOWN_STEP_SCHEMA"); }
  }
  if (!steps.length || !output) throw new Error("TRANSCRIPT_PARTIAL");
  if (gaps.length) result = "incomplete";
  return { ordinal, input: user.user_response, output, steps, outcome: result,
    cursor: { offset: bytes.length, prefix: digest(key, bytes), turns: ordinal }, gaps: [...new Set(gaps)] };
}
/** The terminal hook supplies completion, not an inactivity timer. Settling only handles async I/O. */
export async function settledTranscript(path: string, key: string, cursor?: Cursor, baseline?: string): Promise<ParsedTurn> {
  let previous: string | undefined, lastError = "TRANSCRIPT_MISSING_OR_PRUNED";
  for (let attempt = 0; attempt < 6; attempt++) {
    try {
      const bytes = await readSnapshot(path);
      if (bytes) {
        const fingerprint = digest(key, bytes);
        if (fingerprint === baseline) { lastError = "TRANSCRIPT_STALE"; }
        else {
          const parsed = parseSnapshot(bytes, key, cursor);
          if (fingerprint === previous) return parsed;
          previous = fingerprint;
          lastError = "TRANSCRIPT_CHANGING";
        }
      }
    } catch (error) {
      const code = (error as Error).message;
      if (!["TRANSCRIPT_PARTIAL", "TRANSCRIPT_MALFORMED", "TRANSCRIPT_CHANGING", "TRANSCRIPT_STALE", "TRANSCRIPT_REWRITTEN"].includes(code)) throw error;
      lastError = code;
      previous = undefined;
    }
    if (attempt < 5) await delay(100);
  }
  throw new Error(lastError);
}
