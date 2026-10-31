import {
  constants,
  closeSync,
  fstatSync,
  fsyncSync,
  lstatSync,
  linkSync,
  mkdirSync,
  openSync,
  readSync,
  realpathSync,
  renameSync,
  statSync,
  unlinkSync,
  writeFileSync,
} from "node:fs";
import { dirname, isAbsolute, join, resolve } from "node:path";
import { randomUUID } from "node:crypto";
export class AgentError extends Error {
  constructor(
    readonly code: string,
    readonly exitCode = 7,
  ) {
    super(code);
  }
}
export function protectedDirectory(path: string, create = false): string {
  if (process.platform === "win32")
    throw new AgentError("UNQUALIFIED_PLATFORM", 5);
  if (!isAbsolute(path) || path !== resolve(path))
    throw new AgentError("UNSAFE_PATH", 2);
  // Walk every component before creating anything. Never follow symlink parents.
  let at = "/";
  for (const part of path.split("/").filter(Boolean)) {
    at = join(at, part);
    try {
      const s = lstatSync(at);
      if (s.isSymbolicLink() || !s.isDirectory())
        throw new AgentError("UNSAFE_PATH");
    } catch (e) {
      if ((e as NodeJS.ErrnoException).code !== "ENOENT" || !create) throw e;
      mkdirSync(at, { mode: 0o700 });
    }
  }
  const s = statSync(path);
  if (
    s.uid !== process.getuid?.() ||
    (s.mode & 0o077) !== 0 ||
    realpathSync(path) !== path
  )
    throw new AgentError("UNSAFE_PERMISSIONS");
  return path;
}
export function readProtected(path: string, max = 65_536): Buffer {
  protectedDirectory(dirname(path));
  const fd = openSync(path, constants.O_RDONLY | constants.O_NOFOLLOW);
  try {
    const s = fstatSync(fd);
    if (
      !s.isFile() ||
      s.nlink !== 1 ||
      s.uid !== process.getuid?.() ||
      (s.mode & 0o077) !== 0 ||
      s.size > max
    )
      throw new AgentError("UNSAFE_FILE");
    const buffer = Buffer.alloc(max + 1);
    let count = 0;
    while (count < buffer.length) {
      const n = readSync(fd, buffer, count, buffer.length - count, null);
      if (!n) break;
      count += n;
    }
    const bytes = buffer.subarray(0, count);
    const after = fstatSync(fd),
      current = lstatSync(path);
    if (
      bytes.length > max ||
      s.size !== after.size ||
      s.mtimeMs !== after.mtimeMs ||
      s.ino !== current.ino ||
      s.dev !== current.dev
    )
      throw new AgentError("SOURCE_CHANGED", 6);
    return bytes;
  } finally {
    closeSync(fd);
  }
}
export interface ProtectedSnapshot {
  bytes: Buffer;
  identity: string | null;
}
/** Keep identity as well as contents: an editor can replace a file with identical text. */
export function protectedSnapshot(
  path: string,
  max = 65_536,
): ProtectedSnapshot {
  const identity = () => {
    const s = lstatSync(path, { bigint: true });
    return [s.dev, s.ino, s.size, s.mtimeNs, s.ctimeNs].join(":");
  };
  try {
    const before = identity();
    const bytes = readProtected(path, max);
    if (identity() !== before) throw new AgentError("SOURCE_CHANGED", 6);
    return { bytes, identity: before };
  } catch (e) {
    if ((e as NodeJS.ErrnoException).code === "ENOENT")
      return { bytes: Buffer.alloc(0), identity: null };
    throw e;
  }
}
export function atomicProtected(
  path: string,
  value: string | Buffer,
  expected?: ProtectedSnapshot,
): void {
  protectedDirectory(dirname(path), true);
  try {
    readProtected(path, 1_048_576);
  } catch (e) {
    if ((e as NodeJS.ErrnoException).code !== "ENOENT") throw e;
  }
  const temp = join(dirname(path), `.pending-${randomUUID()}`);
  const fd = openSync(
    temp,
    constants.O_WRONLY |
      constants.O_CREAT |
      constants.O_EXCL |
      constants.O_NOFOLLOW,
    0o600,
  );
  try {
    writeFileSync(fd, value);
    fsyncSync(fd);
    closeSync(fd);
    // Compare after staging and fsync, immediately before replacing the pathname.
    // The caller's private lock cannot coordinate ordinary host/editor saves.
    if (expected) {
      const current = protectedSnapshot(path);
      if (
        current.identity !== expected.identity ||
        !current.bytes.equals(expected.bytes)
      )
        throw new AgentError("SOURCE_CHANGED", 6);
    }
    if (expected?.identity === null) {
      // An absent target can be installed without clobbering even a racing creator.
      try {
        linkSync(temp, path);
      } catch (e) {
        if ((e as NodeJS.ErrnoException).code === "EEXIST")
          throw new AgentError("SOURCE_CHANGED", 6);
        throw e;
      }
      unlinkSync(temp);
    } else {
      renameSync(temp, path);
    }
    const directory = openSync(dirname(path), constants.O_RDONLY);
    try {
      fsyncSync(directory);
    } finally {
      closeSync(directory);
    }
  } catch (e) {
    try {
      closeSync(fd);
    } catch {
      /* already closed */
    }
    try {
      unlinkSync(temp);
    } catch {
      /* absent */
    }
    throw e;
  }
}
