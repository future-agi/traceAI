import type { TracerProvider } from "@opentelemetry/api";
import type {
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

/** Typed inline image shapes: Pi/OMP `{type, mimeType, data}` and Anthropic/Claude Code base64 `source`. */
function imageShape(part: unknown): { mimeType: string; read: () => unknown } | undefined {
  if (!part || typeof part !== "object") return undefined;
  const block = part as { type?: unknown; mimeType?: unknown; data?: unknown; source?: unknown };
  if (block.type !== "image") return undefined;
  if (block.source === undefined) {
    const mimeType = block.mimeType;
    return typeof mimeType === "string" && IMAGE_MIMES.has(mimeType) ? { mimeType, read: () => block.data } : undefined;
  }
  const source = block.source as { type?: unknown; media_type?: unknown; data?: unknown } | null;
  // URL/file sources are retrieval, not inline bytes; they stay unsupported.
  if (!source || typeof source !== "object" || source.type !== "base64") return undefined;
  const mimeType = source.media_type;
  return typeof mimeType === "string" && IMAGE_MIMES.has(mimeType) ? { mimeType, read: () => source.data } : undefined;
}

/**
 * The typed inline image content boundary shared by coding-agent hosts. Not a recursive object walker.
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
  if (!manager || !Array.isArray(blocks) || manager.status().state !== "ready") return [];
  const handles: AttachmentHandle[] = [];
  let remaining = MAX_AGENT_IMAGE_BYTES;
  let spans = admitted.get(manager);
  if (!spans) admitted.set(manager, spans = new WeakMap());
  let seen = spans.get(span);
  if (!seen) spans.set(span, seen = new WeakMap());
  for (let partIndex = 0; partIndex < Math.min(blocks.length, 64); partIndex++) {
    try {
      const part = blocks[partIndex];
      const shape = imageShape(part);
      if (!shape || seen.get(part)?.has(partIndex)) continue;
      let decoded = 0;
      const result = manager.attach(span, {
        kind: "Bytes",
        get data() {
          const value = shape.read();
          if (typeof value !== "string" || value.length > 4 * Math.ceil(remaining / 3))
            throw new Error("UNAVAILABLE_IMAGE");
          const bytes = decodeImage(value);
          if (bytes.length > remaining) throw new Error("UNAVAILABLE_IMAGE");
          decoded = bytes.length;
          return bytes;
        },
      }, { ...options, mimeType: shape.mimeType, partIndex });
      handles.push(result);
      if (result.admission === "queued") {
        // Only bytes the core actually owns count against the per-call budget.
        remaining -= decoded;
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
