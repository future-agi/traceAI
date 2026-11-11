import type { AttachmentAdmission, AttachmentHandle, AttachmentStream } from "./types";

/** @internal Mutable state the manager keeps (and sweeps) for one consumed stream. */
export interface StreamState {
  reason: AttachmentAdmission;
  readonly attachmentId?: string;
  readonly occurrenceId?: string;
  chunks: Uint8Array[];
  length: number;
  readonly maxBytes: number;
  /** Bytes still reserved for this stream (the whole maxBytes until finish). */
  reserved: number;
  lastWrite: number;
  readonly idleTtl: number;
  readonly epoch: number;
  open: boolean;
  handle?: AttachmentHandle;
}
/** @internal */
export interface StreamHost {
  streamWrite(state: StreamState, chunk: Uint8Array): boolean;
  streamFinish(state: StreamState): AttachmentHandle;
  streamAbort(state: StreamState, reason: "incomplete" | "policy_denied" | "too_large"): void;
}
/**
 * Single-object upload of chunks the application ALREADY consumed (design section 6). The core never pulls, clones
 * or tees a stream; adapters forward fulfilled reads to tryWrite.
 */
export class AttachmentConsumedStream implements AttachmentStream {
  constructor(private readonly host: StreamHost | undefined, /** @internal */ readonly state: StreamState) {}
  get reason(): AttachmentAdmission { return this.state.reason; }
  get attachmentId(): string | undefined { return this.state.reason === "queued" || this.state.handle ? this.state.attachmentId : undefined; }
  get occurrenceId(): string | undefined { return this.state.reason === "queued" || this.state.handle ? this.state.occurrenceId : undefined; }
  tryWrite(chunk: Uint8Array): boolean {
    try { return !!this.host && this.state.open && this.host.streamWrite(this.state, chunk); } catch { return false; }
  }
  finish(): AttachmentHandle {
    if (!this.host) return this.state.handle!;
    return this.host.streamFinish(this.state);
  }
  abort(reason: "incomplete" | "policy_denied" = "incomplete"): void {
    try { if (this.host && this.state.open) this.host.streamAbort(this.state, reason === "policy_denied" ? "policy_denied" : "incomplete"); } catch { /* never throws */ }
  }
}
