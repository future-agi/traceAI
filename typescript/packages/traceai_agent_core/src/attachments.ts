import type { TracerProvider } from "@opentelemetry/api";
import type {
  AttachmentCapturePolicy,
  AttachmentHandle,
  AttachmentManager,
  AttachmentOptions,
  AttachmentSpan,
} from "@traceai/fi-core/attachments";

/** A bounded callback budget, not a claim that every provider image is capturable. */
export const MAX_AGENT_IMAGE_BYTES = 256 * 1024;
const MAX_BASE64_LENGTH = 4 * Math.ceil(MAX_AGENT_IMAGE_BYTES / 3);
const IMAGE_MIMES = new Set(["image/png", "image/jpeg", "image/webp"]);
// Identity-only replay guard. Distinct parts/positions/spans keep distinct occurrences.
const admitted = new WeakMap<AttachmentManager, WeakMap<object, WeakMap<object, Set<number>>>>();
export type AgentImageOptions = Omit<AttachmentOptions, "mimeType" | "partIndex">;

/** Reuse the configured provider's manager. Never create another uploader or inspect credentials. */
export function providerAttachments(provider: TracerProvider): AttachmentManager | undefined {
  const candidate = provider as TracerProvider & {
    attachments?: AttachmentManager;
    getDelegate?: () => TracerProvider;
  };
  return candidate.attachments ?? (candidate.getDelegate?.() as typeof candidate | undefined)?.attachments;
}

function decodeImage(value: unknown): Uint8Array {
  if (typeof value !== "string" || !value.length || value.length > MAX_BASE64_LENGTH ||
      value.length % 4 !== 0 || !/^[A-Za-z0-9+/]*={0,2}$/.test(value))
    throw new Error("UNAVAILABLE_IMAGE");
  const bytes = Buffer.from(value, "base64");
  if (!bytes.length || bytes.length > MAX_AGENT_IMAGE_BYTES) throw new Error("UNAVAILABLE_IMAGE");
  return bytes;
}

/**
 * Pi/OMP's typed image content boundary. Not a recursive object walker.
 * Data access and base64 decoding happen inside core admission, after policy,
 * bootstrap, sampling, correlation and descriptor checks. No paths/URLs fetched.
 * The caller must select the original occurrence, not replayed provider history.
 */
export function captureAgentImages(
  manager: AttachmentManager | undefined,
  span: AttachmentSpan,
  blocks: unknown,
  options: AgentImageOptions,
): readonly AttachmentHandle[] {
  if (!manager || !Array.isArray(blocks) || manager.status().state !== "queued") return [];
  const handles: AttachmentHandle[] = [];
  let remaining = MAX_AGENT_IMAGE_BYTES;
  let spans = admitted.get(manager);
  if (!spans) admitted.set(manager, spans = new WeakMap());
  let seen = spans.get(span);
  if (!seen) spans.set(span, seen = new WeakMap());
  for (let partIndex = 0; partIndex < Math.min(blocks.length, 64); partIndex++) {
    try {
      const part = blocks[partIndex];
      if (!part || part.type !== "image" || !IMAGE_MIMES.has(part.mimeType) || seen.get(part)?.has(partIndex)) continue;
      const result = manager.attach(span, {
        kind: "Bytes",
        get data() {
          const value = part.data;
          if (typeof value !== "string" || value.length > 4 * Math.ceil(remaining / 3))
            throw new Error("UNAVAILABLE_IMAGE");
          const bytes = decodeImage(value);
          if (bytes.length > remaining) throw new Error("UNAVAILABLE_IMAGE");
          remaining -= bytes.length;
          return bytes;
        },
      }, { ...options, mimeType: part.mimeType, partIndex });
      handles.push(result);
      if (result.admission === "queued") {
        let positions = seen.get(part);
        if (!positions) seen.set(part, positions = new Set());
        positions.add(partIndex);
      }
    } catch {
      // Host data, including throwing accessors, must not affect inference or diagnostics.
    }
  }
  return handles;
}

/** Restrictions only: adapter defaults cannot undo the provider's hide/PII policy. */
export function agentImagePolicy(policy: {
  hideInputs: boolean;
  hideOutputs: boolean;
  hideErrorDetails: boolean;
}, error = false): AttachmentCapturePolicy {
  return {
    recordInputs: !policy.hideInputs,
    recordOutputs: !policy.hideOutputs,
    errorDetail: error,
    requested: !(error && policy.hideErrorDetails),
  };
}
