import { randomUUID } from "node:crypto";
import type { Span } from "@opentelemetry/api";
import type {
  AttachmentAdmission, AttachmentAdmissionTicket, AttachmentHandle, AttachmentManager,
} from "@traceai/fi-core/attachments";
import type { TraceConfigOptions } from "@traceai/fi-core";
import type { ContentPolicy } from "./spans";

/** Per-send inline image budget, matching the coding-agent helper. Larger images are not captured. */
export const MAX_CURSOR_IMAGE_BYTES = 256 * 1024;
const MAX_IMAGES = 16;
const IMAGE_MIMES = new Set(["image/png", "image/jpeg", "image/webp"]);

/**
 * Exact decoded length of strict, padded base64, computed without decoding. Returns "too_large" for a
 * string longer than `limit` bytes can encode (never scanned), and undefined for anything malformed.
 */
function strictBase64Length(value: unknown, limit: number): number | "too_large" | undefined {
  if (typeof value !== "string" || !value.length || value.length % 4 !== 0) return undefined;
  if (value.length > 4 * Math.ceil(Math.max(limit, 0) / 3)) return "too_large";
  if (!/^[A-Za-z0-9+/]*={0,2}$/.test(value)) return undefined;
  const length = (value.length / 4) * 3 - (value.endsWith("==") ? 2 : value.endsWith("=") ? 1 : 0);
  if (length <= 0) return undefined;
  return length > limit ? "too_large" : length;
}

/** A terminal handle for an image refused after admission; the ticket is already released. */
function refused(admission: AttachmentAdmission): AttachmentHandle {
  return Object.freeze({ attachmentId: randomUUID(), occurrenceId: randomUUID(), admission,
    completion: Promise.resolve(Object.freeze({ status: admission })) });
}
const NO_SOURCE = Object.freeze({ kind: "Bytes" as const, data: new Uint8Array(0) });

/**
 * Deny-wins: the instrumentation's own trace config is not the provider manager's config, so
 * any input hide flag or PII redaction it sets denies prompt images here, as `captureContent:
 * false`, which the core cannot relax (not even with `allowUnredactedMediaWithTextPii`).
 */
export function cursorImageCaptureDenied(policy: ContentPolicy, traceConfig?: TraceConfigOptions): boolean {
  return policy.hideInputs || !!traceConfig && (!!traceConfig.hideInputs || !!traceConfig.hideInputMessages ||
    !!traceConfig.hideInputText || !!traceConfig.hideInputImages || !!traceConfig.piiRedaction);
}

/**
 * `SDKUserMessage.images` inline `{ data, mimeType }` entries only, before count
 * normalization. `{ url }` images are retrieval and are never fetched. The package's
 * hidden-input default (and FI_HIDE_INPUTS) and the instrumentation's other input hide flags
 * and PII redaction are passed as restrictions the core cannot loosen. Each image is preflighted
 * before its base64 is read; only an admitted image is read, strictly validated and decoded.
 */
export function captureCursorImages(
  manager: AttachmentManager | undefined,
  span: Span,
  message: unknown,
  policy: ContentPolicy,
  traceConfig?: TraceConfigOptions,
): readonly AttachmentHandle[] {
  try {
    if (!manager || policy.hideInputs || manager.status().state !== "ready") return [];
    const denied = cursorImageCaptureDenied(policy, traceConfig);
    if (!message || typeof message !== "object") return [];
    const images = (message as { images?: unknown }).images;
    if (!Array.isArray(images)) return [];
    const handles: AttachmentHandle[] = [];
    const capture = denied ? { captureContent: false } : { recordInputs: true, modelInput: true };
    let remaining = MAX_CURSOR_IMAGE_BYTES;
    for (let partIndex = 0; partIndex < Math.min(images.length, MAX_IMAGES); partIndex++) {
      let ticket: AttachmentAdmissionTicket | undefined;
      try {
        const image = images[partIndex] as { mimeType?: unknown; data?: unknown } | null;
        if (!image || typeof image !== "object") continue;
        const mimeType = image.mimeType;
        if (typeof mimeType !== "string" || !IMAGE_MIMES.has(mimeType)) continue;
        const preflight = (maxBytes: number) => manager.preflight(span, {
          mimeType, direction: "input", purpose: "model_input", capture, maxBytes,
        });
        ticket = preflight(remaining);
        let value: unknown;
        let read = false;
        if (!ticket.admitted && (ticket.reason === "too_large" || ticket.reason === "queue_full")) {
          // Budget denials come only after the policy gate passed.
          value = image.data; read = true;
          const exact = strictBase64Length(value, remaining);
          if (typeof exact === "number") ticket = preflight(exact);
        }
        // A denied ticket mints its own terminal handle without touching the source.
        if (!ticket.admitted) { handles.push(ticket.attach(NO_SOURCE)); ticket = undefined; continue; }
        if (!read) value = image.data;
        const length = strictBase64Length(value, Math.min(remaining, ticket.maxBytes));
        if (typeof length !== "number") {
          ticket.release(); ticket = undefined;
          handles.push(refused(length === "too_large" ? "too_large" : "source_unavailable"));
          continue;
        }
        const handle = ticket.attach({ kind: "Bytes", data: Buffer.from(value as string, "base64") }, { role: "user", partIndex });
        ticket = undefined;
        if (handle.admission === "queued") remaining -= length;
        handles.push(handle);
      } catch {
        // A throwing or malformed image entry never affects the run.
        if (ticket) { ticket.release(); handles.push(refused("source_unavailable")); }
      }
    }
    return handles;
  } catch {
    return [];
  }
}
