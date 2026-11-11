import type { Span } from "@opentelemetry/api";
import type { AttachmentReference, AttachmentSpan } from "./types";

export function canonicalAttachmentTraceId(value: string): string | undefined {
  if (typeof value !== "string" || (value.length !== 32 && value.length !== 36) || !/^(?:[0-9a-f]{32}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$/.test(value)) return undefined;
  const hex = value.replace(/-/g, "");
  return /^0+$/.test(hex) ? undefined : hex;
}
export function attachmentTraceUuid(hex: string): string {
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}
export function validAttachmentUuid(value: unknown): value is string {
  return typeof value === "string" && value.length === 36 && canonicalAttachmentTraceId(value) !== undefined;
}
/** Snapshot while recording, or bind to the ACTUAL emitted context. Never consult ambient context later. */
export class AttachmentCorrelation {
  private identity?: Readonly<{ traceId: string; spanId: string; sampled: boolean }>;
  private span?: Span;
  private refs: AttachmentReference[] = [];
  private prepared = false;
  constructor(spanOrContext?: AttachmentSpan) { if (spanOrContext) this.bind(spanOrContext); }
  bind(spanOrContext: AttachmentSpan): boolean {
    if (this.identity) return false;
    try {
      const span = "spanContext" in spanOrContext ? spanOrContext : undefined;
      const ctx = span ? span.spanContext() : spanOrContext as Exclude<AttachmentSpan, Span>;
      const traceId = canonicalAttachmentTraceId(ctx.traceId);
      if (!traceId || ctx.spanId.length !== 16 || !/^[0-9a-f]{16}$/.test(ctx.spanId) || /^0+$/.test(ctx.spanId)) return false;
      this.identity = Object.freeze({ traceId, spanId: ctx.spanId, sampled: (ctx.traceFlags & 1) === 1 && (!span || span.isRecording()) });
      this.span = span;
      return true;
    } catch { return false; }
  }
  /** Stamp the four F01 carrier attributes while the actual span is still recording. Called only for an ADMITTED capture. */
  prepare(sessionId: string, nonce: string): void {
    try {
      if (this.prepared || !this.span?.isRecording()) return;
      this.span.setAttribute("fi.attachment.schema_version", 1);
      this.span.setAttribute("fi.attachment.capture_session_id", sessionId);
      this.span.setAttribute("fi.attachment.correlation_nonce", nonce);
      // The carrier always carries the span's CURRENT projection (design 0.8), never a reset to "[]".
      this.span.setAttribute("fi.attachment.refs", JSON.stringify(this.refs));
      this.prepared = true;
    } catch { /* A foreign span must not break application code. */ }
  }
  snapshot(): Readonly<{ traceId: string; spanId: string; sampled: boolean }> | undefined { return this.identity; }
  /** Called synchronously at admission only. Ended spans are NEVER mutated by transfer completion. */
  project(reference: AttachmentReference, sessionId: string, nonce: string): void {
    try {
      if (!this.span?.isRecording()) { this.span = undefined; return; }
      const next = [...this.refs, Object.freeze({ ...reference })];
      if (next.length > 64) return;
      const json = JSON.stringify(next);
      if (new TextEncoder().encode(json).byteLength > 16384) return;
      this.span.setAttribute("fi.attachment.schema_version", 1);
      this.span.setAttribute("fi.attachment.capture_session_id", sessionId);
      this.span.setAttribute("fi.attachment.correlation_nonce", nonce);
      this.span.setAttribute("fi.attachment.refs", json);
      this.refs = next;
    } catch { /* Host instrumentation must never fail because of an attachment. */ }
  }
  references(): readonly AttachmentReference[] { return this.refs.map(ref => Object.freeze({ ...ref })); }
}
export function attachmentMessageUri(reference: AttachmentReference): Readonly<{ type: "uri"; modality: AttachmentReference["modality"]; mime_type: string; uri: string }> {
  return Object.freeze({ type: "uri", modality: reference.modality, mime_type: reference.mime_type, uri: `fi-attachment:${reference.occurrence_id}` });
}
