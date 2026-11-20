import { context } from "@opentelemetry/api";
import { suppressTracing } from "@opentelemetry/core";
import { validAttachmentUuid } from "./correlation";
import { attachmentWarnLegacyTransport, attachmentWireRejected } from "./warn";
import type {
  AttachmentAdmission, AttachmentBootstrap, AttachmentCompletionStatus, AttachmentContentSource, AttachmentImportJob, AttachmentPacer,
  AttachmentSession, AttachmentTransport, AttachmentTransportV1, AttachmentUpload, AttachmentUploadRequest,
} from "./types";

export type AttachmentErrorCode = AttachmentAdmission | AttachmentCompletionStatus | "failed";
/** Safe errors have only fixed codes, never response bodies, credentials or URLs. */
export class AttachmentTransportError extends Error {
  constructor(readonly code: AttachmentErrorCode, readonly retryable = false, readonly retryAfterMs = 0, readonly httpStatus = 0) {
    super(code); this.name = "AttachmentTransportError";
  }
}
export interface AttachmentHttpRequest {
  url: string; method: "GET" | "POST" | "PUT"; headers: Record<string, string>;
  body?: string | Uint8Array | Blob | AttachmentContentSource; signal: AbortSignal;
  /** Content PUT only: per-slice stall timeout and pacing. */
  stallTimeoutMs?: number; pacer?: AttachmentPacer;
}
export interface AttachmentHttpResponse { status: number; body: Uint8Array; retryAfter?: string }
export type AttachmentHttpClient = (request: AttachmentHttpRequest) => Promise<AttachmentHttpResponse>;

export const ATTACHMENT_CHUNK_BYTES = 256 * 1024;
const SUCCESS_BODY = 65536;
const ERROR_BODY = 4096;
const TOKEN = /^[a-zA-Z0-9_-]{1,128}$/;
const REASONS = new Set(["queued", "disabled", "bootstrap_pending", "policy_denied", "credential_scope_unsupported", "source_unavailable", "unsampled",
  "too_large", "queue_full", "unsupported_version", "endpoint_unconfigured", "scope_unresolved", "correlation_unavailable", "redaction_unavailable",
  "recording_disabled", "binding_pending", "not_found", "conflict", "throttled", "validation_failed", "expired", "deleted", "incomplete", "ready"]);
const STATES = new Set(["pending_upload", "importing", "validating", "ready", "failed", "rejected", "expired", "deleted"]);

/** Strict JSON: duplicate members and non-JSON numbers are rejected (F01 validate.py strict_json). */
export function attachmentStrictJson(text: string): unknown {
  let i = 0;
  const ws = () => { while (i < text.length && " \t\n\r".includes(text[i])) i++; };
  const fail = (): never => { throw new SyntaxError("invalid JSON"); };
  const str = (): string => {
    const start = i; i++;
    for (; i < text.length; i++) {
      const c = text[i];
      if (c === "\\") { i++; continue; }
      if (c === '"') { i++; return JSON.parse(text.slice(start, i)); }
      if (c < " ") fail();
    }
    return fail();
  };
  const value = (depth: number): unknown => {
    if (depth > 64) fail();
    ws(); const c = text[i];
    if (c === "{") {
      i++; const out: Record<string, unknown> = {}; const keys = new Set<string>(); ws();
      if (text[i] === "}") { i++; return out; }
      for (;;) {
        ws(); if (text[i] !== '"') fail(); const key = str(); if (keys.has(key)) fail(); keys.add(key);
        ws(); if (text[i] !== ":") fail(); i++;
        Object.defineProperty(out, key, { value: value(depth + 1), enumerable: true, writable: true, configurable: true });
        ws(); if (text[i] === ",") { i++; continue; } if (text[i] === "}") { i++; return out; } fail();
      }
    }
    if (c === "[") {
      i++; const out: unknown[] = []; ws();
      if (text[i] === "]") { i++; return out; }
      for (;;) { out.push(value(depth + 1)); ws(); if (text[i] === ",") { i++; continue; } if (text[i] === "]") { i++; return out; } fail(); }
    }
    if (c === '"') return str();
    const m = /^-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?|^true|^false|^null/.exec(text.slice(i, i + 400));
    if (!m) return fail();
    i += m[0].length; return JSON.parse(m[0]);
  };
  const result = value(0); ws(); if (i !== text.length) fail();
  return result;
}

const REFINE: Readonly<Record<number, readonly string[]>> = {
  400: ["unsupported_version"], 401: ["credential_scope_unsupported", "expired"],
  403: ["policy_denied", "redaction_unavailable", "recording_disabled", "correlation_unavailable", "credential_scope_unsupported"],
  404: ["not_found", "scope_unresolved"], 409: ["conflict", "deleted", "expired", "policy_denied"], 410: ["expired", "deleted"], 413: ["too_large"],
};
const DEFAULT_REASON: Readonly<Record<number, AttachmentErrorCode>> = {
  400: "unsupported_version", 401: "credential_scope_unsupported", 403: "policy_denied", 404: "not_found", 409: "conflict", 410: "expired", 413: "too_large", 429: "throttled",
};
/** Integer delta-seconds only, capped at 5 s; an HTTP-date is treated as 1 s (design 0.4). */
export function attachmentRetryAfterMs(value: string | undefined, status: number): number {
  if (value === undefined || value === "") return status === 429 ? 1000 : 0;
  const trimmed = String(value).trim();
  if (/^\d{1,9}$/.test(trimmed)) return Math.min(Number(trimmed) * 1000, 5000);
  return 1000;
}
/** One HTTP status -> reason table for every route (design 0.5). */
export function attachmentHttpError(status: number, body?: Uint8Array, retryAfter?: string): AttachmentTransportError {
  let code: AttachmentErrorCode = DEFAULT_REASON[status] ?? "incomplete";
  const refine = REFINE[status];
  if (refine && body && body.byteLength && body.byteLength <= ERROR_BODY) {
    try {
      const parsed = attachmentStrictJson(new TextDecoder("utf-8", { fatal: true }).decode(body)) as Record<string, unknown>;
      if (object(parsed) && Object.keys(parsed).every(key => key === "code" || key === "request_id") && validAttachmentUuid(parsed.request_id) &&
        typeof parsed.code === "string" && REASONS.has(parsed.code) && refine.includes(parsed.code)) code = parsed.code as AttachmentErrorCode;
    } catch { /* Unparseable error bodies are ignored. */ }
  }
  const retryable = status === 429 || status === 408 || status === 500 || status === 502 || status === 503 || status === 504;
  return new AttachmentTransportError(code, retryable, retryable ? attachmentRetryAfterMs(retryAfter, status) : 0, status);
}

/** Idempotency keys (design 0.6). F01 Token: ^[a-zA-Z0-9_-]{1,128}$. */
export function attachmentIdempotencyKey(route: "bootstrap" | "upload" | "complete" | "abort" | "manifest" | "import", id: string, sequence?: number): string {
  const hex = String(id).replace(/-/g, "");
  const prefix = { bootstrap: "bs", upload: "up", complete: "cp", abort: "ab", manifest: "mf", import: "im" }[route];
  return sequence === undefined ? `${prefix}-${hex}` : `${prefix}-${hex}-${sequence}`;
}

async function readBounded(response: Response, limit: number): Promise<Uint8Array | undefined> {
  const reader = response.body?.getReader();
  const chunks: Uint8Array[] = []; let size = 0;
  if (reader) {
    try {
      for (;;) {
        const { value, done } = await reader.read(); if (done) break;
        size += value.byteLength;
        if (size > limit) return undefined;
        chunks.push(value);
      }
    } finally { await reader.cancel().catch(() => undefined); }
  }
  const body = new Uint8Array(size); let offset = 0;
  for (const chunk of chunks) { body.set(chunk, offset); offset += chunk.length; }
  return body;
}
/** Materialize an in-memory content source (browser fetch has no streaming request bodies everywhere). */
export async function attachmentContentBytes(source: AttachmentContentSource, signal: AbortSignal): Promise<Uint8Array | Blob> {
  if (source instanceof Uint8Array || (typeof Blob !== "undefined" && source instanceof Blob)) return source;
  const ranged = source as Exclude<AttachmentContentSource, Uint8Array | Blob>;
  const out = new Uint8Array(ranged.size);
  for (let offset = 0; offset < ranged.size; offset += ATTACHMENT_CHUNK_BYTES) {
    if (signal.aborted) throw new AttachmentTransportError("incomplete");
    await ranged.check();
    const part = await ranged.read(offset, Math.min(ATTACHMENT_CHUNK_BYTES, ranged.size - offset));
    if (part.byteLength !== Math.min(ATTACHMENT_CHUNK_BYTES, ranged.size - offset)) throw new AttachmentTransportError("source_unavailable");
    out.set(part, offset);
  }
  await ranged.check();
  return out;
}
/** No cookies, redirects, arbitrary destination paths or unbounded JSON reads. */
export const attachmentFetchClient: AttachmentHttpClient = async request => {
  try {
    const body = request.body === undefined || typeof request.body === "string" ? request.body : await attachmentContentBytes(request.body, request.signal);
    if (body !== undefined && typeof body !== "string" && request.pacer) await request.pacer.take(body instanceof Uint8Array ? body.byteLength : body.size, request.signal);
    const response = await fetch(request.url, {
      method: request.method, headers: request.headers, body: body as unknown as RequestInit["body"],
      signal: request.signal, redirect: "error", credentials: "omit",
    });
    const ok = response.status >= 200 && response.status < 300;
    const data = await readBounded(response, ok ? SUCCESS_BODY : ERROR_BODY);
    if (!data) {
      if (ok) throw attachmentWireRejected("response size", new AttachmentTransportError("unsupported_version"));
      return { status: response.status, body: new Uint8Array(), retryAfter: response.headers.get("retry-after") ?? undefined };
    }
    return { status: response.status, body: data, retryAfter: response.headers.get("retry-after") ?? undefined };
  } catch (error) {
    if (error instanceof AttachmentTransportError) throw error;
    // A refused redirect surfaces as a network TypeError; the abort signal tells cancellation apart.
    throw new AttachmentTransportError("incomplete", !request.signal.aborted);
  }
};
function object(value: unknown): value is Record<string, any> { return !!value && typeof value === "object" && !Array.isArray(value); }
const LIMIT_MAX: Readonly<Record<string, number>> = { max_upload_bytes: 52428800, segment_bytes: 8388608, segment_duration_ms: 30000,
  recording_bytes: 1073741824, recording_duration_ms: 7200000, active_transfers: 16, active_recordings: 4,
  reserved_bytes: 1073741824, validation_jobs: 1024, initializations_per_minute: 120, links_per_minute: 1000,
  upload_bytes_per_second: 1073741824, playback_bytes_per_second: 1073741824, transfer_timeout_seconds: 600,
  grant_ttl_seconds: 300, revocation_check_seconds: 5 };
export function validAttachmentSession(value: unknown, sessionId: string, now = Date.now()): value is AttachmentSession {
  if (!object(value) || value.schema_version !== 1 || value.capture_session_id !== sessionId || !object(value.scope) ||
    ![value.scope.organization_id, value.scope.workspace_id, value.scope.project_id].every(validAttachmentUuid) ||
    !Number.isFinite(Date.parse(value.expires_at)) || Date.parse(value.expires_at) <= now ||
    !Array.isArray(value.transfer_modes) || value.transfer_modes.length !== 1 || value.transfer_modes[0] !== "authenticated_stream") return false;
  const p = value.policy;
  if (!object(p) || typeof p.version !== "string" || p.version.trim() !== p.version || !TOKEN.test(p.version) ||
    !["enabled", "require_media_redaction", "require_metadata_removal", "allow_unredacted_media_with_text_pii", "record_audio", "record_video"].every(key => typeof p[key] === "boolean") ||
    !Array.isArray(p.allowed_sources) || p.allowed_sources.length > 6 || !p.allowed_sources.every((s: unknown) => ["Bytes", "Blob", "RemoteUrl", "LocalFile", "OwnedRecordingFile", "ProviderFile"].includes(s as string)) ||
    !Array.isArray(p.allowed_modalities) || p.allowed_modalities.length > 5 || !p.allowed_modalities.every((m: unknown) => ["audio", "image", "video", "document", "file"].includes(m as string))) return false;
  return object(value.limits) && Object.entries(LIMIT_MAX).every(([key, max]) => Number.isInteger(value.limits[key]) && value.limits[key] > 0 && value.limits[key] <= max);
}
export function validAttachmentUpload(value: unknown, request: AttachmentUploadRequest, now = Date.now()): value is AttachmentUpload {
  return object(value) && validAttachmentUuid(value.upload_id) && value.attachment_id === request.attachment_id &&
    value.occurrence_id === request.occurrence_id && value.state === "pending_upload" && value.reserved_bytes === request.declared_bytes &&
    Number.isFinite(Date.parse(value.expires_at)) && Date.parse(value.expires_at) > now && value.transfer_mode === "authenticated_stream" &&
    typeof value.write_capability === "string" && value.write_capability.trim() === value.write_capability && /^[a-zA-Z0-9_.-]{16,4096}$/.test(value.write_capability);
}
function validStatus(value: unknown, id: string, key: "upload_id" | "job_id", state?: string): value is { state: string; reason: string } {
  return object(value) && Object.keys(value).length === 3 && value[key] === id && typeof value.state === "string" && STATES.has(value.state) &&
    typeof value.reason === "string" && REASONS.has(value.reason) && (state === undefined || value.state === state);
}
export interface AttachmentHttpTransportOptions {
  endpoint: string;
  /** Explicitly supplied credential/capability. Never read environment in browser code. */
  headers: Readonly<Record<string, string>>;
  client?: AttachmentHttpClient;
  /** Per-slice stall timeout for content PUTs (default 2 s). */
  stallTimeoutMs?: number;
}
/** Symbol under which a legacy-shaped transport carries its full versioned surface. */
export const ATTACHMENT_TRANSPORT_V1: unique symbol = Symbol.for("@traceai/fi-core/attachments/transport-v1");

/** The full design section 9 surface over HTTPS. */
export function createAttachmentHttpTransportV1(options: AttachmentHttpTransportOptions): AttachmentTransportV1 {
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
  const stall = Number.isInteger(options.stallTimeoutMs) && options.stallTimeoutMs! > 0 ? options.stallTimeoutMs! : 2000;
  const bootstraps = new Map<string, AttachmentBootstrap>();
  async function send(method: "GET" | "POST" | "PUT", path: string, body: AttachmentHttpRequest["body"], signal: AbortSignal, expected: number,
    extra: { key?: string; capability?: string; binary?: boolean; pacer?: AttachmentPacer } = {}): Promise<unknown> {
    if (signal.aborted) throw new AttachmentTransportError("incomplete");
    let response: AttachmentHttpResponse;
    const auth = extra.capability !== undefined ? { authorization: `Bearer ${extra.capability}` } : headers;
    try {
      response = await context.with(suppressTracing(context.active()), () => client({
        url: origin + path, method, signal, body,
        headers: { ...auth, ...(body === undefined ? {} : { "content-type": extra.binary ? "application/octet-stream" : "application/json" }),
          "cache-control": "no-store", ...(extra.key ? { "idempotency-key": extra.key } : {}) },
        ...(extra.binary ? { stallTimeoutMs: stall, pacer: extra.pacer } : {}),
      }));
    } catch (error) {
      if (error instanceof AttachmentTransportError) throw error.code === "failed" ? new AttachmentTransportError("incomplete", error.retryable) : error;
      throw new AttachmentTransportError("incomplete", !signal.aborted);
    }
    if (response.status !== expected) {
      if (response.status >= 200 && response.status < 300) throw attachmentWireRejected("HTTP success status", new AttachmentTransportError("unsupported_version"));
      throw attachmentHttpError(response.status, response.body, response.retryAfter);
    }
    if (response.body.byteLength > SUCCESS_BODY) throw attachmentWireRejected("response size", new AttachmentTransportError("unsupported_version"));
    if (expected === 204) return undefined;
    try { return attachmentStrictJson(new TextDecoder("utf-8", { fatal: true }).decode(response.body)); }
    catch { throw attachmentWireRejected("strict JSON body", new AttachmentTransportError("unsupported_version")); }
  }
  const base = (session: AttachmentSession) => {
    if (!validAttachmentUuid(session.scope.project_id)) throw new AttachmentTransportError("scope_unresolved");
    return `/tracer/v1/projects/${session.scope.project_id}/attachments`;
  };
  const uploadPath = (session: AttachmentSession, upload: AttachmentUpload) => {
    if (!validAttachmentUuid(upload.upload_id)) throw new AttachmentTransportError("unsupported_version");
    return `${base(session)}/uploads/${upload.upload_id}`;
  };
  const parseUploadBody = (body: string): AttachmentUploadRequest => {
    try { return attachmentStrictJson(body) as AttachmentUploadRequest; } catch { throw new AttachmentTransportError("unsupported_version"); }
  };
  const completeBody = (session: AttachmentSession) => JSON.stringify({ policy_version: session.policy.version, deletion_generation: 0 });
  const v1: AttachmentTransportV1 = {
    async bootstrap(body, key, signal) {
      const result = await send("POST", "/tracer/v1/attachment-sessions", JSON.stringify(body), signal, 200, { key });
      if (!validAttachmentSession(result, body.capture_session_id)) throw attachmentWireRejected("AttachmentSession", new AttachmentTransportError("unsupported_version"));
      bootstraps.set(body.capture_session_id, { ...body });
      return result;
    },
    async renewSession(current, sequence, signal) {
      // F01 v1 has no renewal route: replay bootstrap with the SAME session id and nonce under a new key (BR-2, CQ-1).
      const body = bootstraps.get(current.capture_session_id);
      if (!body) throw new AttachmentTransportError("unsupported_version");
      const result = await send("POST", "/tracer/v1/attachment-sessions", JSON.stringify(body), signal, 200,
        { key: attachmentIdempotencyKey("bootstrap", current.capture_session_id, sequence) });
      if (!validAttachmentSession(result, body.capture_session_id)) throw attachmentWireRejected("AttachmentSession", new AttachmentTransportError("unsupported_version"));
      return result;
    },
    async initialize(session, body, key, signal) {
      const request = parseUploadBody(body);
      const result = await send("POST", `${base(session)}/uploads`, body, signal, 201, { key });
      if (!validAttachmentUpload(result, request)) throw attachmentWireRejected("UploadResponse", new AttachmentTransportError("unsupported_version"));
      return result;
    },
    async renewUpload(session, upload, body, signal) {
      // Replay the byte-identical initialize under the same key with the ORIGINAL identity (BR-1).
      const request = parseUploadBody(body);
      const result = await send("POST", `${base(session)}/uploads`, body, signal, 201, { key: attachmentIdempotencyKey("upload", request.occurrence_id) });
      if (!validAttachmentUpload(result, request)) throw attachmentWireRejected("UploadResponse", new AttachmentTransportError("unsupported_version"));
      if (result.upload_id !== upload.upload_id || result.write_capability === upload.write_capability ||
        Date.parse(result.expires_at) <= Date.parse(upload.expires_at)) throw new AttachmentTransportError("expired");
      return result;
    },
    async putContent(session, upload, source, signal, pacer) {
      const size = source instanceof Uint8Array ? source.byteLength : (source as { size: number }).size;
      if (size !== upload.reserved_bytes) throw new AttachmentTransportError("too_large");
      const result = await send("PUT", `${uploadPath(session, upload)}/content`, source, signal, 200, { capability: upload.write_capability, binary: true, pacer });
      if (!validStatus(result, upload.upload_id, "upload_id", "pending_upload")) throw attachmentWireRejected("UploadStatus", new AttachmentTransportError("unsupported_version"));
    },
    async complete(session, upload, key, signal) {
      const result = await send("POST", `${uploadPath(session, upload)}/complete`, completeBody(session), signal, 202, { key, capability: upload.write_capability });
      // validating, never ready: late-root and root-first orders both still need validation (F01 lifecycle fixtures).
      if (!validStatus(result, upload.upload_id, "upload_id", "validating")) throw attachmentWireRejected("UploadStatus", new AttachmentTransportError("unsupported_version"));
      return "validating";
    },
    async abort(session, upload, key, signal) {
      const result = await send("POST", `${uploadPath(session, upload)}/abort`, completeBody(session), signal, 200, { key, capability: upload.write_capability });
      if (!validStatus(result, upload.upload_id, "upload_id")) throw attachmentWireRejected("UploadStatus", new AttachmentTransportError("unsupported_version"));
    },
    async uploadStatus(session, upload, signal) {
      const result = await send("GET", `${uploadPath(session, upload)}/status`, undefined, signal, 200, { capability: upload.write_capability });
      if (!validStatus(result, upload.upload_id, "upload_id")) throw attachmentWireRejected("UploadStatus", new AttachmentTransportError("unsupported_version"));
      return { state: result.state, reason: result.reason };
    },
    async putManifest(session, capability, recordingId, body, key, signal) {
      if (!validAttachmentUuid(recordingId) || !/^[a-zA-Z0-9_.-]{16,4096}$/.test(capability)) throw new AttachmentTransportError("unsupported_version");
      if (new TextEncoder().encode(body).byteLength > 1048576) throw new AttachmentTransportError("too_large");
      await send("PUT", `${base(session)}/recordings/${recordingId}/manifest`, body, signal, 204, { key, capability });
    },
    async submitImport(session, body, key, signal) {
      let recordingId: string;
      try { recordingId = (attachmentStrictJson(body) as { recording: { recording_id: string } }).recording.recording_id; }
      catch { throw new AttachmentTransportError("unsupported_version"); }
      const result = await send("POST", `${base(session)}/imports`, body, signal, 202, { key });
      if (!object(result) || Object.keys(result).length !== 4 || result.mode !== "completed_recording" || result.recording_id !== recordingId ||
        result.state !== "importing" || !validAttachmentUuid(result.job_id)) throw attachmentWireRejected("RecordingImportResponse", new AttachmentTransportError("unsupported_version"));
      return { jobId: result.job_id, recordingId, state: "importing" } as AttachmentImportJob;
    },
    async importStatus(session, jobId, signal) {
      if (!validAttachmentUuid(jobId)) throw new AttachmentTransportError("unsupported_version");
      const result = await send("GET", `${base(session)}/imports/${jobId}/status`, undefined, signal, 200);
      if (!validStatus(result, jobId, "job_id")) throw attachmentWireRejected("ImportStatus", new AttachmentTransportError("unsupported_version"));
      return { state: result.state, reason: result.reason };
    },
  };
  return v1;
}

/**
 * Legacy four-method shape, kept for existing doubles and adapters. The returned object also carries the full
 * versioned surface under ATTACHMENT_TRANSPORT_V1, which the manager prefers.
 */
export function createAttachmentHttpTransport(options: AttachmentHttpTransportOptions): AttachmentTransport {
  const v1 = createAttachmentHttpTransportV1(options);
  const legacy: AttachmentTransport & { [ATTACHMENT_TRANSPORT_V1]: AttachmentTransportV1 } = {
    bootstrap: (request, signal) => v1.bootstrap(request, attachmentIdempotencyKey("bootstrap", request.capture_session_id, 0), signal),
    initialize: (session, request, signal) => v1.initialize(session, JSON.stringify(request), attachmentIdempotencyKey("upload", request.occurrence_id), signal),
    upload: (session, upload, data, signal) => v1.putContent(session, upload, data, signal, attachmentNoPacer),
    complete: async (session, upload, _request, signal) => { await v1.complete(session, upload, attachmentIdempotencyKey("complete", upload.upload_id), signal); },
    [ATTACHMENT_TRANSPORT_V1]: v1,
  };
  return legacy;
}
export const attachmentNoPacer: AttachmentPacer = { take: async () => undefined };

/** Adapt a legacy four-method transport. Routes it cannot express fail closed. */
export function attachmentLegacyTransport(transport: AttachmentTransport): AttachmentTransportV1 {
  const requests = new WeakMap<object, AttachmentUploadRequest>();
  const unsupported = async (): Promise<never> => { throw new AttachmentTransportError("unsupported_version"); };
  return {
    bootstrap: (body, _key, signal) => transport.bootstrap(body, signal),
    renewSession: unsupported,
    async initialize(session, body, _key, signal) {
      let request: AttachmentUploadRequest;
      try { request = JSON.parse(body); } catch { throw new AttachmentTransportError("unsupported_version"); }
      const upload = await transport.initialize(session, request, signal);
      if (object(upload)) requests.set(upload, request);
      return upload;
    },
    // No fresh capability can be minted through the legacy shape: renewal yields `expired` (design section 3).
    renewUpload: async () => { throw new AttachmentTransportError("expired"); },
    async putContent(session, upload, source, signal) {
      if (!(source instanceof Uint8Array) && !(typeof Blob !== "undefined" && source instanceof Blob)) {
        // Ranged sources (file leases) are never materialized for a transport that cannot stream them.
        if ((source as { size: number }).size > 8 * 1024 * 1024) throw new AttachmentTransportError("source_unavailable");
      }
      return transport.upload(session, upload, await attachmentContentBytes(source, signal), signal);
    },
    async complete(session, upload, _key, signal) {
      const request = requests.get(upload);
      if (!request) throw new AttachmentTransportError("unsupported_version");
      await transport.complete(session, upload, request, signal);
      return "validating";
    },
    abort: async () => undefined,
    uploadStatus: unsupported,
    putManifest: unsupported,
    submitImport: unsupported,
    importStatus: unsupported,
  };
}
const LEGACY_WIRES = new WeakSet<object>();
/** @internal True for the adapter around a four-method transport, which cannot renew, manifest or import. */
export function attachmentIsLegacyWire(wire: AttachmentTransportV1 | undefined): boolean { return !!wire && LEGACY_WIRES.has(wire); }
/** Pick the versioned surface of whatever transport shape the caller provided. A four-method shape warns once (loudly). */
export function attachmentWireTransport(transport: AttachmentTransport | AttachmentTransportV1 | undefined): AttachmentTransportV1 | undefined {
  if (!transport) return undefined;
  const carried = (transport as { [ATTACHMENT_TRANSPORT_V1]?: AttachmentTransportV1 })[ATTACHMENT_TRANSPORT_V1];
  if (carried && typeof carried.putContent === "function") return carried;
  if (typeof (transport as AttachmentTransportV1).putContent === "function") return transport as AttachmentTransportV1;
  attachmentWarnLegacyTransport();
  const legacy = attachmentLegacyTransport(transport as AttachmentTransport);
  LEGACY_WIRES.add(legacy);
  return legacy;
}
