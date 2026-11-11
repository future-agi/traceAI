/** Browser-safe leaf: no Node filesystem/network imports or ambient credential discovery. */
export { AttachmentManager } from "./manager";
export { DisabledAttachmentManager } from "./disabled";
export type { AttachmentManagerLike } from "./disabled";
export { AttachmentTicket } from "./admission";
export { AttachmentConsumedStream } from "./stream";
export { AttachmentRecording, attachmentBoundaryStep, attachmentSegmentFrames, attachmentFramesToMs, attachmentWavHeader, validAttachmentManifest } from "./recording";
export { AttachmentUnavailableResolver } from "./imports";
export { validAttachmentWire, ATTACHMENT_WIRE_SHAPES } from "./wire";
export { AttachmentCorrelation, canonicalAttachmentTraceId, attachmentTraceUuid, attachmentMessageUri } from "./correlation";
export { evaluateAttachmentPolicy, attachmentModality } from "./policy";
export {
  createAttachmentHttpTransport, createAttachmentHttpTransportV1, attachmentFetchClient, AttachmentTransportError, attachmentHttpError,
  attachmentIdempotencyKey, attachmentRetryAfterMs, attachmentStrictJson, attachmentLegacyTransport, ATTACHMENT_TRANSPORT_V1,
} from "./transport-v1";
export type { AttachmentHttpClient, AttachmentHttpRequest, AttachmentHttpResponse, AttachmentHttpTransportOptions, AttachmentErrorCode } from "./transport-v1";
export type * from "./types";
