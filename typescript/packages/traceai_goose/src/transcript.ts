import { lstat } from "node:fs/promises";
import { join } from "node:path";
import { DatabaseSync } from "node:sqlite";
import { safeParents, contentHash } from "@traceai/agent-core/files";
import { redact, redactStructured } from "@traceai/agent-core/privacy";
import type { ResolvedConfig, UsageInput } from "@traceai/agent-core";
import { goosePaths } from "./paths.js";

export interface Row {
  id: number;
  message_id: string;
  role: string;
  content_json: string;
  metadata_json: string;
  created_timestamp: number;
}
export interface ToolTiming {
  start: number;
  end?: number;
  error?: boolean;
}
export interface SessionState {
  schemaVersion: 2;
  /** Logical identities, not SQLite row IDs: compaction replaces the conversation. */
  baseline: string[];
  completed: string[];
  observed?: string[];
  turnId: string;
  promptHash?: string;
  stopped?: boolean;
  stoppedAt?: number;
  /** Only an explicit native cancellation reason establishes interruption. */
  interrupted?: boolean;
  /** Canonical authorized root; SessionEnd omits working_dir on resumed runs. */
  projectDir?: string;
  pending: boolean;
  start: number;
  generation: number;
  policy: string;
  tools: Record<string, ToolTiming>;
}
export function validSession(value: unknown): value is string {
  return typeof value === "string" && /^[A-Za-z0-9_-]{1,128}$/.test(value);
}
export async function openTranscript(): Promise<DatabaseSync> {
  const directory = join(goosePaths().data, "sessions");
  await safeParents(directory);
  const path = join(directory, "sessions.db");
  const stat = await lstat(path);
  if (
    !stat.isFile() ||
    stat.isSymbolicLink() ||
    stat.nlink !== 1 ||
    (process.getuid && stat.uid !== process.getuid())
  )
    throw new Error("UNSAFE_TRANSCRIPT");
  const db = new DatabaseSync(path, { readOnly: true });
  db.exec("PRAGMA query_only=ON; PRAGMA busy_timeout=100;");
  return db;
}
export function messageKey(row: Pick<Row, "message_id">): string {
  if (
    typeof row.message_id !== "string" ||
    !row.message_id ||
    row.message_id.length > 512
  )
    throw new Error("UNQUALIFIED_MESSAGE_ID");
  return contentHash(row.message_id);
}
interface MessageIndex {
  id: number;
  key: string;
  bytes: number;
}
export function messageIndex(
  db: DatabaseSync,
  session: string,
): MessageIndex[] {
  const rows = db
    .prepare(
      "SELECT id, substr(message_id,1,513) AS message_id, length(content_json) + COALESCE(length(metadata_json),0) AS bytes FROM messages WHERE session_id = ? ORDER BY id LIMIT 4097",
    )
    .all(session) as unknown as (Pick<Row, "id" | "message_id"> & {
    bytes: number;
  })[];
  if (rows.length > 4096) throw new Error("TRANSCRIPT_SESSION_LIMIT");
  const index = rows.map((r) => ({
    id: r.id,
    key: messageKey(r),
    bytes: r.bytes,
  }));
  if (new Set(index.map((r) => r.key)).size !== index.length)
    throw new Error("AMBIGUOUS_MESSAGE_ID");
  return index;
}
export function readRows(
  db: DatabaseSync,
  session: string,
  baseline: string[] = [],
  index = messageIndex(db, session),
): Row[] {
  const seen = new Set(baseline);
  const selected = index.filter((r) => !seen.has(r.key));
  // Bound bytes before JS allocation; refuse a partial turn rather than invent billing.
  if (
    selected.length > 256 ||
    selected.reduce((n, r) => n + r.bytes, 0) > 4 * 1024 * 1024
  )
    throw new Error("TRANSCRIPT_TURN_LIMIT");
  const query = db.prepare(
    "SELECT id, message_id, role, content_json, metadata_json, created_timestamp FROM messages WHERE session_id = ? AND id = ?",
  );
  return selected.map((r) => query.get(session, r.id) as unknown as Row);
}
/** Only the tail can be an already-persisted new prompt. Never search historical text. */
export function persistedPrompt(
  db: DatabaseSync,
  session: string,
  index: MessageIndex[],
  completed: string[],
  message?: string,
): Row | undefined {
  const tail = index.at(-1);
  if (!tail || completed.includes(tail.key) || message === undefined) return;
  const row = readRows(db, session, [], [tail])[0];
  if (row.role !== "user" || !JSON.parse(row.metadata_json || "{}").userVisible)
    return;
  const parts = JSON.parse(row.content_json);
  if (
    Array.isArray(parts) &&
    parts.some((p: any) => p.type === "text" && p.text === message)
  )
    return row;
}
function text(parts: unknown): string {
  if (!Array.isArray(parts)) return "";
  return parts
    .filter((p) => p && p.type === "text" && typeof p.text === "string")
    .map((p) => redact(p.text))
    .join("\n")
    .slice(0, 20000);
}
function count(v: unknown): number | undefined {
  return typeof v === "number" && Number.isSafeInteger(v) && v >= 0
    ? v
    : undefined;
}
function usage(value: any): UsageInput | undefined {
  if (!value || typeof value !== "object") return undefined;
  return {
    input: count(value.inputTokens),
    output: count(value.outputTokens),
    cacheRead: count(value.cacheReadTokens),
    cacheWrite: count(value.cacheWriteTokens),
    // Goose normalizes inputTokens to include cache reads and writes before
    // saving the response, including Anthropic's separately reported counters.
    cacheInInput: true,
    reasoningInOutput: true,
    ...(typeof value.cost === "number" &&
    value.cost >= 0 &&
    Number.isFinite(value.cost) &&
    ["provider_reported", "estimated"].includes(value.costSource)
      ? {
          costUsd: value.cost,
          costSource:
            value.costSource === "provider_reported"
              ? ("reported" as const)
              : ("estimated" as const),
        }
      : {}),
  };
}
/** A Stop can precede the SQLite commit. Silence or an unfinished tool is not a terminal record. */
export function hasTerminalResponse(rows: Row[]): boolean {
  const parsed = rows.map((row) => ({
    role: row.role,
    parts: JSON.parse(row.content_json),
    meta: JSON.parse(row.metadata_json || "{}"),
  }));
  if (parsed.some((row) => !Array.isArray(row.parts))) return false;
  const generations = parsed.filter(
    (row) => row.role === "assistant" && row.meta?.inference,
  );
  const last = generations.at(-1);
  if (
    !last ||
    !text(last.parts) ||
    last.parts.some((p: any) => p?.type === "toolRequest")
  )
    return false;
  const responses = new Set(
    parsed
      .flatMap((row) => row.parts)
      .filter((p: any) => p?.type === "toolResponse")
      .map((p: any) => p.id),
  );
  return generations.every((row) =>
    row.parts.every(
      (p: any) => p?.type !== "toolRequest" || responses.has(p.id),
    ),
  );
}

/** Only persisted provider-response records own billing. Agent/session totals are never added. */
export async function exportTurn(
  config: ResolvedConfig,
  session: string,
  state: SessionState,
  rows: Row[],
) {
  const parsed = rows.map((r) => ({
    ...r,
    parts: JSON.parse(r.content_json),
    meta: JSON.parse(r.metadata_json || "{}"),
  }));
  if (
    parsed.some(
      (row) =>
        !Array.isArray(row.parts) ||
        !row.meta ||
        typeof row.meta !== "object" ||
        !Number.isFinite(row.created_timestamp) ||
        row.parts.some((part: any) => !part || typeof part.type !== "string") ||
        (row.meta.inference &&
          (typeof row.meta.inference.provider !== "string" ||
            typeof (
              row.meta.inference.resolvedModel ??
              row.meta.inference.requestedModel
            ) !== "string")),
    )
  )
    throw new Error("UNQUALIFIED_TRANSCRIPT_SCHEMA");
  const prompt = parsed.find(
    (r) =>
      r.role === "user" &&
      r.meta.userVisible &&
      Array.isArray(r.parts) &&
      r.parts.some(
        (p: any) =>
          p.type === "text" &&
          (!state.promptHash || contentHash(p.text) === state.promptHash),
      ),
  );
  const generations = parsed.filter(
    (r) => r.role === "assistant" && r.meta.inference,
  );
  const detailCount = generations.reduce((n, row) => n + 1 + (row.meta.usage?.isCompaction ? 1 : 0)
    + row.parts.filter((part: any) => part.type === "toolRequest").length, 0);
  const aggregateOnly = detailCount > 240;
  // Only a confirmed, unchanged Stop boundary owns the turn's end time.
  // Delivery on the next prompt must not include the user's idle time.
  const observed = new Set(state.observed ?? []);
  const stopped =
    state.stoppedAt !== undefined &&
    rows.every((row) => observed.has(messageKey(row)));
  const endedAt = stopped
    ? Math.max(state.start, state.stoppedAt!)
    : Date.now();
  const { createTurnTracer } = await import("@traceai/agent-core");
  const tracer = createTurnTracer({ config });
  const turn = tracer.startTurn({
    sessionId: session,
    turnId: state.turnId,
    input: prompt ? text(prompt.parts) : undefined,
    startTime: state.start,
    usagePlan: aggregateOnly ? "turn" : "request",
  });
  const lastGeneration = generations.at(-1);
  const complete =
    lastGeneration &&
    text(lastGeneration.parts) &&
    !lastGeneration.parts.some((p: any) => p.type === "toolRequest");
  // Goose emits Stop even on provider failures with no saved response. Absence is not success.
  let outcome: "success" | "error" | "interrupted" | "incomplete" =
    state.interrupted
      ? "interrupted"
      : state.stopped && complete && (state.stoppedAt === undefined || stopped)
        ? "success"
        : "incomplete";
  if (aggregateOnly) {
    const totals: UsageInput = { cacheInInput: true, reasoningInOutput: true };
    for (const row of generations) {
      const counters = usage(row.meta.usage);
      for (const field of ["input", "output", "cacheRead", "cacheWrite", "reasoning", "costUsd"] as const)
        if (counters?.[field] !== undefined) totals[field] = (totals[field] ?? 0) + counters[field]!;
      if (counters?.costSource === "estimated") totals.costSource = "estimated";
    }
    turn.end({ output: text(lastGeneration?.parts), outcome, usage: totals, gap: "SPAN_DETAIL_LIMIT", endTime: endedAt });
    await tracer.shutdown();
    return;
  }
  let input = prompt ? text(prompt.parts) : undefined;
  let children = 0;
  for (const row of generations) {
    children++;
    const inference = row.meta.inference;
    const id = `model-${messageKey(row)}`;
    // Goose persists second-resolution message timestamps, not OTLP start/end IDs.
    // Do not present this as native request timing or a full model-context capture.
    const at = Math.max(
      state.start,
      Math.min(endedAt, row.created_timestamp * 1000),
    );
    const model = turn.child({
      id,
      kind: "llm",
      name: "generation",
      model: inference.resolvedModel || inference.requestedModel,
      provider: inference.provider,
      fidelity: "recorded-request",
      sourceTiming: "message-interval",
      input,
      startTime: at,
    });
    model.end({
      output:
        text(row.parts) ||
        JSON.stringify(
          row.parts
            .filter((p: any) => p.type === "toolRequest")
            .map((p: any) => ({
              id: p.id,
              name: p.toolCall?.value?.name,
              ...(config.hideInputs
                ? {}
                : {
                    arguments: redactStructured(p.toolCall?.value?.arguments),
                  }),
            })),
        ),
      usage: usage(row.meta.usage),
      endTime: at,
    });
    if (row.meta.usage?.isCompaction) {
      children++;
      turn
        .child({
          id: `compaction-${messageKey(row)}`,
          kind: "compaction",
          parentId: id,
          fidelity: "reconstructed-message",
          startTime: at,
        })
        .end({ endTime: at });
    }
    for (const part of Array.isArray(row.parts) ? row.parts : []) {
      if (part.type !== "toolRequest" || typeof part.id !== "string") continue;
      const source = part.toolCall;
      const response = parsed
        .flatMap((r) => (Array.isArray(r.parts) ? r.parts : []))
        .find((p: any) => p.type === "toolResponse" && p.id === part.id);
      const result = response?.toolResult;
      const timing = state.tools[contentHash(part.id)];
      const failed =
        timing?.error ||
        source?.status === "error" ||
        result?.status === "error" ||
        result?.value?.isError === true;
      // The terminal cancellation owns the root outcome; individual tool failures
      // and unfinished tools keep their own statuses without replacing it.
      if (!state.interrupted) {
        if (failed) outcome = "error";
        else if (!response && outcome !== "error") outcome = "incomplete";
      }
      const toolStart = timing?.start ?? at;
      children++;
      const tool = turn.child({
        id: `tool-${messageKey(row)}-${contentHash(part.id)}`,
        parentId: id,
        kind: "tool",
        toolCallId: part.id,
        name: source?.value?.name,
        input: JSON.stringify(redactStructured(source?.value?.arguments)),
        startTime: toolStart,
        sourceTiming: timing ? "hook-interval" : "message-interval",
        fidelity: "reconstructed-message",
      });
      const output =
        result?.status === "success"
          ? text(result?.value?.content)
          : typeof result?.error === "string"
            ? redact(result.error)
            : undefined;
      tool.end({
        output,
        outcome: failed ? "error" : response ? "success" : "incomplete",
        endTime: timing?.end ?? endedAt,
      });
      // Privacy applies before content crosses span boundaries, including successful
      // tool envelopes that carry isError=true.
      input =
        config.hideOutputs || (failed && config.hideErrorDetails)
          ? undefined
          : output;
      if (children % 32 === 0) await tracer.flush();
    }
    await tracer.flush();
  }
  const final = [...generations]
    .reverse()
    .map((r) => text(r.parts))
    .find(Boolean);
  turn.end({ output: final, outcome, endTime: endedAt });
  await tracer.shutdown();
}
