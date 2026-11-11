/**
 * One-time, process-visible diagnostics for integration mistakes that would otherwise only show up as attachments
 * quietly not arriving. Browser-safe: no `node:*` import. Messages carry fixed text and shape names only, never values,
 * URLs, capabilities or credentials.
 */
export const ATTACHMENT_WARNING_LEGACY_TRANSPORT = "FI_ATTACHMENTS_LEGACY_TRANSPORT";
export const ATTACHMENT_WARNING_WIRE_REJECTED = "FI_ATTACHMENTS_WIRE_REJECTED";
const MIGRATION = "See ATTACHMENTS-MIGRATION.md in @traceai/fi-core.";
const seen = new Set<string>();

type Emitter = (message: string, options: { type: string; code: string }) => void;
/** Emits once per key per process: `process.emitWarning` on Node (honours --no-deprecation/--no-warnings), else `console.warn`. */
export function attachmentWarnOnce(key: string, code: string, message: string, deprecation = false): void {
  if (seen.has(key)) return;
  seen.add(key);
  try {
    const proc = (globalThis as { process?: { emitWarning?: Emitter } }).process;
    if (proc && typeof proc.emitWarning === "function") { proc.emitWarning(`${message} ${MIGRATION}`, { type: deprecation ? "DeprecationWarning" : "Warning", code }); return; }
    const out = (globalThis as { console?: { warn?: (...args: unknown[]) => void } }).console;
    if (out && typeof out.warn === "function") out.warn(`[${code}] ${message} ${MIGRATION}`);
  } catch { /* Diagnostics never break application code (e.g. --throw-deprecation). */ }
}

/** A four-method `AttachmentTransport` was adapted: say exactly what it cannot do, once. */
export function attachmentWarnLegacyTransport(): void {
  attachmentWarnOnce("legacy-transport", ATTACHMENT_WARNING_LEGACY_TRANSPORT,
    "A legacy four-method AttachmentTransport (bootstrap/initialize/upload/complete) was passed to AttachmentManager. " +
    "It cannot renew sessions, PUT recording manifests, submit imports or read upload status: attachments stop with `expired` " +
    "after the first session lifetime, and startRecording, WAV-tier attachOwnedRecording and importCompletedRecording are denied with " +
    "`unsupported_version`. Pass createAttachmentHttpTransportV1(...) or an AttachmentTransportV1; a wrapper must forward the " +
    "ATTACHMENT_TRANSPORT_V1 member of the transport it wraps.", true);
}

/** A response failed its F01 shape check. Fails closed as `unsupported_version` and names the shape once. */
export function attachmentWireRejected<E>(shape: string, error: E): E {
  attachmentWarnOnce(`wire:${shape}`, ATTACHMENT_WARNING_WIRE_REJECTED,
    `An attachment API response failed the F01 ${shape} check, so the affected attachments report \`unsupported_version\`. ` +
    "Check the server, or the test double, against spec/attachments/v1 (every status body carries `reason`).");
  return error;
}
