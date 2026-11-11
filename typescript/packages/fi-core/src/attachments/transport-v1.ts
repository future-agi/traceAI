import { context } from "@opentelemetry/api";
import { suppressTracing } from "@opentelemetry/core";
import { validAttachmentUuid } from "./correlation";
import type { AttachmentAdmission, AttachmentBootstrap, AttachmentSession, AttachmentTransport, AttachmentUpload, AttachmentUploadRequest } from "./types";

/** Safe errors have only fixed codes, never response bodies, credentials or URLs. */
export class AttachmentTransportError extends Error {
  constructor(readonly code: AttachmentAdmission | "failed", readonly retryable = false, readonly retryAfterMs = 0) {
    super(code); this.name = "AttachmentTransportError";
  }
}
export interface AttachmentHttpRequest {
  url: string; method: "POST" | "PUT"; headers: Record<string, string>;
  body: string | Uint8Array | Blob; signal: AbortSignal;
}
export interface AttachmentHttpResponse { status: number; body: Uint8Array; retryAfter?: string }
export type AttachmentHttpClient = (request: AttachmentHttpRequest) => Promise<AttachmentHttpResponse>;

/** No cookies, redirects, arbitrary destination paths or unbounded JSON reads. */
export const attachmentFetchClient: AttachmentHttpClient = async request => {
  try {
    const response = await fetch(request.url, {
      method: request.method, headers: request.headers, body: request.body,
      signal: request.signal, redirect: "error", credentials: "omit",
    });
    if (response.status < 200 || response.status >= 300) {
      await response.body?.cancel().catch(() => undefined);
      return { status: response.status, body: new Uint8Array(), retryAfter: response.headers.get("retry-after") ?? undefined };
    }
    const reader = response.body?.getReader();
    const chunks: Uint8Array[] = []; let size = 0;
    if (reader) {
      try {
        for (;;) {
          const { value, done } = await reader.read(); if (done) break;
          size += value.byteLength;
          if (size > 65536) throw new AttachmentTransportError("failed");
          chunks.push(value);
        }
      } finally { await reader.cancel().catch(() => undefined); }
    }
    const body = new Uint8Array(size); let offset = 0;
    for (const chunk of chunks) { body.set(chunk, offset); offset += chunk.length; }
    return { status: response.status, body, retryAfter: response.headers.get("retry-after") ?? undefined };
  } catch (error) {
    if (error instanceof AttachmentTransportError) throw error;
    throw new AttachmentTransportError("failed", !request.signal.aborted);
  }
};
function object(value: unknown): value is Record<string, any> { return !!value && typeof value === "object" && !Array.isArray(value); }
export function validAttachmentSession(value: unknown, sessionId: string): value is AttachmentSession {
  if (!object(value) || value.schema_version !== 1 || value.capture_session_id !== sessionId || !object(value.scope) ||
    ![value.scope.organization_id, value.scope.workspace_id, value.scope.project_id].every(validAttachmentUuid) ||
    !Number.isFinite(Date.parse(value.expires_at)) || Date.parse(value.expires_at) <= Date.now() ||
    !Array.isArray(value.transfer_modes) || value.transfer_modes.length !== 1 || value.transfer_modes[0] !== "authenticated_stream") return false;
  const p = value.policy;
  if (!object(p) || typeof p.version !== "string" || p.version.trim() !== p.version || !/^[a-zA-Z0-9_-]{1,128}$/.test(p.version) ||
    !["enabled", "require_media_redaction", "require_metadata_removal", "allow_unredacted_media_with_text_pii", "record_audio", "record_video"].every(key => typeof p[key] === "boolean") ||
    !Array.isArray(p.allowed_sources) || p.allowed_sources.length > 6 || !p.allowed_sources.every((s: unknown) => ["Bytes", "Blob", "RemoteUrl", "LocalFile", "OwnedRecordingFile", "ProviderFile"].includes(s as string)) ||
    !Array.isArray(p.allowed_modalities) || p.allowed_modalities.length > 5 || !p.allowed_modalities.every((m: unknown) => ["audio", "image", "video", "document", "file"].includes(m as string))) return false;
  const limits: Record<string, number> = { max_upload_bytes: 52428800, segment_bytes: 8388608, segment_duration_ms: 30000,
    recording_bytes: 1073741824, recording_duration_ms: 7200000, active_transfers: 16, active_recordings: 4,
    reserved_bytes: 1073741824, validation_jobs: 1024, initializations_per_minute: 120, links_per_minute: 1000,
    upload_bytes_per_second: 1073741824, playback_bytes_per_second: 1073741824, transfer_timeout_seconds: 600,
    grant_ttl_seconds: 300, revocation_check_seconds: 5 };
  return object(value.limits) && Object.entries(limits).every(([key, max]) => Number.isInteger(value.limits[key]) && value.limits[key] > 0 && value.limits[key] <= max);
}
export function validAttachmentUpload(value: unknown, request: AttachmentUploadRequest): value is AttachmentUpload {
  return object(value) && validAttachmentUuid(value.upload_id) && value.attachment_id === request.attachment_id &&
    value.occurrence_id === request.occurrence_id && value.state === "pending_upload" && value.reserved_bytes === request.declared_bytes &&
    Number.isFinite(Date.parse(value.expires_at)) && Date.parse(value.expires_at) > Date.now() && value.transfer_mode === "authenticated_stream" && typeof value.write_capability === "string" && value.write_capability.trim() === value.write_capability && /^[a-zA-Z0-9_.-]{16,4096}$/.test(value.write_capability);
}
export interface AttachmentHttpTransportOptions {
  endpoint: string;
  /** Explicitly supplied credential/capability. Never read environment in browser code. */
  headers: Readonly<Record<string, string>>;
  client?: AttachmentHttpClient;
}
export function createAttachmentHttpTransport(options: AttachmentHttpTransportOptions): AttachmentTransport {
  const url = new URL(options.endpoint);
  if (url.protocol !== "https:" || url.username || url.password || url.search || url.hash || url.pathname !== "/") {
    throw new AttachmentTransportError("endpoint_unconfigured");
  }
  const origin = url.origin;
  const headers: Record<string, string> = {};
  const allowed = new Set(["authorization", "x-api-key", "x-secret-key", "x-workspace-id"]);
  for (const [name, value] of Object.entries(options.headers)) {
    if (allowed.has(name.toLowerCase()) && typeof value === "string" && value.length <= 8192 && !/[\r\n]/.test(value)) headers[name.toLowerCase()] = value;
  }
  const client = options.client ?? attachmentFetchClient;
  async function send(path: string, body: string | Uint8Array | Blob, signal: AbortSignal, expected: number, key?: string, capability?: string): Promise<any> {
    if (signal.aborted) throw new AttachmentTransportError("incomplete");
    const binary = typeof body !== "string";
    let response: AttachmentHttpResponse;
    try {
      response = await context.with(suppressTracing(context.active()), () => client({
        url: origin + path, method: binary ? "PUT" : "POST", signal, body,
        headers: { ...(capability ? { authorization: `Bearer ${capability}` } : headers), "content-type": binary ? "application/octet-stream" : "application/json", "cache-control": "no-store", ...(key ? { "idempotency-key": key } : {}) },
      }));
    } catch (error) { throw error instanceof AttachmentTransportError ? error : new AttachmentTransportError("failed", !signal.aborted); }
    if (response.status !== expected) {
      const code = response.status === 401 || response.status === 403 ? "credential_scope_unsupported" : response.status === 413 ? "too_large" : response.status === 409 ? "policy_denied" : response.status === 410 ? "expired" : "failed";
      const retryable = response.status === 429 || response.status >= 500 && response.status <= 599;
      const after = Number(response.retryAfter);
      throw new AttachmentTransportError(code, retryable, Number.isFinite(after) && after >= 0 ? Math.min(after * 1000, 2000) : 0);
    }
    if (response.body.byteLength > 65536) throw new AttachmentTransportError("failed");
    try { return JSON.parse(new TextDecoder().decode(response.body)); }
    catch { throw new AttachmentTransportError("failed"); }
  }
  const base = (session: AttachmentSession) => {
    if (!validAttachmentUuid(session.scope.project_id)) throw new AttachmentTransportError("scope_unresolved");
    return `/tracer/v1/projects/${session.scope.project_id}/attachments/uploads`;
  };
  const uploadPath = (session: AttachmentSession, upload: AttachmentUpload) => {
    if (!validAttachmentUuid(upload.upload_id)) throw new AttachmentTransportError("failed");
    return `${base(session)}/${upload.upload_id}`;
  };
  return {
    async bootstrap(request, signal) {
      const result = await send("/tracer/v1/attachment-sessions", JSON.stringify(request), signal, 200, request.capture_session_id);
      if (!validAttachmentSession(result, request.capture_session_id)) throw new AttachmentTransportError("unsupported_version");
      return result;
    },
    async initialize(session, request, signal) {
      const result = await send(base(session), JSON.stringify(request), signal, 201, request.occurrence_id);
      if (!validAttachmentUpload(result, request)) throw new AttachmentTransportError("failed");
      return result;
    },
    async upload(session, upload, data, signal) {
      const result = await send(`${uploadPath(session, upload)}/content`, data, signal, 200, undefined, upload.write_capability);
      if (!object(result) || result.upload_id !== upload.upload_id || result.state !== "pending_upload") throw new AttachmentTransportError("failed");
    },
    async complete(session, upload, request, signal) {
      const result = await send(`${uploadPath(session, upload)}/complete`, JSON.stringify({ policy_version: request.policy_version, deletion_generation: request.deletion_generation }), signal, 202, request.occurrence_id, upload.write_capability);
      if (!object(result) || result.upload_id !== upload.upload_id || result.state !== "validating") throw new AttachmentTransportError("failed");
    },
  };
}
