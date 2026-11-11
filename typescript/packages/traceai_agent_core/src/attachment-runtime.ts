import {
  ATTACHMENT_TRANSPORT_V1,
  AttachmentTransportError,
  attachmentNodeHttpClient,
  createAttachmentHttpTransportV1,
  type NodeAttachmentManagerOptions,
} from "@traceai/fi-core/attachments/node";
import type { AttachmentTransport, AttachmentTransportV1 } from "@traceai/fi-core/attachments";
import type { ResolvedConfig } from "./config.js";
import { collectionEnabled } from "./collection.js";
import { approvedAttachmentEndpoint } from "./attachment-status.js";

/** The versioned surface of a caller transport: V1 itself, or the V1 member a legacy-shaped HTTP transport carries. */
function versionedTransport(transport: AttachmentTransport | AttachmentTransportV1): AttachmentTransportV1 | undefined {
  const carried = (transport as { [ATTACHMENT_TRANSPORT_V1]?: AttachmentTransportV1 })[ATTACHMENT_TRANSPORT_V1];
  if (carried && typeof carried.putContent === "function") return carried;
  return typeof (transport as AttachmentTransportV1).putContent === "function" ? transport as AttachmentTransportV1 : undefined;
}

/** Every wire call re-checks the provider's collection policy and route revalidation first. */
function guardV1(delegate: AttachmentTransportV1, check: () => Promise<void>): AttachmentTransportV1 {
  return {
    async bootstrap(body, key, signal) { await check(); return delegate.bootstrap(body, key, signal); },
    async renewSession(current, sequence, signal) { await check(); return delegate.renewSession(current, sequence, signal); },
    async initialize(session, body, key, signal) { await check(); return delegate.initialize(session, body, key, signal); },
    async renewUpload(session, upload, body, signal) { await check(); return delegate.renewUpload(session, upload, body, signal); },
    async putContent(session, upload, source, signal, pacer) { await check(); return delegate.putContent(session, upload, source, signal, pacer); },
    async complete(session, upload, key, signal) { await check(); return delegate.complete(session, upload, key, signal); },
    async abort(session, upload, key, signal) { await check(); return delegate.abort(session, upload, key, signal); },
    async uploadStatus(session, upload, signal) { await check(); return delegate.uploadStatus(session, upload, signal); },
    async putManifest(session, capability, recordingId, body, key, signal) {
      await check(); return delegate.putManifest(session, capability, recordingId, body, key, signal);
    },
    async submitImport(session, body, key, signal) { await check(); return delegate.submitImport(session, body, key, signal); },
    async importStatus(session, jobId, signal) { await check(); return delegate.importStatus(session, jobId, signal); },
  };
}

/**
 * A caller-supplied four-method transport stays four-method, so fi-core still marks it legacy and emits
 * FI_ATTACHMENTS_LEGACY_TRANSPORT once (no renewal, recordings or imports). Pass an AttachmentTransportV1 instead.
 */
function guardLegacy(delegate: AttachmentTransport, check: () => Promise<void>): AttachmentTransport {
  return {
    async bootstrap(request, signal) { await check(); return delegate.bootstrap(request, signal); },
    async initialize(session, request, signal) { await check(); return delegate.initialize(session, request, signal); },
    async upload(session, upload, data, signal) { await check(); return delegate.upload(session, upload, data, signal); },
    async complete(session, upload, request, signal) { await check(); return delegate.complete(session, upload, request, signal); },
  };
}

/** One provider-owned runtime with the same credential and revocation boundary as OTLP. */
export function agentAttachmentRuntime(
  config: ResolvedConfig,
  options: NodeAttachmentManagerOptions = {},
): NodeAttachmentManagerOptions {
  // Neither the collector URL nor provider/tool content is a control-plane origin, and the
  // credential goes only to the approved destination's origin (destination approval binds a
  // saved credential to that origin). Any other endpoint stays unconfigured, with no headers.
  const endpoint = approvedAttachmentEndpoint(config, options.endpoint ?? process.env.FI_ATTACHMENTS_ENDPOINT);
  const headers: Readonly<Record<string, string>> = config.credentials && endpoint ? {
    "x-api-key": config.credentials.apiKey,
    "x-secret-key": config.credentials.secretKey,
  } : {};
  const allowed = () => collectionEnabled(config) && config.captureContent &&
    config.destinationApproved && !!config.credentials;
  const check = async () => {
    if (!allowed() || (config.revalidate && !(await config.revalidate())))
      throw new AttachmentTransportError("policy_denied");
  };
  let delegate: AttachmentTransportV1 | undefined;
  let legacy: AttachmentTransport | undefined;
  if (options.transport) {
    delegate = versionedTransport(options.transport);
    if (!delegate) legacy = options.transport as AttachmentTransport;
  } else if (endpoint && allowed()) {
    try { delegate = createAttachmentHttpTransportV1({ endpoint, headers, client: attachmentNodeHttpClient }); }
    catch { /* Unsupported/untrusted control origin remains unconfigured. */ }
  }
  // Always hand the manager a transport, so fi-core never builds its own from
  // FI_ATTACHMENTS_ENDPOINT. Without an approved endpoint every call is endpoint_unconfigured.
  // The stub is V1-shaped so an unapproved endpoint is not reported as a legacy transport.
  const unconfigured = async (): Promise<never> => { throw new AttachmentTransportError("endpoint_unconfigured"); };
  const transport = legacy ? guardLegacy(legacy, check) : guardV1(delegate ?? {
    bootstrap: unconfigured, renewSession: unconfigured, initialize: unconfigured, renewUpload: unconfigured,
    putContent: unconfigured, complete: unconfigured, abort: unconfigured, uploadStatus: unconfigured,
    putManifest: unconfigured, submitImport: unconfigured, importStatus: unconfigured,
  }, check);
  return {
    ...options, endpoint, headers, transport,
    enabled: allowed() ? options.enabled : false,
    projectName: config.projectName,
    projectType: "observe",
    environment: () => allowed() ? process.env : { FI_ATTACHMENTS_ENABLED: "false" },
    traceConfig: {
      ...options.traceConfig,
      hideInputs: config.hideInputs || options.traceConfig?.hideInputs,
      hideOutputs: config.hideOutputs || options.traceConfig?.hideOutputs,
      hideErrorDetails: config.hideErrorDetails || options.traceConfig?.hideErrorDetails,
    },
  };
}
