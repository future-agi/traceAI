import { validAttachmentUuid } from "./correlation";
import type {
  AttachmentAdmission, AttachmentCompletedRecordingRequest, AttachmentCompletedRecordingResolver, AttachmentDirection, AttachmentResolvedCompletedRecording,
} from "./types";

const AUDIO = ["audio/wav", "audio/ogg", "audio/mpeg", "audio/mp4", "audio/webm", "audio/flac", "audio/L16"];
const TOKEN = /^[a-zA-Z0-9_-]{1,128}$/;
const RESOLVER_REASONS: readonly AttachmentAdmission[] = ["source_unavailable", "policy_denied", "credential_scope_unsupported", "too_large"];

/** Core default: the core never talks to providers (design 5.2). */
export class AttachmentUnavailableResolver implements AttachmentCompletedRecordingResolver {
  async resolve(): Promise<AttachmentAdmission> { return "source_unavailable"; }
}
export function validAttachmentImportRequest(request: AttachmentCompletedRecordingRequest): boolean {
  return !!request && typeof request.provider === "string" && TOKEN.test(request.provider) && typeof request.providerRef === "string" &&
    TOKEN.test(request.providerRef) && (request.direction === undefined || ["input", "output", "mixed"].includes(request.direction));
}
/** Resolved direction must be at least as inclusive as the request; `mixed` wins (trusted ancestry). */
export function attachmentImportDirection(requested: AttachmentDirection, resolved: AttachmentDirection): AttachmentDirection | undefined {
  if (resolved === requested) return resolved;
  if (resolved === "mixed") return "mixed";
  return undefined;
}
/** Validates a resolver result (design 5.3 step 3). Returns a reason, or undefined when valid. */
export function attachmentResolvedRecordingDenial(value: unknown, limits: { recording_bytes?: number; recording_duration_ms?: number } | undefined): AttachmentAdmission | "unsupported_version" | undefined {
  if (typeof value === "string") return RESOLVER_REASONS.includes(value as AttachmentAdmission) ? value as AttachmentAdmission : "source_unavailable";
  const r = value as AttachmentResolvedCompletedRecording;
  if (!r || typeof r !== "object" || !r.source || typeof r.source !== "object") return "unsupported_version";
  const s = r.source as Record<string, unknown>;
  if (s.kind === "ProviderFile") {
    if (Object.keys(s).length !== 3 || !validAttachmentUuid(s.connectionId) || !validAttachmentUuid(s.registeredOutputId)) return "unsupported_version";
  } else if (s.kind === "RemoteUrlEnvelope") {
    if (Object.keys(s).length !== 2 || !validAttachmentUuid(s.envelopeId)) return "unsupported_version";
  } else return "unsupported_version";
  if (!AUDIO.includes(r.mimeType) || !["input", "output", "mixed"].includes(r.direction)) return "unsupported_version";
  if (!Number.isInteger(r.declaredBytes) || !Number.isInteger(r.declaredDurationMs)) return "unsupported_version";
  const maxBytes = Math.min(limits?.recording_bytes ?? 1073741824, 1073741824);
  const maxMs = Math.min(limits?.recording_duration_ms ?? 7200000, 7200000);
  // Never truncated: an out-of-range completed source is too_large (F01 import rule 2).
  if (r.declaredBytes < 1 || r.declaredBytes > maxBytes || r.declaredDurationMs < 1 || r.declaredDurationMs > maxMs) return "too_large";
  return undefined;
}
