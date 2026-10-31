import Database from "better-sqlite3";
import { closeSync, constants, fstatSync, lstatSync, openSync } from "node:fs";
import { AgentError } from "./files.js";

const owned = new Set<string>();
/**
 * A kernel-owned administrative mutex, not a capture database. SQLite's pinned
 * Unix VFS supplies cross-process advisory locking on an empty, permanent inode.
 * No tables, writes, journals, PID guessing, lease expiry, or unlink on release.
 * The sole spool connection remains in the storage child.
 */
export function configurationLock(path: string): () => void {
  // A second same-process open/close can release POSIX inode locks. Reject it
  // before opening a validation descriptor, not just at BEGIN EXCLUSIVE.
  if (owned.has(path)) throw new AgentError("CONFIGURATION_CONFLICT", 6);
  // Older previews used a directory with no owner identity. Never guess whether
  // that owner died: upgrading a live old CLI must not create a second writer.
  try {
    if (lstatSync(path).isDirectory())
      throw new AgentError("LEGACY_CONFIG_LOCK_REQUIRES_RECOVERY", 6);
  } catch (e) {
    if ((e as NodeJS.ErrnoException).code !== "ENOENT") throw e;
  }
  const fd = openSync(
    path,
    constants.O_RDWR | constants.O_CREAT | constants.O_NOFOLLOW,
    0o600,
  );
  let stat;
  try {
    stat = fstatSync(fd);
    if (
      !stat.isFile() ||
      stat.nlink !== 1 ||
      stat.size !== 0 ||
      stat.uid !== process.getuid?.() ||
      (stat.mode & 0o077) !== 0
    )
      throw new AgentError("UNSAFE_CONFIG_LOCK", 6);
  } finally {
    closeSync(fd); // Must precede lock acquisition; no other inode handles remain.
  }
  let lock: Database.Database | undefined;
  try {
    lock = new Database(path, { timeout: 0 });
    const current = lstatSync(path);
    if (stat.ino !== current.ino || stat.dev !== current.dev)
      throw new AgentError("CONFIGURATION_CONFLICT", 6);
    lock.exec("BEGIN EXCLUSIVE");
    owned.add(path);
    return () => {
      try {
        lock!.close();
      } finally {
        owned.delete(path);
      }
    };
  } catch (e) {
    lock?.close();
    if ((e as { code?: string }).code === "SQLITE_BUSY")
      throw new AgentError("CONFIGURATION_CONFLICT", 6);
    throw e;
  }
}
