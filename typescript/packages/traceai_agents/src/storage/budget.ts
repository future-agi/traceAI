import {
  closeSync,
  constants,
  fstatSync,
  fsyncSync,
  lstatSync,
  openSync,
  readdirSync,
  statfsSync,
  writeSync,
} from "node:fs";
import { join } from "node:path";
import { AgentError, protectedDirectory } from "../security/files.js";
export const MiB = 1_048_576;
export const STORAGE_LIMITS = Object.freeze({
  cap: 256 * MiB,
  highWater: 192 * MiB,
  db: 64 * MiB,
  pages: 16384,
  pageSize: 4096,
  reserve: 16 * MiB,
  logical: 40 * MiB,
  route: 16 * MiB,
  wal: 32 + 16384 * (4096 + 24),
});
const owned = new Set(["spool.db", "spool.db-wal", "spool.db-shm", "reserve"]);
export function measure(root: string): {
  physical: number;
  length: number;
  wal: number;
} {
  let physical = 0,
    length = 0,
    wal = 0;
  for (const name of readdirSync(root)) {
    const s = lstatSync(join(root, name));
    if (
      !owned.has(name) ||
      !s.isFile() ||
      s.isSymbolicLink() ||
      s.nlink !== 1 ||
      s.uid !== process.getuid?.() ||
      s.mode & 0o077
    )
      throw new AgentError("PAUSED_UNEXPECTED_STATE");
    physical += Math.max(s.size, s.blocks * 512);
    length += s.size;
    if (name === "spool.db-wal") wal = s.size;
  }
  return { physical, length, wal };
}
export function reserve(root: string): void {
  protectedDirectory(root, true);
  const path = join(root, "reserve");
  try {
    const fd = openSync(
      path,
      constants.O_WRONLY |
        constants.O_CREAT |
        constants.O_EXCL |
        constants.O_NOFOLLOW,
      0o600,
    );
    try {
      const block = Buffer.alloc(MiB);
      for (let i = 0; i < 16; i++) writeSync(fd, block);
      fsyncSync(fd);
    } finally {
      closeSync(fd);
    }
  } catch (e) {
    if ((e as NodeJS.ErrnoException).code !== "EEXIST") throw e;
  }
  const fd = openSync(path, constants.O_RDONLY | constants.O_NOFOLLOW);
  try {
    const s = fstatSync(fd);
    if (
      s.size !== STORAGE_LIMITS.reserve ||
      s.blocks * 512 < STORAGE_LIMITS.reserve
    )
      throw new AgentError("PAUSED_RESERVE");
  } finally {
    closeSync(fd);
  }
}
/** One whole-DB WAL transaction plus worst-case DB expansion is reserved before BEGIN. */
export function preflight(root: string): ReturnType<typeof measure> {
  const current = measure(root);
  if (
    current.physical > STORAGE_LIMITS.highWater ||
    current.wal > STORAGE_LIMITS.wal
  )
    throw new AgentError("PAUSED_PHYSICAL_QUOTA");
  const disk = statfsSync(root);
  const worstRemaining =
    STORAGE_LIMITS.db + STORAGE_LIMITS.wal + MiB + STORAGE_LIMITS.reserve;
  if (
    Math.max(current.physical, worstRemaining) > STORAGE_LIMITS.cap ||
    disk.bavail * disk.bsize < worstRemaining
  )
    throw new AgentError("PAUSED_DISK_HEADROOM");
  return current;
}
