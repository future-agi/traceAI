import type { Span, SpanContext } from "@opentelemetry/api";
import type { TraceConfigOptions } from "../trace/trace-config/types";
import type { AttachmentCorrelation } from "./correlation";

export type AttachmentDirection = "input" | "output" | "mixed";
export type AttachmentModality = "audio" | "image" | "video" | "document" | "file";
export type AttachmentPurpose = "conversation_recording" | "turn_recording" | "model_input" | "model_output" | "tool_result" | "computer_screenshot" | "diagnostic" | "document";
export type AttachmentSourceKind = "Bytes" | "Blob" | "RemoteUrl" | "LocalFile" | "OwnedRecordingFile" | "ProviderFile";
/** No source is inferred from a string. Unsupported retrieval sources fail closed. */
export type AttachmentSource =
  | { kind: "Bytes"; data: Uint8Array }
  | { kind: "Blob"; data: Blob }
  | { kind: "RemoteUrl"; url: string }
  | { kind: "LocalFile"; path: string }
  | { kind: "OwnedRecordingFile"; lease: AttachmentOwnedFileRef }
  | { kind: "ProviderFile"; provider: string; fileId: string };
export type AttachmentAdmission = "queued" | "disabled" | "bootstrap_pending" | "policy_denied" | "credential_scope_unsupported" | "source_unavailable" | "unsampled" | "too_large" | "queue_full" | "unsupported_version" | "endpoint_unconfigured" | "scope_unresolved" | "correlation_unavailable" | "redaction_unavailable" | "recording_disabled" | "expired" | "deleted" | "incomplete";
/** Terminal transfer outcomes (design 0.3). `validating` means transferred, NOT readable. `importing` is an accepted import job. */
export type AttachmentCompletionStatus = "validating" | "importing" | "incomplete" | "expired" | "deleted" | "policy_denied" | "too_large" | "throttled" |
  "conflict" | "not_found" | "credential_scope_unsupported" | "validation_failed" | "unsupported_version" | "source_unavailable" | "redaction_unavailable";
/** `failed` is retired (design 0.3): it is never produced and stays in the type only for source compatibility. */
export type AttachmentCompletion = Readonly<{ status: AttachmentAdmission | AttachmentCompletionStatus | "failed" }>;
export interface AttachmentHandle {
  readonly attachmentId: string;
  readonly occurrenceId: string;
  readonly admission: AttachmentAdmission;
  /** Never rejects; validating is transferred, NOT readable. Use flush/shutdown for process-exit grace. */
  readonly completion: Promise<AttachmentCompletion>;
}
/** Semantic ancestry can only add restrictions, never remove a directional restriction. */
export interface AttachmentCapturePolicy {
  captureContent?: boolean;
  recordInputs?: boolean;
  recordOutputs?: boolean;
  muted?: boolean;
  temporaryChat?: boolean;
  requested?: boolean;
  modelInput?: boolean;
  containsInput?: boolean;
  containsOutput?: boolean;
  errorDetail?: boolean;
  newRecording?: boolean;
}
export interface AttachmentOptions {
  mimeType: string;
  direction: AttachmentDirection;
  purpose?: AttachmentPurpose;
  conversationId?: string;
  turnId?: string;
  toolCallId?: string;
  messageIndex?: number;
  partIndex?: number;
  role?: "user" | "assistant" | "tool" | "system" | "unknown";
  channel?: number;
  capture?: AttachmentCapturePolicy;
}
export interface AttachmentConfig {
  enabled?: boolean;
  recordAudio?: boolean;
  recordVideo?: boolean;
  allowUnredactedMediaWithTextPii?: boolean;
  /** Trusted control-plane origin, never inferred from the OTLP or source URL. */
  endpoint?: string;
  maxDescriptors?: number;
  maxOwnedBytes?: number;
  workers?: number;
  requestTimeoutMs?: number;
  shutdownTimeoutMs?: number;
  /** One content PUT (default 120 s), capped by the negotiated transfer_timeout_seconds. */
  contentTimeoutMs?: number;
  /** One manifest PUT (default 10 s, max 30 s). */
  manifestTimeoutMs?: number;
  /** Largest mutable buffer copied synchronously inside a caller's call (default 8 MiB, max 32 MiB). */
  syncCopyBytes?: number;
  /** Unused preflight ticket lifetime (default 10 s, max 30 s). */
  ticketTtlMs?: number;
  /** Consumed-stream idle lifetime (default 60 s, max 300 s). */
  streamIdleTtlMs?: number;
  /** Session-end grace for adapter finalization (default and max 30 s). */
  finalizationTimeoutMs?: number;
}
export interface AttachmentReference {
  attachment_id: string;
  occurrence_id: string;
  direction: AttachmentDirection;
  modality: AttachmentModality;
  mime_type: string;
  purpose?: AttachmentPurpose;
  message_index?: number;
  part_index?: number;
  role?: AttachmentOptions["role"];
  channel?: number;
  conversation_id?: string;
  turn_id?: string;
  tool_call_id?: string;
}
export interface AttachmentServerPolicy {
  version: string;
  enabled: boolean;
  allowed_sources: AttachmentSourceKind[];
  allowed_modalities: AttachmentModality[];
  require_media_redaction: boolean;
  require_metadata_removal: boolean;
  allow_unredacted_media_with_text_pii: boolean;
  record_audio: boolean;
  record_video: boolean;
}
export interface AttachmentSession {
  schema_version: 1;
  scope: { organization_id: string; workspace_id: string; project_id: string };
  capture_session_id: string;
  policy: AttachmentServerPolicy;
  limits: { max_upload_bytes: number; [name: string]: number };
  expires_at: string;
  transfer_modes: ["authenticated_stream"];
}
export interface AttachmentBootstrap {
  schema_versions: [1];
  project_name: string;
  project_type: string;
  workspace_id?: string;
  capture_session_id: string;
  correlation_nonce: string;
}
export interface AttachmentUploadRequest {
  schema_version: 1;
  attachment_id: string;
  occurrence_id: string;
  association: { capture_session_id: string; correlation_nonce: string; parent: { trace_id: string; span_id: string } };
  policy_version: string;
  deletion_generation: number;
  mime_type: string;
  modality: AttachmentModality;
  direction: AttachmentDirection;
  source_kind: AttachmentSourceKind;
  declared_bytes: number;
  purpose?: AttachmentPurpose;
  message_index?: number;
  part_index?: number;
  recording_id?: string;
  segment_index?: number;
  conversation_id?: string;
  turn_id?: string;
  tool_call_id?: string;
}
export interface AttachmentUpload {
  upload_id: string;
  attachment_id: string;
  occurrence_id: string;
  state: "pending_upload";
  reserved_bytes: number;
  expires_at: string;
  transfer_mode: "authenticated_stream";
  write_capability: string;
}
/** Transport implementations must abort promptly, bound responses and suppress instrumentation. */
export interface AttachmentTransport {
  bootstrap(request: AttachmentBootstrap, signal: AbortSignal): Promise<AttachmentSession>;
  initialize(session: AttachmentSession, request: AttachmentUploadRequest, signal: AbortSignal): Promise<AttachmentUpload>;
  upload(session: AttachmentSession, upload: AttachmentUpload, data: Uint8Array | Blob, signal: AbortSignal): Promise<void>;
  complete(session: AttachmentSession, upload: AttachmentUpload, request: AttachmentUploadRequest, signal: AbortSignal): Promise<void>;
}
export interface AttachmentManagerOptions extends AttachmentConfig {
  projectName?: string;
  projectType?: string;
  workspaceId?: string;
  traceConfig?: TraceConfigOptions;
  /** Node uses process.env via the provider; browsers do not inspect any ambient credentials. */
  environment?: () => Readonly<Record<string, string | undefined>>;
  /** The legacy four-method shape or the full versioned wire surface (design section 9). */
  transport?: AttachmentTransport | AttachmentTransportV1;
  /** @internal Test clock. `now` is monotonic milliseconds; `wall` is epoch milliseconds. */
  clock?: AttachmentClock;
}
export interface AttachmentFlushResult { readonly unfinished: number; readonly dropped: number }
export type AttachmentSpan = Span | SpanContext;

// ---------------------------------------------------------------------------
// Design sections 0-9 (sdk-streaming-design.md). Additive; earlier names are unchanged.
// ---------------------------------------------------------------------------
export interface AttachmentClock { now(): number; wall(): number }
/** Runtime state (design 0.2). `deleted` and `policy_denied` remain only for an explicit `invalidate()`. */
export type AttachmentRuntimeState = "disabled" | "endpoint_unconfigured" | "credential_scope_unsupported" | "scope_unresolved" | "bootstrap_pending" |
  "ready" | "expired" | "unsupported_version" | "incomplete" | "deleted" | "policy_denied" | "source_unavailable";
export interface AttachmentStatus {
  readonly state: AttachmentRuntimeState;
  readonly queued: number; readonly active: number;
  readonly openTickets: number; readonly openStreams: number; readonly openRecordings: number;
  readonly ownedBytes: number; readonly dropped: number;
}
export type AttachmentOperation = "observe_consumed" | "import_existing" | "create_recording";
export type AttachmentRole = NonNullable<AttachmentOptions["role"]>;
export type AttachmentTarget = AttachmentSpan | AttachmentCorrelation | undefined;
export type AttachmentGapReason = "queue_full" | "bootstrap_pending" | "source_unavailable" | "policy_denied" | "truncated" | "incomplete";
export interface AttachmentPreflightOptions {
  mimeType: string;
  direction: AttachmentDirection;
  /** Upper bound on the bytes the ticket may later carry; > 0. */
  maxBytes: number;
  operation?: AttachmentOperation;
  sourceKind?: AttachmentSourceKind;
  purpose?: AttachmentPurpose;
  capture?: AttachmentCapturePolicy;
  ttlMs?: number;
}
export interface AttachmentFields {
  messageIndex?: number; partIndex?: number; role?: AttachmentRole; channel?: number;
  conversationId?: string; turnId?: string; toolCallId?: string;
}
export interface AttachmentStreamOptions extends AttachmentFields {
  mimeType: string; direction: AttachmentDirection; maxBytes: number;
  purpose?: AttachmentPurpose; capture?: AttachmentCapturePolicy; idleTtlMs?: number;
}
export interface AttachmentStream {
  readonly reason: AttachmentAdmission; readonly attachmentId?: string; readonly occurrenceId?: string;
  /** Synchronous, never throws, does no I/O; copies the chunk. */
  tryWrite(chunk: Uint8Array): boolean;
  /** Non-blocking: enqueues the single upload. */
  finish(): AttachmentHandle;
  abort(reason?: "incomplete" | "policy_denied"): void;
}
export interface AttachmentAdmissionTicket {
  readonly admitted: boolean;
  readonly reason: AttachmentAdmission;
  readonly maxBytes: number;
  readonly correlation?: AttachmentCorrelation;
  attach(source: Extract<AttachmentSource, { kind: "Bytes" | "Blob" | "OwnedRecordingFile" }>, fields?: AttachmentFields): AttachmentHandle;
  startStream(options: AttachmentStreamOptions): AttachmentStream;
  release(): void;
}
export interface AttachmentPcmFormat { sampleRateHz: number; sampleFormat?: "s16le" }
export interface AttachmentTrackSpec { channels: readonly number[]; direction: AttachmentDirection; role: "user" | "assistant" | "mixed" | "unknown" }
export interface AttachmentRecordingOptions {
  format: AttachmentPcmFormat;
  layout: "mixed" | "stereo" | "separate_tracks";
  tracks: readonly AttachmentTrackSpec[];
  operation: "observe_consumed" | "create_recording";
  purpose?: "conversation_recording" | "turn_recording";
  capture?: AttachmentCapturePolicy;
  conversationId?: string; turnId?: string;
  windowSegments?: number;
}
export interface AttachmentRecordingStatus {
  readonly state: "pending" | "recording" | "finishing" | "terminal";
  readonly reason: AttachmentAdmission | AttachmentCompletionStatus;
  readonly sealed: number; readonly uploaded: number; readonly gapMs: number; readonly manifestRevision: number; readonly ownedBytes: number;
}
export interface AttachmentRecordingResult {
  readonly recordingId: string;
  readonly state: "published" | "partial" | "unpublished" | "denied";
  readonly reason?: string;
  readonly segmentsPublished: number;
  readonly gapMs: number;
  readonly captureComplete: boolean;
  /** 0 = never published. */
  readonly manifestRevision: number;
}
export interface AttachmentRecordingWriter {
  /** Client UUID; never projected on spans. */
  readonly recordingId: string;
  readonly reason: AttachmentAdmission;
  tryWrite(pcm: Uint8Array, track?: number): boolean;
  markGap(durationMs: number, reason?: AttachmentGapReason, track?: number): void;
  markPlayout(startMs: number, durationMs: number, evidence: "generated" | "played" | "unknown" | "truncated"): void;
  /** Bounded; never rejects. */
  finish(timeoutMs?: number): Promise<AttachmentRecordingResult>;
  abort(): void;
  status(): AttachmentRecordingStatus;
}
/** Opaque file reference minted only by a recording file scope from this core copy. */
declare const attachmentOwnedFileBrand: unique symbol;
export interface AttachmentOwnedFileRef { readonly [attachmentOwnedFileBrand]: true }
export interface AttachmentFileIdentity { dev: bigint; ino: bigint; size: number }
export interface AttachmentRecordingFileScope {
  identify(relativePath: string): Promise<AttachmentFileIdentity>;
  source(relativePath: string, identity: AttachmentFileIdentity): { kind: "OwnedRecordingFile"; lease: AttachmentOwnedFileRef };
  close(): Promise<void>;
}
export interface AttachmentOwnedRecordingOptions {
  mimeType: string; direction: AttachmentDirection; wavTracks?: readonly AttachmentTrackSpec[];
  purpose?: "conversation_recording" | "turn_recording"; capture?: AttachmentCapturePolicy; conversationId?: string;
}
export interface AttachmentOwnedRecordingHandle {
  /** Provisional for WAV until its header is read after admission. */
  readonly tier: "single" | "wav_recording" | "denied";
  readonly reason: AttachmentAdmission;
  readonly recordingId?: string;
  readonly completion: Promise<AttachmentCompletion>;
  /** Bounded wait; resolves `incomplete` at the deadline (and cancels). */
  wait(timeoutMs?: number): Promise<AttachmentCompletion>;
  cancel(): void;
}
export interface AttachmentCompletedRecordingRequest { provider: string; providerRef: string; direction?: AttachmentDirection }
export type AttachmentImportSourceRef = { kind: "ProviderFile"; connectionId: string; registeredOutputId: string } | { kind: "RemoteUrlEnvelope"; envelopeId: string };
export interface AttachmentResolvedCompletedRecording { source: AttachmentImportSourceRef; declaredBytes: number; declaredDurationMs: number; mimeType: string; direction: AttachmentDirection }
export interface AttachmentCompletedRecordingResolver {
  resolve(request: AttachmentCompletedRecordingRequest, signal: AbortSignal): Promise<AttachmentResolvedCompletedRecording | AttachmentAdmission>;
}
export interface AttachmentImportOptions { resolver: AttachmentCompletedRecordingResolver; capture?: AttachmentCapturePolicy; timeoutMs?: number }
export interface AttachmentImportHandle {
  readonly reason: AttachmentAdmission | AttachmentCompletionStatus;
  readonly recordingId?: string;
  readonly jobId?: string;
  readonly completion: Promise<AttachmentCompletion>;
  /** Exactly one status GET per call. */
  status(timeoutMs?: number): Promise<{ state: string; reason: string }>;
}
export interface AttachmentImportJob { jobId: string; recordingId: string; state: "importing" }
/** In-memory bytes, an immutable Blob, or a bounded range reader (owned files, WAV ranges). */
export type AttachmentContentSource = Uint8Array | Blob | { readonly size: number; read(offset: number, size: number): Promise<Uint8Array>; check(): Promise<void> };
export interface AttachmentPacer { take(bytes: number, signal: AbortSignal): Promise<void> }
/** The ONE versioned wire surface (design section 9). Every HTTP shape stays behind this. */
export interface AttachmentTransportV1 {
  bootstrap(body: AttachmentBootstrap, key: string, signal: AbortSignal): Promise<AttachmentSession>;
  renewSession(current: AttachmentSession, sequence: number, signal: AbortSignal): Promise<AttachmentSession>;
  initialize(session: AttachmentSession, body: string, key: string, signal: AbortSignal): Promise<AttachmentUpload>;
  renewUpload(session: AttachmentSession, upload: AttachmentUpload, body: string, signal: AbortSignal): Promise<AttachmentUpload>;
  putContent(session: AttachmentSession, upload: AttachmentUpload, source: AttachmentContentSource, signal: AbortSignal, pacer: AttachmentPacer): Promise<void>;
  complete(session: AttachmentSession, upload: AttachmentUpload, key: string, signal: AbortSignal): Promise<"validating">;
  abort(session: AttachmentSession, upload: AttachmentUpload, key: string, signal: AbortSignal): Promise<void>;
  uploadStatus(session: AttachmentSession, upload: AttachmentUpload, signal: AbortSignal): Promise<{ state: string; reason: string }>;
  putManifest(session: AttachmentSession, capability: string, recordingId: string, body: string, key: string, signal: AbortSignal): Promise<void>;
  submitImport(session: AttachmentSession, body: string, key: string, signal: AbortSignal): Promise<AttachmentImportJob>;
  importStatus(session: AttachmentSession, jobId: string, signal: AbortSignal): Promise<{ state: string; reason: string }>;
}
export interface AttachmentRecordingTrack { track_id: string; channels: number[]; direction: AttachmentDirection; role: AttachmentTrackSpec["role"] }
export interface AttachmentRecordingManifest {
  schema_version: 1; recording_id: string; timebase: "session_relative_ms";
  sample_format: "s16le"; sample_rate_hz: number; channels: number; codec: "pcm"; container: "wav";
  layout: AttachmentRecordingOptions["layout"]; tracks: AttachmentRecordingTrack[];
  segments: { occurrence_id: string; index: number; start_ms: number; duration_ms: number; track_id?: string }[];
  gaps: { start_ms: number; duration_ms: number; reason: AttachmentGapReason; track_id?: string }[];
  playout: { start_ms: number; duration_ms: number; evidence: "generated" | "played" | "unknown" | "truncated" }[];
  capture_complete: boolean; duration_ms: number;
}
