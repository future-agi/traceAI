/**
 * Hand validators for every F01 shape the SDK sends or parses (design 9, T-TR-01). They mirror
 * contract.schema.json plus the cross-field rules of validate.py, with no schema engine in the runtime.
 */
import { validAttachmentManifest } from "./recording";
import { attachmentStrictJson } from "./transport-v1";
import type { AttachmentRecordingManifest } from "./types";

type V = (value: unknown) => boolean;
const obj = (v: unknown): v is Record<string, unknown> => !!v && typeof v === "object" && !Array.isArray(v);
const UUID: V = v => typeof v === "string" && v.length === 36 && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(v) && v !== "00000000-0000-0000-0000-000000000000";
const TRACE: V = v => (typeof v === "string" && /^[0-9a-f]{32}$/.test(v) && !/^0{32}$/.test(v)) || UUID(v);
const SPAN: V = v => typeof v === "string" && /^[0-9a-f]{16}$/.test(v) && !/^0{16}$/.test(v);
const TOKEN: V = v => typeof v === "string" && /^[a-zA-Z0-9_-]{1,128}$/.test(v);
const int = (min: number, max: number): V => v => typeof v === "number" && Number.isInteger(v) && v >= min && v <= max;
const SAFE = int(0, Number.MAX_SAFE_INTEGER);
const TIMESTAMP: V = v => typeof v === "string" && v.length <= 40 &&
  /^\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(\.\d+)?([Zz]|[+-]\d{2}:\d{2})$/.test(v) && Number.isFinite(Date.parse(v));
const oneOf = (...values: unknown[]): V => v => values.includes(v);
const DIRECTION = oneOf("input", "output", "mixed");
const MODALITY = oneOf("audio", "image", "video", "document", "file");
const AUDIO = ["audio/wav", "audio/ogg", "audio/mpeg", "audio/mp4", "audio/webm", "audio/flac", "audio/L16"];
const MIME_BY: Record<string, string[]> = { audio: AUDIO, image: ["image/png", "image/jpeg", "image/webp"], video: ["video/mp4", "video/webm"],
  document: ["application/pdf"], file: ["application/octet-stream"] };
const MIME = oneOf(...Object.values(MIME_BY).flat());
const PURPOSE = oneOf("conversation_recording", "turn_recording", "model_input", "model_output", "tool_result", "computer_screenshot", "diagnostic", "document");
const SOURCE = oneOf("Bytes", "Blob", "RemoteUrl", "LocalFile", "OwnedRecordingFile", "ProviderFile");
const STATUS = oneOf("pending_upload", "importing", "validating", "ready", "failed", "rejected", "expired", "deleted");
const REASON = oneOf("queued", "disabled", "bootstrap_pending", "policy_denied", "credential_scope_unsupported", "source_unavailable", "unsampled", "too_large",
  "queue_full", "unsupported_version", "endpoint_unconfigured", "scope_unresolved", "correlation_unavailable", "redaction_unavailable", "recording_disabled",
  "binding_pending", "not_found", "conflict", "throttled", "validation_failed", "expired", "deleted", "incomplete", "ready");
/** Closed object: every required key present, no unknown key, every present key valid. */
function closed(required: string[], properties: Record<string, V>): V {
  return v => obj(v) && required.every(key => Object.prototype.hasOwnProperty.call(v, key)) &&
    Object.keys(v).every(key => Object.prototype.hasOwnProperty.call(properties, key) && properties[key](v[key]));
}
const array = (item: V, min: number, max: number, unique = false): V => v => Array.isArray(v) && v.length >= min && v.length <= max && v.every(item) &&
  (!unique || new Set(v.map(x => JSON.stringify(x))).size === v.length);
const SCOPE = closed(["organization_id", "workspace_id", "project_id"], { organization_id: UUID, workspace_id: UUID, project_id: UUID });
const PARENT = closed(["trace_id", "span_id"], { trace_id: TRACE, span_id: SPAN });
const ASSOCIATION = closed(["capture_session_id", "correlation_nonce", "parent"], { capture_session_id: UUID, correlation_nonce: UUID, parent: PARENT });
const mimeMatches = (v: Record<string, unknown>) => (MIME_BY[v.modality as string] ?? []).includes(v.mime_type as string);
const REFERENCE_SHAPE = closed(["attachment_id", "occurrence_id", "direction", "modality", "mime_type"], { attachment_id: UUID, occurrence_id: UUID,
  direction: DIRECTION, modality: MODALITY, mime_type: MIME, purpose: PURPOSE, message_index: int(0, 65535), part_index: int(0, 65535),
  role: oneOf("user", "assistant", "tool", "system", "unknown"), channel: int(0, 31), conversation_id: TOKEN, turn_id: TOKEN, tool_call_id: TOKEN });
const REFERENCE: V = v => REFERENCE_SHAPE(v) && mimeMatches(v as Record<string, unknown>);
const REFERENCES: V = v => array(REFERENCE, 0, 64, true)(v) && new Set((v as { occurrence_id: string }[]).map(r => r.occurrence_id)).size === (v as unknown[]).length;
const UPLOAD_REQUEST_SHAPE = closed(["schema_version", "attachment_id", "occurrence_id", "association", "policy_version", "deletion_generation", "mime_type",
  "modality", "direction", "source_kind", "declared_bytes"], { schema_version: oneOf(1), attachment_id: UUID, occurrence_id: UUID, association: ASSOCIATION,
  policy_version: TOKEN, deletion_generation: SAFE, mime_type: MIME, modality: MODALITY, direction: DIRECTION, source_kind: SOURCE, purpose: PURPOSE,
  declared_bytes: int(1, 52428800), message_index: int(0, 65535), part_index: int(0, 65535), recording_id: UUID, segment_index: int(0, 65535),
  conversation_id: TOKEN, turn_id: TOKEN, tool_call_id: TOKEN });
const UPLOAD_REQUEST: V = v => {
  if (!UPLOAD_REQUEST_SHAPE(v)) return false;
  const r = v as Record<string, unknown>;
  if (("recording_id" in r) !== ("segment_index" in r)) return false;
  if (!mimeMatches(r)) return false;
  if (r.modality === "image" && (r.declared_bytes as number) > 20971520) return false;
  if ("recording_id" in r && (r.declared_bytes as number) > 8388608) return false;
  return true;
};
const POLICY = closed(["version", "enabled", "allowed_sources", "allowed_modalities", "require_media_redaction", "require_metadata_removal",
  "allow_unredacted_media_with_text_pii", "record_audio", "record_video"], { version: TOKEN, enabled: v => typeof v === "boolean",
  allowed_sources: array(SOURCE, 0, 6, true), allowed_modalities: array(MODALITY, 0, 5, true), require_media_redaction: v => typeof v === "boolean",
  require_metadata_removal: v => typeof v === "boolean", allow_unredacted_media_with_text_pii: v => typeof v === "boolean",
  record_audio: v => typeof v === "boolean", record_video: v => typeof v === "boolean" });
const LIMIT_MAX: Record<string, number> = { max_upload_bytes: 52428800, segment_bytes: 8388608, segment_duration_ms: 30000, recording_bytes: 1073741824,
  recording_duration_ms: 7200000, active_transfers: 16, active_recordings: 4, reserved_bytes: 1073741824, validation_jobs: 1024, initializations_per_minute: 120,
  links_per_minute: 1000, upload_bytes_per_second: 1073741824, playback_bytes_per_second: 1073741824, transfer_timeout_seconds: 600, grant_ttl_seconds: 300,
  revocation_check_seconds: 5 };
const LIMITS = closed(Object.keys(LIMIT_MAX), Object.fromEntries(Object.entries(LIMIT_MAX).map(([k, max]) => [k, int(1, max)])));
const IMPORT_SOURCE: V = v => closed(["kind", "envelope_id"], { kind: oneOf("RemoteUrlEnvelope"), envelope_id: UUID })(v) ||
  closed(["kind", "connection_id", "registered_output_id"], { kind: oneOf("ProviderFile"), connection_id: UUID, registered_output_id: UUID })(v);
const COMPLETED = closed(["schema_version", "association", "policy_version", "deletion_generation", "mime_type", "direction", "source_kind", "recording_id",
  "modality", "declared_bytes", "declared_duration_ms"], { schema_version: oneOf(1), association: ASSOCIATION, policy_version: TOKEN, deletion_generation: SAFE,
  mime_type: oneOf(...AUDIO), direction: DIRECTION, source_kind: oneOf("ProviderFile", "RemoteUrl"), recording_id: UUID, modality: oneOf("audio"),
  declared_bytes: int(1, 1073741824), declared_duration_ms: int(1, 7200000) });
const RECORDING_IMPORT: V = v => {
  if (!closed(["mode", "recording", "source"], { mode: oneOf("completed_recording"), recording: COMPLETED, source: IMPORT_SOURCE })(v)) return false;
  const r = v as { recording: { source_kind: string }; source: { kind: string } };
  return r.recording.source_kind === (r.source.kind === "RemoteUrlEnvelope" ? "RemoteUrl" : "ProviderFile");
};
const TRACK = closed(["track_id", "channels", "direction", "role"], { track_id: UUID, channels: array(int(0, 31), 1, 32, true), direction: DIRECTION,
  role: oneOf("user", "assistant", "mixed", "unknown") });
const MANIFEST_SHAPE = closed(["schema_version", "recording_id", "timebase", "sample_format", "sample_rate_hz", "channels", "codec", "container", "layout",
  "tracks", "segments", "gaps", "playout", "capture_complete", "duration_ms"], { schema_version: oneOf(1), recording_id: UUID, timebase: oneOf("session_relative_ms"),
  sample_format: oneOf("s16le", "s24le", "s32le", "f32le", "encoded"), sample_rate_hz: int(8000, 192000), channels: int(1, 32),
  codec: oneOf("pcm", "opus", "mp3", "aac", "flac"), container: oneOf("wav", "ogg", "mp3", "mp4", "webm", "flac", "raw"), layout: oneOf("mixed", "stereo", "separate_tracks"),
  tracks: array(TRACK, 1, 32), segments: array(closed(["occurrence_id", "index", "start_ms", "duration_ms"], { occurrence_id: UUID, index: int(0, 65535),
    start_ms: int(0, 7200000), duration_ms: int(1, 30000), track_id: UUID }), 0, 65536),
  gaps: array(closed(["start_ms", "duration_ms", "reason"], { start_ms: int(0, 7200000), duration_ms: int(1, 7200000),
    reason: oneOf("queue_full", "bootstrap_pending", "source_unavailable", "policy_denied", "truncated", "incomplete"), track_id: UUID }), 0, 65536),
  playout: array(closed(["start_ms", "duration_ms", "evidence"], { start_ms: int(0, 7200000), duration_ms: int(1, 7200000),
    evidence: oneOf("generated", "played", "unknown", "truncated") }), 0, 65536),
  capture_complete: v => typeof v === "boolean", duration_ms: int(0, 7200000) });
const SPAN_ATTRIBUTES: V = v => {
  if (!closed(["fi.attachment.schema_version", "fi.attachment.refs"], { "fi.attachment.schema_version": oneOf(1),
    "fi.attachment.refs": x => typeof x === "string" && x.length <= 16384, "fi.attachment.capture_session_id": UUID, "fi.attachment.correlation_nonce": UUID })(v)) return false;
  const a = v as Record<string, string>;
  if (("fi.attachment.capture_session_id" in a) !== ("fi.attachment.correlation_nonce" in a)) return false;
  if (new TextEncoder().encode(a["fi.attachment.refs"]).byteLength > 16384) return false;
  try { return REFERENCES(attachmentStrictJson(a["fi.attachment.refs"])); } catch { return false; }
};
const VALIDATORS: Record<string, V> = {
  BootstrapRequest: closed(["schema_versions", "project_name", "project_type", "capture_session_id", "correlation_nonce"], {
    schema_versions: array(int(1, 65535), 1, 8, true), project_name: v => typeof v === "string" && v.length >= 1 && v.length <= 255,
    project_type: v => typeof v === "string" && v.length >= 1 && v.length <= 64, workspace_id: UUID, capture_session_id: UUID, correlation_nonce: UUID }),
  BootstrapResponse: closed(["schema_version", "scope", "capture_session_id", "policy", "limits", "expires_at", "transfer_modes"], { schema_version: oneOf(1),
    scope: SCOPE, capture_session_id: UUID, policy: POLICY, limits: LIMITS, expires_at: TIMESTAMP,
    transfer_modes: v => Array.isArray(v) && v.length === 1 && v[0] === "authenticated_stream" }),
  UploadRequest: UPLOAD_REQUEST,
  UploadResponse: closed(["upload_id", "attachment_id", "occurrence_id", "state", "reserved_bytes", "expires_at", "transfer_mode", "write_capability"], {
    upload_id: UUID, attachment_id: UUID, occurrence_id: UUID, state: oneOf("pending_upload"), reserved_bytes: int(1, 52428800), expires_at: TIMESTAMP,
    transfer_mode: oneOf("authenticated_stream"), write_capability: v => typeof v === "string" && /^[a-zA-Z0-9_.-]{16,4096}$/.test(v) }),
  CompleteRequest: closed(["policy_version", "deletion_generation"], { policy_version: TOKEN, deletion_generation: SAFE }),
  StatusResponse: closed(["upload_id", "state", "reason"], { upload_id: UUID, state: STATUS, reason: REASON }),
  RecordingManifest: v => MANIFEST_SHAPE(v) && validAttachmentManifest(v as AttachmentRecordingManifest),
  RecordingImportRequest: RECORDING_IMPORT,
  RecordingImportResponse: closed(["mode", "job_id", "recording_id", "state"], { mode: oneOf("completed_recording"), job_id: UUID, recording_id: UUID, state: oneOf("importing") }),
  ImportStatusResponse: closed(["job_id", "state", "reason"], { job_id: UUID, state: STATUS, reason: REASON }),
  Error: closed(["code", "request_id"], { code: REASON, request_id: UUID }),
  SpanAttributes: SPAN_ATTRIBUTES,
  References: REFERENCES,
  Reference: REFERENCE,
};
/** Shapes this SDK sends or parses. */
export const ATTACHMENT_WIRE_SHAPES = Object.freeze(Object.keys(VALIDATORS));
export function validAttachmentWire(name: string, value: unknown): boolean {
  const validate = VALIDATORS[name];
  if (!validate) throw new TypeError("unsupported wire shape");
  try { return validate(value); } catch { return false; }
}
