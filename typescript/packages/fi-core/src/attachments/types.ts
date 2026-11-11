import type { Span, SpanContext } from "@opentelemetry/api";
import type { TraceConfigOptions } from "../trace/trace-config/types";

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
  | { kind: "OwnedRecordingFile"; lease: unknown }
  | { kind: "ProviderFile"; provider: string; fileId: string };
export type AttachmentAdmission = "queued" | "disabled" | "bootstrap_pending" | "policy_denied" | "credential_scope_unsupported" | "source_unavailable" | "unsampled" | "too_large" | "queue_full" | "unsupported_version" | "endpoint_unconfigured" | "scope_unresolved" | "correlation_unavailable" | "redaction_unavailable" | "recording_disabled" | "expired" | "deleted" | "incomplete";
export type AttachmentCompletion = Readonly<{ status: AttachmentAdmission | "validating" | "failed" | "deleted" }>;
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
  transport?: AttachmentTransport;
}
export interface AttachmentFlushResult { readonly unfinished: number; readonly dropped: number }
export type AttachmentSpan = Span | SpanContext;
