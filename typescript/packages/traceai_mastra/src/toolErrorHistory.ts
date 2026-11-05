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
  pairs: Map<string, { calls: Set<string>; messages: Set<string> }>;
  saturated: boolean;
};

/**
 * Establish call provenance at native tool/step completion, not at the next
 * model request (a terminal tool step has none). New runtimes supply toolCallId;
 * the floor supplies it on the parent model_step's output.toolCalls instead.
 * Join that declaration by parent step, tool identity and arguments, in either
 * event order. Only fingerprints survive the synchronous capture.
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
      trace = { scopes: new Map(), pairs: new Map(), saturated: this.overflow };
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
    // Arguments are part of the join, not retained customer data. Normalize key
    // order because schema parsing can reorder a tool input's object properties.
    const key = (step: string, tool: string, input: unknown): string => fingerprint(JSON.stringify(
      [step, tool, input], (_key, value) => value && typeof value === "object" && !Array.isArray(value)
        ? Object.fromEntries(Object.entries(value).sort(([a], [b]) => a.localeCompare(b))) : value,
    ));
    const pair = (id: string) => {
      let entry = trace!.pairs.get(id);
      if (!entry && trace!.pairs.size < 128) {
        entry = { calls: new Set<string>(), messages: new Set<string>() };
        trace!.pairs.set(id, entry);
      }
      if (!entry) trace!.saturated = true;
      return entry;
    };
    try {
      const callId = (span.attributes as { toolCallId?: unknown } | undefined)?.toolCallId;
      if (/^(?:tool_call|mcp_tool_call|client_tool_call|provider_tool_call)$/.test(span.type)
        && typeof span.errorInfo?.message === "string") {
        const message = span.errorInfo.message;
        history.remember(span.traceId, message);
        if (typeof callId === "string") history.link(fingerprint(callId), fingerprint(message));
        else if (span.parentSpanId && native.entityId) {
          const entry = pair(key(span.parentSpanId, native.entityId, span.input));
          if (entry) {
            if (entry.messages.size < 128) entry.messages.add(fingerprint(message));
            else trace.saturated = true;
            for (const id of entry.calls) history.link(id, fingerprint(message));
          }
        }
      }
      if (span.type === "model_step") {
        const calls = (span.output as { toolCalls?: unknown } | undefined)?.toolCalls;
        if (Array.isArray(calls)) {
          for (const call of calls) {
            if (typeof call?.toolCallId !== "string" || typeof call.toolName !== "string") continue;
            const entry = pair(key(span.id, call.toolName, call.args));
            if (!entry) continue;
            if (entry.calls.size < 128) entry.calls.add(fingerprint(call.toolCallId));
            else trace.saturated = true;
            for (const message of entry.messages) history.link(fingerprint(call.toolCallId), message);
          }
        }
      }
    } catch {
      // Unserializable custom inputs must not break export or silently drop the
      // privacy boundary. Native framework inputs are bounded serializable views.
      trace.saturated = true;
    }
    if (trace.saturated) history.saturate();
  }

  matches(span: AnyExportedSpan, message: string, toolCallId?: string): boolean {
    const trace = this.traces.get(span.traceId);
    if (trace?.saturated || (!trace && this.overflow)) return true;
    const history = this.agents.get(this.scope(span));
    return history ? history.matches(span.traceId, message, toolCallId)
      : this.overflow || this.agents.size >= 128;
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
