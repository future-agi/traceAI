import {
  sessionId,
  workId,
  traceId,
  spanId,
  type CanonicalFact,
} from "@traceai/agent-core";
import type { NormalizeContext } from "../../adapters.js";
import { AgentError } from "../../security/files.js";

export type CursorSurface = "ide" | "cli";
export const HOOK_SCHEMA = "cursor-hooks-2026-10-31-v1";
export const STREAM_SCHEMA = "cursor-cli-stream-2026-10-31-v1";
// Fixture-qualified contracts, not a claim that arbitrary/newer host builds work.
export const VERSIONS = { ide: ["3.24.18"], cli: ["2026.10.17"] } as const;
export const EVENTS = ["postToolUse", "postToolUseFailure", "stop"] as const;
export const TOOLS = [
  "Shell",
  "Read",
  "Write",
  "Edit",
  "Grep",
  "Glob",
  "Task",
  "Delete",
  "custom",
] as const;
type Data = Record<string, unknown>;
export function object(value: unknown): Data {
  if (!value || typeof value !== "object" || Array.isArray(value))
    throw new AgentError("INVALID_CURSOR_EVENT", 2);
  return value as Data;
}
function id(value: unknown): string {
  if (
    typeof value !== "string" ||
    !/^[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,255}$/.test(value)
  )
    throw new AgentError("HELD_IDENTITY", 3);
  return value;
}
function duration(value: unknown): number {
  if (value === undefined) return 0;
  if (
    typeof value !== "number" ||
    !Number.isFinite(value) ||
    value < 0 ||
    value > 86_400_000
  )
    throw new AgentError("INVALID_CURSOR_DURATION", 2);
  return value;
}
function tool(value: unknown): string {
  return TOOLS.includes(value as (typeof TOOLS)[number])
    ? (value as string)
    : "custom";
}
export interface CursorEvent {
  conversation_id: string;
  generation_id: string;
  cursor_version: string;
  hook_event_name: (typeof EVENTS)[number];
  tool_use_id?: string;
  tool_name?: string;
  duration?: number;
  status?: "completed" | "aborted" | "error";
  failure_type?: "timeout" | "error" | "permission_denied";
  is_interrupt?: boolean;
}
/** This allowlist runs BEFORE IPC, and again in the runtime. No content/path/model labels. */
export function minimizeCursorEvent(
  input: unknown,
  surface: CursorSurface,
  expectedEvent?: string,
): CursorEvent {
  const p = object(input);
  if (!(VERSIONS[surface] as readonly unknown[]).includes(p.cursor_version))
    throw new AgentError("UNKNOWN_CURSOR_VERSION", 5);
  if (
    !EVENTS.includes(p.hook_event_name as (typeof EVENTS)[number]) ||
    (expectedEvent && expectedEvent !== p.hook_event_name)
  )
    throw new AgentError("UNSUPPORTED_CURSOR_EVENT", 5);
  const e: CursorEvent = {
    conversation_id: id(p.conversation_id),
    generation_id: id(p.generation_id),
    cursor_version: p.cursor_version as string,
    hook_event_name: p.hook_event_name as CursorEvent["hook_event_name"],
  };
  if (e.hook_event_name === "stop") {
    if (!["completed", "aborted", "error"].includes(p.status as string))
      throw new AgentError("INVALID_CURSOR_STATUS", 2);
    e.status = p.status as CursorEvent["status"];
  } else {
    e.tool_use_id = id(p.tool_use_id);
    e.tool_name = tool(p.tool_name);
    e.duration = duration(p.duration);
    if (e.hook_event_name === "postToolUseFailure") {
      if (
        !["timeout", "error", "permission_denied"].includes(
          p.failure_type as string,
        )
      )
        throw new AgentError("INVALID_CURSOR_STATUS", 2);
      e.failure_type = p.failure_type as CursorEvent["failure_type"];
      e.is_interrupt = p.is_interrupt === true;
    }
  }
  return e;
}
function fact(
  surface: CursorSurface,
  schema: string,
  session: string,
  turn: string,
  unit: CanonicalFact["unit"],
  anchor: string | undefined,
  c: NormalizeContext,
): CanonicalFact {
  // Stream imports cannot be joined to hooks: no documented generation_id <-> request_id alias.
  const namespace =
    schema === STREAM_SCHEMA ? "cursor-cli-stream" : "cursor-" + surface;
  const turnWork = workId(c.identityKey, namespace, "turn", session, turn);
  const work = workId(c.identityKey, namespace, unit, session, turn, anchor);
  return {
    schemaVersion: 1,
    sourceWorkId: work,
    identityProfile: namespace + "-v1",
    sessionId: sessionId(c.identityKey, namespace, session),
    turnId: turnWork,
    traceId: traceId(turnWork),
    spanId: spanId(work),
    ...(unit !== "turn" ? { parentSpanId: spanId(turnWork) } : {}),
    agent: "cursor",
    surface,
    agentVersion: VERSIONS[surface][0],
    source: schema === STREAM_SCHEMA ? "transcript" : "hook",
    sourceSchema: schema,
    reconstructionVersion: "1",
    policyRevision: c.policyRevision,
    unit,
    timing: "unknown",
    startNs: "0",
    endNs: "0",
    outcome: "incomplete",
    incomplete: true,
    inputCompleteness: "unavailable",
    outputCompleteness: "unavailable",
  };
}
/** Relative intervals are stamped once, atomically, by admitObserved in the storage child. */
export function normalizeCursorHook(
  input: unknown,
  surface: CursorSurface,
  c: NormalizeContext,
): CanonicalFact[] {
  if (c.sourceSchema !== HOOK_SCHEMA)
    throw new AgentError("UNKNOWN_SOURCE_SCHEMA", 5);
  const e = minimizeCursorEvent(input, surface);
  const f = fact(
    surface,
    HOOK_SCHEMA,
    e.conversation_id,
    e.generation_id,
    e.hook_event_name === "stop" ? "turn" : "tool",
    e.tool_use_id,
    c,
  );
  if (e.hook_event_name === "stop") {
    f.outcome =
      e.status === "completed"
        ? "success"
        : e.status === "aborted"
          ? "cancelled"
          : "error";
  } else {
    f.tool = e.tool_name;
    f.outcome =
      e.hook_event_name === "postToolUse"
        ? "success"
        : e.is_interrupt
          ? "cancelled"
          : e.failure_type === "permission_denied"
            ? "permission_denied"
            : "error";
    // Reported duration ends at our callback receipt, not a provider/source timestamp.
    // The documented millisecond field is a number, not necessarily an integer.
    // The bounded duration remains safely representable at nanosecond resolution.
    f.endNs = BigInt(Math.round((e.duration ?? 0) * 1_000_000)).toString();
    f.timing = e.duration ? "estimated" : "unknown";
  }
  return [f];
}
/** Bounded explicit stream-json import. Persisted IDE/CLI transcript schemas are NOT inferred. */
export function readCursorStream(
  bytes: Buffer,
  c: NormalizeContext,
): CanonicalFact[] {
  if (c.sourceSchema !== STREAM_SCHEMA)
    throw new AgentError("UNKNOWN_SOURCE_SCHEMA", 5);
  if (bytes.length > 1_048_576) throw new AgentError("SOURCE_TOO_LARGE", 4);
  if (!bytes.length || bytes[bytes.length - 1] !== 10)
    throw new AgentError("PARTIAL_SOURCE_TAIL", 4);
  const lines = bytes.toString("utf8").trimEnd().split("\n");
  if (lines.length > 4096) throw new AgentError("SOURCE_TOO_LARGE", 4);
  let records: Data[];
  try {
    records = lines.map((line) => object(JSON.parse(line)));
  } catch {
    throw new AgentError("UNKNOWN_SOURCE_SCHEMA", 5);
  }
  const terminal = records[records.length - 1];
  if (
    terminal.type !== "result" ||
    terminal.subtype !== "success" ||
    terminal.is_error !== false
  )
    throw new AgentError("INCOMPLETE_CURSOR_STREAM", 4);
  const session = id(terminal.session_id),
    turn = id(terminal.request_id);
  if (
    records.some((r) => r.session_id !== session) ||
    records.filter((r) => r.type === "result").length !== 1 ||
    records.filter((r) => r.type === "user").length !== 1
  )
    throw new AgentError("AMBIGUOUS_CURSOR_TURN", 3);
  const times = records
    .filter((r) => r.type === "assistant" && r.timestamp_ms !== undefined)
    .map((r) => {
      const t = r.timestamp_ms;
      if (
        typeof t !== "number" ||
        !Number.isSafeInteger(t) ||
        t <= 0 ||
        t > 18_446_744_073_709
      )
        throw new AgentError("INVALID_SOURCE_TIME", 2);
      return t;
    });
  // A saved untimestamped stream has no portable wall clock. Never use import time as execution time.
  if (!times.length) throw new AgentError("HELD_SOURCE_TIME", 3);
  const root = fact("cli", STREAM_SCHEMA, session, turn, "turn", undefined, c);
  root.startNs = (BigInt(Math.min(...times)) * 1_000_000n).toString();
  root.endNs = (BigInt(Math.max(...times)) * 1_000_000n).toString();
  root.timing = "message_interval";
  root.outcome = "success";
  const facts = new Map<string, CanonicalFact>([[root.sourceWorkId, root]]);
  const add = (f: CanonicalFact) => {
    const old = facts.get(f.sourceWorkId);
    if (old && JSON.stringify(old) !== JSON.stringify(f))
      throw new AgentError("HELD_IMMUTABLE_CONFLICT", 3);
    facts.set(f.sourceWorkId, f);
    if (facts.size > 128) throw new AgentError("IMPORT_SLICE_REQUIRED", 4);
  };
  for (const r of records) {
    // Only the documented model_call_id flush identifies an inference unit. Deltas, thoughts,
    // final flush and result text are NOT additional requests, even when they contain usage.
    if (r.type === "assistant" && r.model_call_id !== undefined) {
      const f = fact(
        "cli",
        STREAM_SCHEMA,
        session,
        turn,
        "request",
        id(r.model_call_id),
        c,
      );
      if (r.timestamp_ms === undefined)
        throw new AgentError("HELD_SOURCE_TIME", 3);
      f.startNs = f.endNs = (
        BigInt(r.timestamp_ms as number) * 1_000_000n
      ).toString();
      f.timing = "message_interval";
      add(f);
    } else if (r.type === "tool_call" && r.subtype === "completed") {
      const f = fact(
        "cli",
        STREAM_SCHEMA,
        session,
        turn,
        "tool",
        id(r.call_id),
        c,
      );
      const call = object(r.tool_call);
      const names = ["readToolCall", "writeToolCall"].filter(
        (name) => call[name] !== undefined,
      );
      f.tool =
        names.length === 1
          ? names[0] === "readToolCall"
            ? "Read"
            : "Write"
          : "custom";
      // No native timestamp or originating model ID on this event. No FIFO model/tool join.
      f.startNs = f.endNs = root.endNs;
      f.timing = "estimated";
      add(f);
    }
  }
  return [...facts.values()];
}
