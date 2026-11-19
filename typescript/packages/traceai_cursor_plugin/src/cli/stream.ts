import { StringDecoder } from "node:string_decoder";
import type { ChildHandle, TurnHandle, TurnTracer } from "@traceai/agent-core";
import { toolResultFailed } from "../events.js";

const MAX_LINE_BYTES = 1024 * 1024;
const MAX_EVENTS = 10_000;
const MAX_TOOLS = 256;
// Whole NDJSON events are bounded above. The core redacts complete fields before
// applying its 20K content cap; slicing here can expose a credential prefix.
type RecordValue = Record<string, unknown>;
const record = (value: unknown): RecordValue | undefined =>
  value !== null && typeof value === "object" && !Array.isArray(value)
    ? value as RecordValue : undefined;
const id = (value: unknown): string | undefined =>
  typeof value === "string" && value.length > 0 && value.length <= 512 && !/[\x00-\x1f]/.test(value)
    ? value : undefined;
const text = (value: unknown): string | undefined =>
  typeof value === "string" ? value : undefined;
function json(value: unknown): string | undefined {
  if (value === undefined) return undefined;
  try { return JSON.stringify(value); } catch { return undefined; }
}

export interface CliStreamDiagnostics {
  events: number;
  malformed: number;
  oversized: number;
  dropped: number;
  duplicateTools: number;
  sessionMismatch: number;
  terminal: boolean;
  source: "cursor-cli-stream-json";
  fidelity: "aggregate-run";
}

/** The print stream has no proven hook/stream identity alias. It is an EXCLUSIVE
 * observation route, never a transcript import or enrichment of IDE hooks.
 * Assistant deltas/flushes are ignored: only the actual terminal result supplies
 * output. No model requests, prompt boundaries, tokens or cost are inferred.
 * All content goes immediately through the shared tracer's privacy policy;
 * only bounded identities and span handles are retained in memory.
 */
export function createCliStreamObserver(options: {
  tracer: TurnTracer;
  /** A launcher-owned invocation identity, not an invented source request id. */
  runId: string;
  /** Required literal prevents accidentally mixing this with hook capture. */
  ownership: "exclusive-stream";
}) {
  if (options.ownership !== "exclusive-stream" || !id(options.runId))
    throw new Error("CURSOR_STREAM_OWNERSHIP_REQUIRED");
  const diagnostics: CliStreamDiagnostics = {
    events: 0, malformed: 0, oversized: 0, dropped: 0,
    duplicateTools: 0, sessionMismatch: 0, terminal: false,
    source: "cursor-cli-stream-json", fidelity: "aggregate-run",
  };
  let session: string | undefined;
  let root: TurnHandle | undefined;
  let closed = false;
  let terminalOutput: string | undefined;
  let terminalError = false;
  const tools = new Map<string, { handle: ChildHandle; done: boolean }>();

  function accept(event: unknown): void {
    if (closed || diagnostics.events >= MAX_EVENTS) {
      diagnostics.dropped++; return;
    }
    diagnostics.events++;
    const value = record(event);
    if (!value) { diagnostics.malformed++; return; }
    if (!["system", "user", "assistant", "tool_call", "result"].includes(String(value.type))) return;
    const eventSession = id(value.session_id);
    if (!eventSession) { diagnostics.malformed++; return; }
    if (session && session !== eventSession) { diagnostics.sessionMismatch++; return; }
    if (diagnostics.terminal) { diagnostics.dropped++; return; }
    session = eventSession;
    if (!root) root = options.tracer.startTurn({
      sessionId: session,
      turnId: `cli-print-run:${options.runId}`,
      unit: "run", usagePlan: "unknown",
    });
    if (value.type === "result") {
      if (value.subtype !== "success" && value.is_error !== true) {
        diagnostics.malformed++; return;
      }
      diagnostics.terminal = true;
      terminalOutput = text(value.result);
      terminalError = value.is_error === true;
      // Wait for process close: a nonzero exit after result must not look successful.
      return;
    }
    if (value.type !== "tool_call") return;
    const callId = id(value.call_id);
    const call = record(value.tool_call);
    if (!callId || !call || !["started", "completed"].includes(String(value.subtype))) {
      diagnostics.malformed++; return;
    }
    const entries = Object.entries(call).filter(([key, payload]) =>
      (key.endsWith("ToolCall") || key === "function") && record(payload));
    if (entries.length !== 1) { diagnostics.malformed++; return; }
    const [type, raw] = entries[0];
    const detail = record(raw)!;
    let tool = tools.get(callId);
    if (tool?.done || (tool && value.subtype === "started")) {
      diagnostics.duplicateTools++; return;
    }
    if (!tool) {
      if (tools.size >= MAX_TOOLS) { diagnostics.dropped++; return; }
      tool = { handle: root.child({
        id: callId, toolCallId: callId, kind: "tool",
        name: type === "function" ? text(detail.name) : type,
        input: json(detail.args ?? detail.arguments), fidelity: "unknown",
      }), done: false };
      tools.set(callId, tool);
    }
    if (value.subtype === "completed") {
      const result = record(detail.result);
      const success = record(result?.success);
      const failed = toolResultFailed(detail.result) || result?.error !== undefined || result?.failure !== undefined ||
        detail.is_error === true || value.is_error === true ||
        (typeof success?.exitCode === "number" && success.exitCode !== 0) ||
        (typeof success?.exit_code === "number" && success.exit_code !== 0);
      tool.done = true;
      tool.handle.end({ output: json(detail.result), outcome: failed ? "error" : result ? "success" : "incomplete" });
    }
  }
  // Bound before decoding/parsing; a hostile or corrupted line cannot grow the
  // pending buffer. Dropping an oversized line does not desynchronize the next.
  const decoder = new StringDecoder("utf8");
  let pending = "";
  let pendingBytes = 0;
  let skipping = false;
  function push(chunk: Uint8Array): void {
    const input = Buffer.from(chunk.buffer, chunk.byteOffset, chunk.byteLength);
    let start = 0;
    for (let i = 0; i <= input.length; i++) {
      if (i < input.length && input[i] !== 10) continue;
      const part = input.subarray(start, i);
      if (!skipping) {
        pendingBytes += part.length;
        if (pendingBytes > MAX_LINE_BYTES) {
          diagnostics.oversized++; skipping = true; pending = ""; decoder.end();
        } else pending += decoder.write(part);
      }
      if (i < input.length) {
        if (!skipping) {
          pending += decoder.end();
          if (pending.trim()) {
            try { accept(JSON.parse(pending)); } catch { diagnostics.malformed++; }
          }
        }
        pending = ""; pendingBytes = 0; skipping = false;
      }
      start = i + 1;
    }
  }
  function finish(exitCode: number | null, signal?: string | null): void {
    if (closed) return;
    // NDJSON may end with one complete line lacking a newline; an incomplete
    // JSON tail is a diagnostic, never a fabricated successful terminal event.
    if (!skipping && pending.trim()) {
      try { accept(JSON.parse(pending + decoder.end())); } catch { diagnostics.malformed++; }
    }
    closed = true; pending = "";
    root?.end({
      output: terminalOutput,
      outcome: signal ? "interrupted" : terminalError || (exitCode !== null && exitCode !== 0)
        ? "error" : !diagnostics.terminal || diagnostics.malformed || diagnostics.oversized ||
          diagnostics.dropped || diagnostics.sessionMismatch ? "incomplete" : "success",
    });
    terminalOutput = undefined;
    tools.clear();
  }
  return { push, finish, diagnostics };
}
