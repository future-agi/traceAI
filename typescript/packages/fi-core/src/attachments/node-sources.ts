/**
 * Node-only owned recording files (design section 4). Reachable only from `./attachments/node`.
 *
 * Guarantees, stated honestly (design 4.4): the root must be canonical (realpath equals the path) with no symlink
 * component; every intermediate component is lstat'ed (a directory, not a symlink) immediately before the leaf is
 * opened with O_NOFOLLOW; the opened handle must match the identified (dev, ino, size) and its realpath must equal the
 * expected path. RESIDUAL RACE: an intermediate directory swapped to a symlink between the lstat and the open can
 * redirect the leaf; the identity bind and the post-open realpath check are the mitigation. Windows is unsupported.
 */
import { constants, promises as fs } from "node:fs";
import * as path from "node:path";
import { registerAttachmentOwnedFile, type AttachmentOwnedLease } from "./owned";
import { AttachmentTransportError } from "./transport-v1";
import type { AttachmentFileIdentity, AttachmentOwnedFileRef, AttachmentRecordingFileScope } from "./types";

const unavailable = () => new AttachmentTransportError("source_unavailable");
function validRelative(relativePath: string): string[] | undefined {
  if (typeof relativePath !== "string" || !relativePath || relativePath.length > 4096 || relativePath.includes("\0") || path.isAbsolute(relativePath)) return undefined;
  const parts = relativePath.split("/");
  if (parts.some(part => !part || part === "." || part === ".." || part.includes("\\"))) return undefined;
  return parts;
}
async function noSymlinkDirectory(full: string): Promise<void> {
  const info = await fs.lstat(full);
  if (info.isSymbolicLink() || !info.isDirectory()) throw unavailable();
}
async function openLeaf(root: string, parts: readonly string[]) {
  let current = root;
  for (const part of parts.slice(0, -1)) { current = path.join(current, part); await noSymlinkDirectory(current); }
  const full = path.join(root, ...parts);
  const handle = await fs.open(full, constants.O_RDONLY | constants.O_NOFOLLOW | (constants.O_NONBLOCK ?? 0));
  try {
    const stat = await handle.stat({ bigint: true });
    if (!stat.isFile()) throw unavailable();
    if (await fs.realpath(full) !== full) throw unavailable();
    return { handle, stat, full };
  } catch (error) { await handle.close().catch(() => undefined); throw error; }
}
class Scope implements AttachmentRecordingFileScope {
  private closedFlag = false;
  private readonly leases = new Set<AttachmentOwnedLease>();
  constructor(private readonly root: string) {}
  closed(): boolean { return this.closedFlag; }
  async identify(relativePath: string): Promise<AttachmentFileIdentity> {
    const parts = validRelative(relativePath);
    if (this.closedFlag || !parts) throw unavailable();
    const { handle, stat } = await openLeaf(this.root, parts).catch(() => { throw unavailable(); });
    await handle.close().catch(() => undefined);
    if (stat.size > BigInt(Number.MAX_SAFE_INTEGER)) throw unavailable();
    return Object.freeze({ dev: stat.dev, ino: stat.ino, size: Number(stat.size) });
  }
  source(relativePath: string, identity: AttachmentFileIdentity): { kind: "OwnedRecordingFile"; lease: AttachmentOwnedFileRef } {
    const parts = validRelative(relativePath);
    if (!parts || !identity || typeof identity.dev !== "bigint" || typeof identity.ino !== "bigint" || !Number.isSafeInteger(identity.size)) {
      // A forged or invalid identity yields a ref that always fails closed.
      return { kind: "OwnedRecordingFile", lease: registerAttachmentOwnedFile({ identity: { dev: BigInt(0), ino: BigInt(0), size: 0 }, open: async () => { throw unavailable(); }, closed: () => true }) };
    }
    const fixed = Object.freeze({ dev: identity.dev, ino: identity.ino, size: identity.size });
    const ref = registerAttachmentOwnedFile({
      identity: fixed,
      closed: () => this.closedFlag,
      open: async () => {
        if (this.closedFlag) throw unavailable();
        const { handle, stat } = await openLeaf(this.root, parts).catch(() => { throw unavailable(); });
        // Identity bind: a substituted file must also reproduce (dev, ino, size).
        if (stat.dev !== fixed.dev || stat.ino !== fixed.ino || stat.size !== BigInt(fixed.size)) { await handle.close().catch(() => undefined); throw unavailable(); }
        const snapshot = { dev: stat.dev, ino: stat.ino, size: stat.size, mtime: stat.mtimeNs, ctime: stat.ctimeNs };
        let open = true;
        const lease: AttachmentOwnedLease = {
          size: fixed.size,
          // Positional reads on our own handle never move an application's file position.
          read: async (offset, length) => {
            if (!open || this.closedFlag) throw unavailable();
            const buffer = new Uint8Array(length); let done = 0;
            while (done < length) {
              const { bytesRead } = await handle.read(buffer, done, length - done, offset + done);
              if (!bytesRead) break; done += bytesRead;
            }
            return done === length ? buffer : buffer.subarray(0, done);
          },
          check: async () => {
            if (!open || this.closedFlag) throw unavailable();
            const now = await handle.stat({ bigint: true });
            if (now.dev !== snapshot.dev || now.ino !== snapshot.ino || now.size !== snapshot.size || now.mtimeNs !== snapshot.mtime || now.ctimeNs !== snapshot.ctime) throw unavailable();
          },
          close: async () => { if (!open) return; open = false; this.leases.delete(lease); await handle.close().catch(() => undefined); },
        };
        this.leases.add(lease);
        return lease;
      },
    });
    return { kind: "OwnedRecordingFile", lease: ref };
  }
  async close(): Promise<void> {
    this.closedFlag = true;
    await Promise.all([...this.leases].map(lease => lease.close()));
  }
}
/** An explicitly bound session's recording root. A report path alone is NOT authority (F01). */
export async function createRecordingFileScope(root: string, options: { enabled: true }): Promise<AttachmentRecordingFileScope> {
  if (!options || options.enabled !== true || process.platform === "win32" || typeof root !== "string" || !path.isAbsolute(root)) throw unavailable();
  const normalized = path.resolve(root);
  if (normalized !== root.replace(/\/+$/, "") && normalized !== root) throw unavailable();
  let real: string;
  try { real = await fs.realpath(normalized); } catch { throw unavailable(); }
  if (real !== normalized) throw unavailable();
  // lstat every root component: none may be a symlink.
  const parts = normalized.split(path.sep).filter(Boolean);
  let current: string = path.sep;
  for (const part of parts) { current = path.join(current, part); await noSymlinkDirectory(current).catch(() => { throw unavailable(); }); }
  return new Scope(normalized);
}
