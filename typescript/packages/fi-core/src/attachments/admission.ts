import type { AttachmentCorrelation } from "./correlation";
import type { AdmissionFacts } from "./internal";
import type {
  AttachmentAdmission, AttachmentAdmissionTicket, AttachmentFields, AttachmentHandle, AttachmentSource, AttachmentStream, AttachmentStreamOptions,
} from "./types";

/** @internal */
export interface TicketHost {
  commitTicket(ticket: AttachmentTicket, source: Extract<AttachmentSource, { kind: "Bytes" | "Blob" | "OwnedRecordingFile" }>, fields: AttachmentFields): AttachmentHandle;
  commitStream(ticket: AttachmentTicket, options: AttachmentStreamOptions): AttachmentStream;
  releaseReservation(id: number): void;
  deniedHandle(reason: AttachmentAdmission): AttachmentHandle;
  deniedStream(reason: AttachmentAdmission): AttachmentStream;
}
/**
 * Single-use preflight ticket (design 1.5). It holds a reservation id only, never the source; the manager tracks the
 * reservation by id so an abandoned ticket can be collected and its budget returned by a finalizer.
 */
export class AttachmentTicket implements AttachmentAdmissionTicket {
  /** @internal */ used = false;
  constructor(
    private readonly host: TicketHost,
    readonly reason: AttachmentAdmission,
    readonly maxBytes: number,
    readonly correlation: AttachmentCorrelation | undefined,
    /** @internal */ readonly facts: AdmissionFacts | undefined,
    /** @internal */ readonly reservation: number,
    /** @internal */ readonly expiresAt: number,
  ) {}
  get admitted(): boolean { return this.reason === "queued"; }
  attach(source: Extract<AttachmentSource, { kind: "Bytes" | "Blob" | "OwnedRecordingFile" }>, fields: AttachmentFields = {}): AttachmentHandle {
    try {
      if (!this.admitted) return this.host.deniedHandle(this.reason);
      if (this.used) return this.host.deniedHandle("conflict" as AttachmentAdmission);
      return this.host.commitTicket(this, source, fields ?? {});
    } catch { return this.host.deniedHandle("source_unavailable"); }
  }
  startStream(options: AttachmentStreamOptions): AttachmentStream {
    try {
      if (!this.admitted) return this.host.deniedStream(this.reason);
      if (this.used) return this.host.deniedStream("conflict" as AttachmentAdmission);
      return this.host.commitStream(this, options);
    } catch { return this.host.deniedStream("source_unavailable"); }
  }
  release(): void {
    if (this.used || !this.admitted) return;
    this.used = true;
    this.host.releaseReservation(this.reservation);
  }
}
