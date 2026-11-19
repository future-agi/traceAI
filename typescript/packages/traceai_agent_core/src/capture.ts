import { TRANSPORT_CONTRACT } from "./config.js";
import { minimize } from "./privacy.js";
import {
  LIMITS,
  STRUCTURE_POLICY,
  type CanonicalFact,
  type Policy,
} from "./schema.js";

/** A separate, non-durable protocol for a future managed worker. Not the preview observe/commit protocol. */
export interface FinalizedCaptureRequest {
  protocolVersion: 1;
  transport: typeof TRANSPORT_CONTRACT;
  routeId: string;
  capability: string;
  fact: CanonicalFact;
}
export interface FinalizedCaptureAck {
  protocolVersion: 1;
  durable: false;
  code:
    | "QUEUED"
    | "DISABLED"
    | "QUEUE_LIMIT"
    | "INVALID_FACT"
    | "LOCAL_UNAVAILABLE";
}
/** Called before IPC: sanitize first, then bound the frame. Destinations are never event-supplied. */
export function encodeFinalizedCapture(
  request: FinalizedCaptureRequest,
  policy: Policy = STRUCTURE_POLICY,
): Buffer {
  if (
    request.protocolVersion !== 1 ||
    request.transport !== TRANSPORT_CONTRACT ||
    !/^[a-zA-Z0-9_-]{16,128}$/.test(request.routeId) ||
    !/^[a-zA-Z0-9_-]{32,128}$/.test(request.capability)
  )
    throw new Error("INVALID_CAPTURE_ENVELOPE");
  const body = Buffer.from(
    JSON.stringify({
      protocolVersion: 1,
      transport: TRANSPORT_CONTRACT,
      routeId: request.routeId,
      capability: request.capability,
      fact: minimize(request.fact, policy),
    }),
  );
  if (body.length > LIMITS.ingress) throw new Error("OVERSIZE_FRAME");
  const length = Buffer.alloc(4);
  length.writeUInt32BE(body.length);
  return Buffer.concat([length, body]);
}
