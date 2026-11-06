import { constants, promises as fs } from "node:fs";
import { dirname, resolve, parse } from "node:path";
import { createHash, randomBytes } from "node:crypto";
import { acquireWriteLock } from "./file-lock.js";

const MAX_FILE = 64 * 1024;
export interface FileOptions {
  privateFile?: boolean;
  maxBytes?: number;
  lockWaitMs?: number;
}
/** Boolean and byte-limit arguments retain the host-settings and Goose APIs. */
type FilePolicy = boolean | number | FileOptions;
function policy(
  value: FilePolicy = true,
): Required<Pick<FileOptions, "privateFile" | "maxBytes">> & FileOptions {
  const options =
    typeof value === "boolean"
      ? { privateFile: value }
      : typeof value === "number"
        ? { maxBytes: value }
        : value;
  const result = { privateFile: true, maxBytes: MAX_FILE, ...options };
  if (
    !Number.isSafeInteger(result.maxBytes) ||
    result.maxBytes < 1 ||
    result.maxBytes > 1024 * 1024
  )
    throw new Error("INVALID_FILE_LIMIT");
  return result;
}
export const contentHash = (text: string): string =>
  createHash("sha256").update(text).digest("hex");
function owned(stat: { uid: number; mode: number }): void {
  if (process.getuid && stat.uid !== process.getuid())
    throw new Error("UNSAFE_FILE_OWNER");
}
/** No following symlinks, including ancestor directories. Windows ACL support is not claimed. */
export async function safeParents(path: string, create = false): Promise<void> {
  if (process.platform === "win32") throw new Error("WINDOWS_ACL_UNSUPPORTED");
  const absolute = resolve(path);
  let current = parse(absolute).root;
  for (const part of absolute
    .slice(current.length)
    .split("/")
    .filter(Boolean)) {
    current = resolve(current, part);
    if (create)
      await fs
        .mkdir(current, { mode: 0o700 })
        .catch((e: NodeJS.ErrnoException) => {
          if (e.code !== "EEXIST") throw e;
        });
    const st = await fs.lstat(current);
    if (!st.isDirectory() || st.isSymbolicLink())
      throw new Error("UNSAFE_DIRECTORY");
    if (st.mode & 0o022 && !(st.mode & 0o1000))
      throw new Error("UNSAFE_DIRECTORY_MODE");
  }
}
export async function ensurePrivateDirectory(path: string): Promise<void> {
  await safeParents(path, true);
  const st = await fs.lstat(path);
  owned(st);
  if (st.mode & 0o077) throw new Error("UNSAFE_PRIVATE_DIRECTORY_MODE");
}
export async function readProtected(
  path: string,
  privateFile = true,
  maxBytes = MAX_FILE,
): Promise<string | undefined> {
  policy({ privateFile, maxBytes });
  try {
    await safeParents(dirname(path));
    // O_NONBLOCK prevents a FIFO/device at an untrusted project path from wedging hooks.
    const handle = await fs.open(
      path,
      constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK,
    );
    try {
      const st = await handle.stat();
      owned(st);
      if (
        !st.isFile() ||
        st.nlink !== 1 ||
        st.size > maxBytes ||
        st.mode & (privateFile ? 0o077 : 0o022)
      )
        throw new Error("UNSAFE_FILE");
      const bytes = Buffer.alloc(maxBytes + 1);
      const { bytesRead } = await handle.read(bytes, 0, bytes.length, 0);
      if (bytesRead > maxBytes) throw new Error("OVERSIZE_FILE");
      return bytes.subarray(0, bytesRead).toString("utf8");
    } finally {
      await handle.close();
    }
  } catch (e) {
    if ((e as NodeJS.ErrnoException).code === "ENOENT") return undefined;
    throw e;
  }
}
export async function withFileLock<T>(
  path: string,
  action: (assertHeld: () => Promise<void>) => Promise<T>,
  options: FileOptions = {},
): Promise<T> {
  await safeParents(dirname(path), true);
  owned(await fs.lstat(dirname(path)));
  const lease = await acquireWriteLock(path, options);
  try {
    return await action(lease.assertHeld);
  } finally {
    await lease.release();
  }
}
async function replaceProtected(
  path: string,
  text: string,
  expected: string | null | undefined,
  options: FilePolicy,
  assertHeld: () => Promise<void>,
): Promise<void> {
  const { privateFile, maxBytes } = policy(options);
  if (Buffer.byteLength(text) > maxBytes) throw new Error("OVERSIZE_FILE");
  let temporary: string | undefined;
  try {
    const old = await readProtected(path, privateFile, maxBytes);
    if (
      expected !== undefined &&
      (old === undefined ? null : contentHash(old)) !== expected
    )
      throw new Error("CONCURRENT_SAVE");
    temporary = `${path}.${randomBytes(12).toString("hex")}.tmp`;
    const handle = await fs.open(
      temporary,
      constants.O_CREAT |
        constants.O_EXCL |
        constants.O_WRONLY |
        constants.O_NOFOLLOW,
      0o600,
    );
    try {
      await handle.writeFile(text);
      await handle.sync();
    } finally {
      await handle.close();
    }
    if ((await readProtected(path, privateFile, maxBytes)) !== old)
      throw new Error("CONCURRENT_SAVE");
    await assertHeld();
    await fs.rename(temporary, path);
    temporary = undefined;
  } finally {
    if (temporary) await fs.unlink(temporary).catch(() => {});
  }
}
/** Optimistic reconciliation detects host editors; the renewable lease serializes cooperating writers. */
export async function writeProtected(
  path: string,
  text: string,
  expected?: string | null,
  options: FilePolicy = true,
): Promise<void> {
  const parsed = policy(options);
  if (Buffer.byteLength(text) > parsed.maxBytes)
    throw new Error("OVERSIZE_FILE");
  await withFileLock(
    path,
    (check) => replaceProtected(path, text, expected, parsed, check),
    parsed,
  );
}
/** Caller MUST own the same external lease for every reader/writer of this path.
 * No nested lease is acquired, so a killed hook cannot strand a second claim. */
export async function writeProtectedUnderLock(
  path: string,
  text: string,
  expected?: string | null,
  options: FilePolicy = true,
): Promise<void> {
  await safeParents(dirname(path), true);
  owned(await fs.lstat(dirname(path)));
  await replaceProtected(path, text, expected, options, async () => {});
}
export async function readJson<T>(
  path: string,
  privateFile = true,
  maxBytes = MAX_FILE,
): Promise<T | undefined> {
  const text = await readProtected(path, privateFile, maxBytes);
  if (text === undefined) return undefined;
  try {
    return JSON.parse(text) as T;
  } catch {
    throw new Error("INVALID_CONFIG_JSON");
  }
}
export async function updateJson<T>(
  path: string,
  update: (old: T | undefined) => T,
  options: FilePolicy = true,
): Promise<void> {
  const parsed = policy(options);
  await withFileLock(
    path,
    async (check) => {
      for (let attempt = 0; attempt < 3; attempt++) {
        const text = await readProtected(
          path,
          parsed.privateFile,
          parsed.maxBytes,
        );
        let old: T | undefined;
        try {
          old = text === undefined ? undefined : (JSON.parse(text) as T);
        } catch {
          throw new Error("INVALID_CONFIG_JSON");
        }
        try {
          await replaceProtected(
            path,
            JSON.stringify(update(old), null, 2) + "\n",
            text === undefined ? null : contentHash(text),
            parsed,
            check,
          );
          return;
        } catch (e) {
          if ((e as Error).message !== "CONCURRENT_SAVE" || attempt === 2)
            throw e;
        }
      }
    },
    parsed,
  );
}
/** Remove only an unchanged owned file; never unlink a symlink or clobber host edits. */
export async function removeProtected(
  path: string,
  expected: string,
  privateFile = true,
): Promise<void> {
  if ((await readProtected(path, privateFile)) === undefined) return;
  await withFileLock(path, async (check) => {
    const old = await readProtected(path, privateFile);
    if (old === undefined) return;
    if (contentHash(old) !== expected) throw new Error("CONCURRENT_SAVE");
    await check();
    await fs.unlink(path);
  });
}
