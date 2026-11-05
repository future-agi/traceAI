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

  matches(traceId: string, message: string): boolean {
    const failures = this.traces.get(traceId);
    return failures
      ? failures.saturated || failures.messages.has(fingerprint(message))
      : this.overflow;
  }

  forget(traceId: string): void { this.traces.delete(traceId); }

  clear(): void {
    this.traces.clear();
    this.overflow = false;
  }
}
