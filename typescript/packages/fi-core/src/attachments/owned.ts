/** Browser-safe registry of owned-file references minted by this core copy's Node scope (design 4.3). */
import type { AttachmentFileIdentity, AttachmentOwnedFileRef } from "./types";

/** @internal An open lease over one owned file. Reads never move any application file position. */
export interface AttachmentOwnedLease {
  readonly size: number;
  read(offset: number, size: number): Promise<Uint8Array>;
  /** Throws source_unavailable when (dev, ino, size, mtime, ctime) changed since open. */
  check(): Promise<void>;
  close(): Promise<void>;
}
/** @internal */
export interface AttachmentOwnedFileRecord {
  readonly identity: AttachmentFileIdentity;
  open(): Promise<AttachmentOwnedLease>;
  closed(): boolean;
}
const records = new WeakMap<object, AttachmentOwnedFileRecord>();
/** @internal Only the Node scope calls this. */
export function registerAttachmentOwnedFile(record: AttachmentOwnedFileRecord): AttachmentOwnedFileRef {
  const ref = Object.freeze(Object.create(null)) as AttachmentOwnedFileRef;
  records.set(ref as unknown as object, record);
  return ref;
}
/** @internal A foreign or forged object resolves to undefined (source_unavailable). */
export function attachmentOwnedFile(ref: unknown): AttachmentOwnedFileRecord | undefined {
  return ref && typeof ref === "object" ? records.get(ref) : undefined;
}
