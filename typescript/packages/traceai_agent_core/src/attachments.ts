import { randomUUID } from "node:crypto";
import type { TracerProvider } from "@opentelemetry/api";
import type {
  AttachmentAdmission,
  AttachmentAdmissionTicket,
  AttachmentHandle,
  AttachmentManager,
  AttachmentOptions,
  AttachmentSpan,
} from "@traceai/fi-core/attachments";

/** A bounded callback budget, not a claim that every provider image is capturable. */
export const MAX_AGENT_IMAGE_BYTES = 256 * 1024;
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

/** A terminal handle for a part the adapter refused after admission; the ticket is already released. */
function refused(admission: AttachmentAdmission): AttachmentHandle {
  return Object.freeze({ attachmentId: randomUUID(), occurrenceId: randomUUID(), admission,
    completion: Promise.resolve(Object.freeze({ status: admission })) });
}
const NO_SOURCE = Object.freeze({ kind: "Bytes" as const, data: new Uint8Array(0) });

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
 * Each part is preflighted (deny-wins policy, sampling, correlation, budget) BEFORE its base64 is read;
 * only an admitted part is read, strictly validated, decoded and handed to `ticket.attach`. A part the
 * per-call budget or the manager's limits refuse is re-preflighted once at its exact decoded length
 * (policy already passed, so reading the string length is allowed). No paths/URLs fetched.
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
  const { direction, purpose, capture, ...fields } = options;
  for (let partIndex = 0; partIndex < Math.min(blocks.length, 64); partIndex++) {
    let ticket: AttachmentAdmissionTicket | undefined;
    try {
      const part = blocks[partIndex];
      const shape = imageShape(part);
      if (!shape || seen.get(part)?.has(partIndex)) continue;
      const preflight = (maxBytes: number) => manager.preflight(span, { mimeType: shape.mimeType, direction, purpose, capture, maxBytes });
      ticket = preflight(remaining);
      let value: unknown;
      let read = false;
      if (!ticket.admitted && (ticket.reason === "too_large" || ticket.reason === "queue_full")) {
        // Budget denials come only after the policy gate passed.
        value = shape.read(); read = true;
        const exact = strictBase64Length(value, remaining);
        if (typeof exact === "number") ticket = preflight(exact);
      }
      // A denied ticket mints its own terminal handle without touching the source.
      if (!ticket.admitted) { handles.push(ticket.attach(NO_SOURCE)); ticket = undefined; continue; }
      if (!read) value = shape.read();
      const length = strictBase64Length(value, Math.min(remaining, ticket.maxBytes));
      if (typeof length !== "number") {
        ticket.release(); ticket = undefined;
        handles.push(refused(length === "too_large" ? "too_large" : "source_unavailable"));
        continue;
      }
      const result = ticket.attach({ kind: "Bytes", data: Buffer.from(value as string, "base64") }, { ...fields, partIndex });
      ticket = undefined;
      handles.push(result);
      if (result.admission === "queued") {
        // Only bytes the core actually owns count against the per-call budget.
        remaining -= length;
        let positions = seen.get(part);
        if (!positions) seen.set(part, positions = new Set());
        positions.add(partIndex);
      }
    } catch {
      // Host data, including throwing accessors, must not affect inference or diagnostics.
      // A part that was preflighted reports why; a malformed block before preflight is skipped.
      if (ticket) { ticket.release(); handles.push(refused("source_unavailable")); }
    }
  }
  return handles;
}
