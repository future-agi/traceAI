import {
  AttachmentTransportError,
  attachmentNodeHttpClient,
  createAttachmentHttpTransport,
  type NodeAttachmentManagerOptions,
} from "@traceai/fi-core/attachments/node";
import type { AttachmentTransport } from "@traceai/fi-core/attachments";
import type { ResolvedConfig } from "./config.js";
import { collectionEnabled } from "./collection.js";

/** One provider-owned runtime with the same credential and revocation boundary as OTLP. */
export function agentAttachmentRuntime(
  config: ResolvedConfig,
  options: NodeAttachmentManagerOptions = {},
): NodeAttachmentManagerOptions {
  // Neither the collector URL nor provider/tool content is a control-plane origin.
  const endpoint = options.endpoint ?? process.env.FI_ATTACHMENTS_ENDPOINT;
  const headers: Readonly<Record<string, string>> = config.credentials ? {
    "x-api-key": config.credentials.apiKey,
    "x-secret-key": config.credentials.secretKey,
  } : {};
  const allowed = () => collectionEnabled(config) && config.captureContent &&
    config.destinationApproved && !!config.credentials;
  let transport = options.transport;
  if (!transport && endpoint && allowed()) {
    try { transport = createAttachmentHttpTransport({ endpoint, headers, client: attachmentNodeHttpClient }); }
    catch { /* Unsupported/untrusted control origin remains unconfigured. */ }
  }
  const delegate = transport;
  const check = async () => {
    if (!allowed() || (config.revalidate && !(await config.revalidate())))
      throw new AttachmentTransportError("policy_denied");
  };
  const guarded: AttachmentTransport | undefined = delegate && {
    async bootstrap(request, signal) { await check(); return delegate.bootstrap(request, signal); },
    async initialize(session, request, signal) { await check(); return delegate.initialize(session, request, signal); },
    async upload(session, upload, data, signal) { await check(); return delegate.upload(session, upload, data, signal); },
    async complete(session, upload, request, signal) { await check(); return delegate.complete(session, upload, request, signal); },
  };
  return {
    ...options, endpoint, headers, transport: guarded,
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
