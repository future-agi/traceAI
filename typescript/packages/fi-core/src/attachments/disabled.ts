import { AttachmentTicket } from "./admission";
import { AttachmentCorrelation } from "./correlation";
import { AttachmentManager } from "./manager";
import type {
  AttachmentAdmissionTicket, AttachmentCompletedRecordingRequest, AttachmentCompletion, AttachmentFlushResult, AttachmentHandle, AttachmentImportHandle,
  AttachmentImportOptions, AttachmentManagerOptions, AttachmentOptions, AttachmentOwnedFileRef, AttachmentOwnedRecordingHandle, AttachmentOwnedRecordingOptions,
  AttachmentPreflightOptions, AttachmentRecordingOptions, AttachmentRecordingWriter, AttachmentSource, AttachmentSpan, AttachmentStatus, AttachmentStream,
  AttachmentStreamOptions, AttachmentTarget,
} from "./types";

const DISABLED = Object.freeze({ status: "disabled" as const }) as AttachmentCompletion;
const DONE = Promise.resolve(DISABLED);
const EMPTY: AttachmentFlushResult = Object.freeze({ unfinished: 0, dropped: 0 });
const STATUS: AttachmentStatus = Object.freeze({ state: "disabled", queued: 0, active: 0, openTickets: 0, openStreams: 0, openRecordings: 0, ownedBytes: 0, dropped: 0 });
const HANDLE: AttachmentHandle = Object.freeze({ attachmentId: "00000000-0000-0000-0000-000000000000", occurrenceId: "00000000-0000-0000-0000-000000000000",
  admission: "disabled", completion: DONE });
const STREAM: AttachmentStream = Object.freeze({ reason: "disabled" as const, tryWrite: () => false, finish: () => HANDLE, abort: () => undefined });
const OWNED: AttachmentOwnedRecordingHandle = Object.freeze({ tier: "denied" as const, reason: "disabled" as const, completion: DONE, wait: () => DONE, cancel: () => undefined });
const IMPORT: AttachmentImportHandle = Object.freeze({ reason: "disabled" as const, completion: DONE, status: async () => ({ state: "failed", reason: "disabled" }) });
const RESULT = Object.freeze({ recordingId: "00000000-0000-0000-0000-000000000000", state: "denied" as const, reason: "disabled", segmentsPublished: 0, gapMs: 0,
  captureComplete: false, manifestRevision: 0 });
const WRITER: AttachmentRecordingWriter = Object.freeze({ recordingId: RESULT.recordingId, reason: "disabled" as const, tryWrite: () => false, markGap: () => undefined,
  markPlayout: () => undefined, finish: async () => RESULT, abort: () => undefined,
  status: () => Object.freeze({ state: "terminal" as const, reason: "disabled" as const, sealed: 0, uploaded: 0, gapMs: 0, manifestRevision: 0, ownedBytes: 0 }) });

/**
 * Attachments OFF (the default): the full public surface, every call returning immediately. It starts no timer,
 * socket, process listener, finalizer or bootstrap, writes no span attribute and never touches a source (design 8.1).
 */
export class DisabledAttachmentManager extends AttachmentManager {
  constructor(_options: AttachmentManagerOptions = {}) { super({ enabled: false, environment: () => ({}) }); }
  async ready(): Promise<boolean> { return false; }
  correlation(spanOrContext?: AttachmentSpan): AttachmentCorrelation { return new AttachmentCorrelation(spanOrContext); }
  preflight(_target: AttachmentTarget, _options: AttachmentPreflightOptions): AttachmentAdmissionTicket {
    return new AttachmentTicket({ commitTicket: () => HANDLE, commitStream: () => STREAM, releaseReservation: () => undefined, deniedHandle: () => HANDLE, deniedStream: () => STREAM },
      "disabled", 0, undefined, undefined, -1, 0);
  }
  attach(_target: AttachmentTarget, _source: AttachmentSource, _options: AttachmentOptions): AttachmentHandle { return HANDLE; }
  startAttachmentStream(_target: AttachmentTarget, _options: AttachmentStreamOptions): AttachmentStream { return STREAM; }
  startRecording(_target: AttachmentTarget, _options: AttachmentRecordingOptions): AttachmentRecordingWriter { return WRITER; }
  attachOwnedRecording(_target: AttachmentTarget, _source: { kind: "OwnedRecordingFile"; lease: AttachmentOwnedFileRef }, _options: AttachmentOwnedRecordingOptions): AttachmentOwnedRecordingHandle { return OWNED; }
  /** The resolver is NOT called. */
  async importCompletedRecording(_target: AttachmentTarget, _request: AttachmentCompletedRecordingRequest, _options: AttachmentImportOptions): Promise<AttachmentImportHandle> { return IMPORT; }
  updatePolicy(): void { /* can never turn a disabled manager on */ }
  invalidate(): void { /* nothing to revoke */ }
  async flush(): Promise<AttachmentFlushResult> { return EMPTY; }
  shutdown(): Promise<AttachmentFlushResult> { return Promise.resolve(EMPTY); }
  status(): AttachmentStatus { return STATUS; }
}
/** Same public surface whether attachments are on or off. */
export type AttachmentManagerLike = AttachmentManager;
