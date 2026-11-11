import type { Link } from "@opentelemetry/api";

/**
 * An application-server-authorized identity, NOT a browser upload capability.
 * Obtain it from your authenticated server, never from an RTC/bot event payload.
 * The occurrence remains on its original server parent; a link grants no access.
 */
export interface PipecatServerRecording {
  readonly schemaVersion: 1;
  readonly parent: { readonly traceId: string; readonly spanId: string };
  readonly occurrenceId: string;
  /** Opaque session UUID; no participant names or provider credentials. */
  readonly conversationId: string;
}

const uuid = (value: unknown): value is string => typeof value === "string" &&
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(value) &&
  value !== "00000000-0000-0000-0000-000000000000";

/** Content-free correlation only. It never calls an upload, relink or resolve API. */
export function pipecatRecordingLink(value: PipecatServerRecording | undefined): Link | undefined {
  try {
    if (value?.schemaVersion !== 1) return;
    // Preserve all trace bits; this reference-only browser leaf must not import
    // the upload manager merely to normalize the two contract spellings.
    const sourceTraceId = value.parent?.traceId;
    const traceId = uuid(sourceTraceId) ? sourceTraceId.replace(/-/g, "") : sourceTraceId;
    const spanId = value.parent?.spanId;
    const occurrenceId = value.occurrenceId;
    const conversationId = value.conversationId;
    if (typeof traceId !== "string" || !/^[0-9a-f]{32}$/.test(traceId) || /^0+$/.test(traceId) ||
        typeof spanId !== "string" || !/^[0-9a-f]{16}$/.test(spanId) ||
        /^0+$/.test(spanId) || !uuid(occurrenceId) || !uuid(conversationId)) return;
    return {
      context: { traceId, spanId, traceFlags: 0, isRemote: true },
      attributes: {
        "pipecat.server_recording.occurrence_id": occurrenceId,
        "pipecat.server_recording.conversation_id": conversationId,
      },
    };
  } catch {
    // Malformed or unavailable server identity must not interrupt RTC callbacks.
    return;
  }
}
