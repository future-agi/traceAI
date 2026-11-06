import * as nodeFs from "node:fs";
import { lock } from "proper-lockfile";

/** All protected writers share this versioned lease; legacy O_EXCL files stay inert. */
export const writeLockPath = (path: string): string => `${path}.write-lock`;
function checked(stat: nodeFs.Stats): nodeFs.Stats {
  if (
    !stat.isDirectory() ||
    stat.isSymbolicLink() ||
    stat.mode & 0o077 ||
    (process.getuid && stat.uid !== process.getuid())
  )
    throw new Error("UNSAFE_LOCK");
  return stat;
}
const directory = async (path: string) =>
  checked(await nodeFs.promises.lstat(path));
type Callback = (error: NodeJS.ErrnoException | null) => void;
// Use no-follow descriptors for heartbeats too, not just the initial lstat.
const lockFs = {
  ...nodeFs,
  mkdir(path: string, callback: Callback) {
    nodeFs.mkdir(path, { mode: 0o700 }, callback);
  },
  stat(
    path: string,
    callback: (
      error: NodeJS.ErrnoException | null,
      stat?: nodeFs.Stats,
    ) => void,
  ) {
    directory(path).then((stat) => callback(null, stat), callback);
  },
  utimes(path: string, atime: Date, mtime: Date, callback: Callback) {
    (async () => {
      const handle = await nodeFs.promises.open(
        path,
        nodeFs.constants.O_RDONLY |
          nodeFs.constants.O_DIRECTORY |
          nodeFs.constants.O_NOFOLLOW,
      );
      try {
        checked(await handle.stat());
        await handle.utimes(atime, mtime);
      } finally {
        await handle.close();
      }
    })().then(() => callback(null), callback);
  },
  rmdir(path: string, callback: Callback) {
    directory(path)
      .then(() => nodeFs.promises.rmdir(path))
      .then(() => callback(null), callback);
  },
  rmdirSync(path: string) {
    checked(nodeFs.lstatSync(path));
    nodeFs.rmdirSync(path);
  },
};
export async function acquireWriteLock(
  path: string,
  options: { lockWaitMs?: number } = {},
): Promise<{
  assertHeld(): Promise<void>;
  release(): Promise<void>;
}> {
  const lockPath = writeLockPath(path);
  await directory(lockPath).catch((error: NodeJS.ErrnoException) => {
    if (error.code !== "ENOENT") throw error;
  });
  let compromised = false;
  const wait =
    options.lockWaitMs === undefined
      ? 12000
      : Math.max(0, Math.min(12000, options.lockWaitMs));
  if (!Number.isFinite(wait)) throw new Error("INVALID_LOCK_WAIT");
  // The target as well as lockfilePath must be distinct from an adapter's outer
  // event lease: proper-lockfile indexes its in-process owners by target.
  const release = await lock(lockPath, {
    realpath: false,
    lockfilePath: lockPath,
    stale: 5000,
    update: 1000,
    retries: {
      retries: Math.ceil(wait / 50),
      factor: 1,
      minTimeout: 50,
      maxTimeout: 50,
    },
    fs: lockFs as typeof nodeFs,
    onCompromised: () => {
      compromised = true;
    },
  }).catch((error: NodeJS.ErrnoException) => {
    if (error.code === "ELOCKED") throw new Error("CONCURRENT_SAVE");
    throw error;
  });
  const original = await directory(lockPath);
  const assertHeld = async () => {
    const current = await directory(lockPath);
    if (
      compromised ||
      current.dev !== original.dev ||
      current.ino !== original.ino
    )
      throw new Error("CONCURRENT_SAVE");
  };
  return {
    assertHeld,
    async release() {
      try {
        await assertHeld();
      } catch {
        return;
      }
      await release();
    },
  };
}
