import { createHash } from "node:crypto";
import type { AnyExportedSpan } from "@mastra/core/observability";

const fingerprint = (message: string): string => createHash("sha256").update(message).digest("hex");
type Failures = { messages: Set<string>; saturated: boolean };

/**
 * Old Mastra model_step requests flatten tool failures to ordinary strings, with
 * no isError bit or tool-call ID on the failed native tool span. Remember only
 * fingerprints of native failures, scoped to the trace, not the sensitive text.
 * Bounds also cover abandoned traces; overflow fails closed for ambiguous legacy
 * tool results rather than silently evicting provenance and exporting an error.
 */
export class ToolErrorHistory {
  private readonly traces = new Map<string, Failures>();
  private overflow = false;
  // A continuation has a new trace, but retains the tool-call IDs in native
  // response.messages. Learn ID + message pairs from the original trace's
  // request history, where even the oldest SDK includes the call ID. Do not
  // retain plaintext, or treat the same text from an unrelated call as failure.
  private readonly continuedCalls = new Set<string>();
  private continuedOverflow = false;

  remember(traceId: string, message: string): void {
    let failures = this.traces.get(traceId);
    if (!failures) {
      if (this.traces.size >= 1024) { this.overflow = true; return; }
      // Once an untracked trace may have lost a failure, a later free slot must
      // not incorrectly make that trace's incomplete fingerprint set trusted.
      failures = { messages: new Set(), saturated: this.overflow };
      this.traces.set(traceId, failures);
    }
    if (failures.messages.size >= 128) failures.saturated = true;
    else failures.messages.add(fingerprint(message));
  }

  matches(traceId: string, message: string, toolCallId?: string): boolean {
    const failures = this.traces.get(traceId);
    const failed = failures
      ? failures.saturated || failures.messages.has(fingerprint(message))
      : this.overflow;
    if (toolCallId === undefined) return failed;
    const call = fingerprint(JSON.stringify([fingerprint(toolCallId), fingerprint(message)]));
    if (failed) this.link(fingerprint(toolCallId), fingerprint(message));
    // Never evict a known error while the exporter can still accept a replay.
    // Saturation conservatively hides ambiguous legacy string tool results;
    // structured successful results and unrelated user content are unaffected.
    return failed || this.continuedOverflow || this.continuedCalls.has(call);
  }

  /** Exact native invocation identity; never learn an ID from matching text. */
  matchesCall(message: string, toolCallId: string): boolean {
    return this.continuedOverflow || this.continuedCalls.has(fingerprint(JSON.stringify(
      [fingerprint(toolCallId), fingerprint(message)],
    )));
  }

  saturate(): void { this.continuedOverflow = true; }

  /** Join native provenance without retaining the call ID or exception plaintext. */
  link(callFingerprint: string, messageFingerprint: string): void {
    const call = fingerprint(JSON.stringify([callFingerprint, messageFingerprint]));
    if (this.continuedCalls.size < 16384) this.continuedCalls.add(call);
    else if (!this.continuedCalls.has(call)) this.continuedOverflow = true;
  }

  forget(traceId: string): void { this.traces.delete(traceId); }

  clear(): void {
    this.traces.clear();
    this.overflow = false;
    this.continuedCalls.clear();
    this.continuedOverflow = false;
  }
}

type NativeTrace = {
  scopes: Map<string, string>;
  saturated: boolean;
  invocationIds: boolean;
};

/**
 * Establish call provenance from the native tool-error lifecycle before root
 * completion. New runtimes also supply toolCallId on the failed tool span.
 * Parsed span inputs and original model arguments are never invocation identity.
 * Only fingerprints survive synchronous capture.
 *
 * Each exporter owns this state. Agent entity identity scopes continuation, and
 * unknown ancestry stays trace-local rather than contaminating another agent.
 * Root completion releases the native joins; only bounded call/message hashes
 * survive flush. Shutdown clears everything. Overload fails closed for ambiguous
 * string results, without evicting remembered failures in other agent scopes.
 */
export class NativeToolErrorHistory {
  private readonly traces = new Map<string, NativeTrace>();
  private readonly agents = new Map<string, ToolErrorHistory>();
  private overflow = false;

  private history(scope: string): ToolErrorHistory | undefined {
    let history = this.agents.get(scope);
    if (!history && this.agents.size < 128) {
      history = new ToolErrorHistory();
      this.agents.set(scope, history);
    }
    return history;
  }

  private scope(span: AnyExportedSpan): string {
    return this.traces.get(span.traceId)?.scopes.get(span.id) ?? fingerprint(span.traceId);
  }

  capture(span: AnyExportedSpan, ended: boolean): void {
    let trace = this.traces.get(span.traceId);
    if (!trace) {
      if (this.traces.size >= 1024) { this.overflow = true; return; }
      trace = { scopes: new Map(), saturated: this.overflow, invocationIds: false };
      this.traces.set(span.traceId, trace);
    }
    const native = span as AnyExportedSpan & { entityId?: string };
    const scope = span.type === "agent_run" && native.entityId
      ? fingerprint(`agent:${native.entityId}`)
      : trace.scopes.get(span.id) ?? trace.scopes.get(span.parentSpanId ?? "") ?? fingerprint(span.traceId);
    if (trace.scopes.size < 1024 || trace.scopes.has(span.id)) trace.scopes.set(span.id, scope);
    else trace.saturated = true;
    if (!ended) return;
    const history = this.history(scope);
    if (!history) return;
    const callId = (span.attributes as { toolCallId?: unknown } | undefined)?.toolCallId;
    if (/^(?:tool_call|mcp_tool_call|client_tool_call|provider_tool_call)$/.test(span.type)
      && typeof span.errorInfo?.message === "string") {
      const message = span.errorInfo.message;
      history.remember(span.traceId, message);
      if (typeof callId === "string") {
        trace.invocationIds = true;
        history.link(fingerprint(callId), fingerprint(message));
      }
    }
    if (trace.saturated) history.saturate();
  }

  /** The emitting SDK pairs the call ID with its failure before schema views diverge. */
  captureInvocation(span: AnyExportedSpan, payload: unknown): void {
    const error = payload as { toolCallId?: unknown; error?: { message?: unknown } } | undefined;
    if (typeof error?.toolCallId !== "string" || typeof error.error?.message !== "string") return;
    this.capture(span, false);
    const trace = this.traces.get(span.traceId);
    if (trace) trace.invocationIds = true;
    const history = this.history(this.scope(span));
    history?.remember(span.traceId, error.error.message);
    history?.link(fingerprint(error.toolCallId), fingerprint(error.error.message));
  }

  matches(span: AnyExportedSpan, message: string, toolCallId?: string): boolean {
    const trace = this.traces.get(span.traceId);
    if (trace?.saturated || (!trace && this.overflow)) return true;
    const scope = this.scope(span);
    const history = this.agents.get(scope);
    if (!history) return this.overflow || this.agents.size >= 128;
    // Preserve the existing within-trace safety for manually emitted spans with
    // neither agent nor invocation identity. Never infer continuation IDs from
    // text: this anonymous scope is discarded at root completion. Real native
    // invocations always use exact ID/message pairs, including in the first turn.
    const anonymousLegacy = scope === fingerprint(span.traceId) && !trace?.invocationIds;
    return toolCallId === undefined || anonymousLegacy ? history.matches(span.traceId, message)
      : history.matchesCall(message, toolCallId);
  }

  forget(traceId: string): void {
    this.traces.delete(traceId);
    for (const history of this.agents.values()) history.forget(traceId);
    // Spans without an identifiable agent cannot establish cross-trace scope.
    this.agents.delete(fingerprint(traceId));
  }

  clear(): void {
    this.traces.clear();
    this.agents.clear();
    this.overflow = false;
  }
}
