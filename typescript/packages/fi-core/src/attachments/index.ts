/** Browser-safe leaf: no Node filesystem/network imports or ambient credential discovery. */
export { AttachmentManager } from "./manager";
export { AttachmentCorrelation, canonicalAttachmentTraceId, attachmentTraceUuid, attachmentMessageUri } from "./correlation";
export { evaluateAttachmentPolicy, attachmentModality } from "./policy";
export { createAttachmentHttpTransport, attachmentFetchClient, AttachmentTransportError } from "./transport-v1";
export type { AttachmentHttpClient, AttachmentHttpRequest, AttachmentHttpResponse, AttachmentHttpTransportOptions } from "./transport-v1";
export type * from "./types";
