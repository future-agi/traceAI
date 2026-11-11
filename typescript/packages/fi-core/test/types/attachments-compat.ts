/*
 * Type-level guard for ATTACHMENTS-MIGRATION.md, compiled against the BUILT declarations (what consumers see).
 * Each `@ts-expect-error` must be consumed; if a retired value becomes assignable again, tsc fails with TS2578.
 */
import type {
  AttachmentCompletion, AttachmentManager, AttachmentManagerOptions, AttachmentStatus, AttachmentTransport, AttachmentTransportV1,
} from "../../dist/src/attachments/index";

declare const manager: AttachmentManager;
declare const status: AttachmentStatus;
declare const completion: AttachmentCompletion;
declare const legacy: AttachmentTransport;
declare const wire: AttachmentTransportV1;

// Ready state: "queued" -> "ready" (design 0.2).
// @ts-expect-error TS2367: "queued" is no longer a runtime state; compare with "ready".
export const oldReady = manager.status().state !== "queued";
// @ts-expect-error TS2367: same check through a stored status object.
export const oldReadyStored = status.state === "queued";
export const newReady: boolean = manager.status().state === "ready";

// Completion: "failed" retired for "incomplete" / "unsupported_version" (design 0.3).
// @ts-expect-error TS2367: "failed" is never produced.
export const oldFailed = completion.status === "failed";
export const newFailed: boolean = completion.status === "incomplete" || completion.status === "unsupported_version";

// Admission results keep "queued": that is the admitted reason, not a runtime state.
export const admitted: boolean = manager.attach(undefined, { kind: "Bytes", data: new Uint8Array(1) }, { mimeType: "image/png", direction: "input" }).admission === "queued";

// Both transport shapes still type-check; the legacy one is warned about at runtime.
export const legacyOptions: AttachmentManagerOptions = { transport: legacy };
export const v1Options: AttachmentManagerOptions = { transport: wire };
