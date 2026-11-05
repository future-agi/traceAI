import { createHash } from "node:crypto";

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
    const call = fingerprint(JSON.stringify([toolCallId, message]));
    if (failed) {
      if (this.continuedCalls.size < 16384) this.continuedCalls.add(call);
      else if (!this.continuedCalls.has(call)) this.continuedOverflow = true;
    }
    // Never evict a known error while the exporter can still accept a replay.
    // Saturation conservatively hides ambiguous legacy string tool results;
    // structured successful results and unrelated user content are unaffected.
    return failed || this.continuedOverflow || this.continuedCalls.has(call);
  }

  forget(traceId: string): void { this.traces.delete(traceId); }

  clear(): void {
    this.traces.clear();
    this.overflow = false;
    this.continuedCalls.clear();
    this.continuedOverflow = false;
  }
}
