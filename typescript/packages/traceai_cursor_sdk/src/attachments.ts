import type { Span } from "@opentelemetry/api";
import type { AttachmentHandle, AttachmentManager } from "@traceai/fi-core/attachments";
import type { TraceConfigOptions } from "@traceai/fi-core";
import type { ContentPolicy } from "./spans";

/** Per-send inline image budget, matching the coding-agent helper. Larger images are not captured. */
export const MAX_CURSOR_IMAGE_BYTES = 256 * 1024;
const MAX_IMAGES = 16;
const IMAGE_MIMES = new Set(["image/png", "image/jpeg", "image/webp"]);

function decode(value: unknown, remaining: number): Uint8Array {
  if (typeof value !== "string" || !value.length || value.length > 4 * Math.ceil(remaining / 3) ||
      value.length % 4 !== 0 || !/^[A-Za-z0-9+/]*={0,2}$/.test(value))
    throw new Error("UNAVAILABLE_IMAGE");
  const bytes = Buffer.from(value, "base64");
  if (!bytes.length || bytes.length > remaining) throw new Error("UNAVAILABLE_IMAGE");
  return bytes;
}

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
 * and PII redaction are passed as restrictions the core cannot loosen; base64 is read and
 * decoded only inside core admission.
 */
export function captureCursorImages(
  manager: AttachmentManager | undefined,
  span: Span,
  message: unknown,
  policy: ContentPolicy,
  traceConfig?: TraceConfigOptions,
): readonly AttachmentHandle[] {
  try {
    if (!manager || policy.hideInputs || manager.status().state !== "queued") return [];
    const denied = cursorImageCaptureDenied(policy, traceConfig);
    if (!message || typeof message !== "object") return [];
    const images = (message as { images?: unknown }).images;
    if (!Array.isArray(images)) return [];
    const handles: AttachmentHandle[] = [];
    let remaining = MAX_CURSOR_IMAGE_BYTES;
    for (let partIndex = 0; partIndex < Math.min(images.length, MAX_IMAGES); partIndex++) {
      try {
        const image = images[partIndex] as { mimeType?: unknown; data?: unknown } | null;
        if (!image || typeof image !== "object") continue;
        const mimeType = image.mimeType;
        if (typeof mimeType !== "string" || !IMAGE_MIMES.has(mimeType)) continue;
        let decoded = 0;
        const handle = manager.attach(span, {
          kind: "Bytes",
          get data() {
            const bytes = decode(image.data, remaining);
            decoded = bytes.length;
            return bytes;
          },
        }, {
          mimeType, direction: "input", purpose: "model_input", role: "user", partIndex,
          capture: denied ? { captureContent: false } : { recordInputs: true, modelInput: true },
        });
        if (handle.admission === "queued") remaining -= decoded;
        handles.push(handle);
      } catch {
        // A throwing or malformed image entry never affects the run.
      }
    }
    return handles;
  } catch {
    return [];
  }
}
