/** @internal Contracts between the manager and its ticket, stream, recording and import helpers. Not public API. */
import type {
  AttachmentAdmission, AttachmentCapturePolicy, AttachmentClock, AttachmentCompletion, AttachmentContentSource, AttachmentDirection,
  AttachmentOperation, AttachmentPurpose, AttachmentRuntimeState, AttachmentSession, AttachmentSourceKind, AttachmentUpload,
} from "./types";

export interface AdmissionFacts {
  mimeType: string;
  direction: AttachmentDirection;
  purpose?: AttachmentPurpose;
  capture?: AttachmentCapturePolicy;
  operation: AttachmentOperation;
  sourceKind: AttachmentSourceKind;
  /** Only the completed-import path may name ProviderFile / RemoteUrl (design 1.4 step 8). */
  importSource?: boolean;
}
export interface CoreJob {
  readonly lane: string;
  /** Owned bytes released by the manager when this job ends. */
  bytes: number;
  readonly controller: AbortController;
  run(signal: AbortSignal): Promise<void>;
  /** Re-evaluates admission against the current policy epoch; a reason drops the job. */
  denied(): AttachmentAdmission | undefined;
  fail(reason: AttachmentCompletion["status"]): void;
  settled?: boolean;
}
export interface UploadSpec {
  /** Serialized once at admission; replays are byte-identical. */
  body: string;
  occurrenceId: string;
  declaredBytes: number;
  source: AttachmentContentSource;
  recheck(): AttachmentAdmission | undefined;
  /** Recording segments under BR-7 fallback PUT the manifest between content and complete. */
  beforeComplete?(outcome: UploadOutcome, signal: AbortSignal): Promise<void>;
}
export interface UploadOutcome {
  status: AttachmentCompletion["status"];
  upload?: AttachmentUpload;
  body: string;
  renewals: number;
}
export interface AttachmentHost {
  readonly clock: AttachmentClock;
  readonly sessionId: string;
  readonly nonce: string;
  readonly chunkBytes: number;
  readonly manifestTimeoutMs: number;
  readonly finalizationTimeoutMs: number;
  currentSession(): AttachmentSession | undefined;
  runtimeState(): AttachmentRuntimeState;
  policyEpoch(): number;
  stopping(): boolean;
  recheck(facts: AdmissionFacts, options?: { ignoreSessionExpiry?: boolean }): AttachmentAdmission | undefined;
  reserveBytes(bytes: number): boolean;
  releaseBytes(bytes: number): void;
  enqueue(job: CoreJob): void;
  runUpload(spec: UploadSpec, signal: AbortSignal): Promise<UploadOutcome>;
  renewUpload(outcome: UploadOutcome, signal: AbortSignal): Promise<boolean>;
  putManifest(capability: string, recordingId: string, body: string, key: string, signal: AbortSignal, replay: boolean): Promise<AttachmentCompletion["status"] | "ok">;
  recordingClosed(id: string): void;
  /** Schedules a deferred session-renewal check (no I/O on the caller's stack); recording writes call it (design 3.1). */
  touch(): void;
  countDrop(): void;
  notify(): void;
}
