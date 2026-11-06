import { constants, promises as fs } from "node:fs";
import { dirname, resolve, parse } from "node:path";
import { createHash, randomBytes } from "node:crypto";

const MAX_FILE = 64 * 1024;
export const contentHash = (text: string): string =>
  createHash("sha256").update(text).digest("hex");
const uid = (): number | undefined => process.getuid?.();
function owned(stat: { uid: number; mode: number }): void {
  if (uid() !== undefined && stat.uid !== uid())
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
    // Root-owned system ancestors are allowed; writable shared ancestors must be sticky (e.g. /tmp).
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
): Promise<string | undefined> {
  try {
    await safeParents(dirname(path));
    const handle = await fs.open(
      path,
      constants.O_RDONLY | constants.O_NOFOLLOW,
    );
    try {
      const st = await handle.stat();
      owned(st);
      if (
        !st.isFile() ||
        st.nlink !== 1 ||
        st.size > MAX_FILE ||
        (privateFile && st.mode & 0o077)
      )
        throw new Error("UNSAFE_FILE");
      const bytes = Buffer.alloc(MAX_FILE + 1);
      const { bytesRead } = await handle.read(bytes, 0, bytes.length, 0);
      if (bytesRead > MAX_FILE) throw new Error("OVERSIZE_FILE");
      return bytes.subarray(0, bytesRead).toString("utf8");
    } finally {
      await handle.close();
    }
  } catch (e) {
    if ((e as NodeJS.ErrnoException).code === "ENOENT") return undefined;
    throw e;
  }
}
/** Optimistic owned-file reconciliation. Callers retry by re-reading, never replay a stale merge. */
export async function writeProtected(
  path: string,
  text: string,
  expected?: string | null,
): Promise<void> {
  if (Buffer.byteLength(text) > MAX_FILE) throw new Error("OVERSIZE_FILE");
  await safeParents(dirname(path), true);
  owned(await fs.lstat(dirname(path)));
  const lock = await fs
    .open(
      `${path}.lock`,
      constants.O_CREAT |
        constants.O_EXCL |
        constants.O_WRONLY |
        constants.O_NOFOLLOW,
      0o600,
    )
    .catch(() => {
      throw new Error("CONCURRENT_SAVE");
    });
  let temporary: string | undefined;
  try {
    const old = await readProtected(path);
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
    // Check again immediately before atomic replacement. Uncooperative host editors cannot be locked.
    if ((await readProtected(path)) !== old) throw new Error("CONCURRENT_SAVE");
    await fs.rename(temporary, path);
    temporary = undefined;
  } finally {
    if (temporary) await fs.unlink(temporary).catch(() => {});
    await lock.close();
    await fs.unlink(`${path}.lock`).catch(() => {});
  }
}
export async function readJson<T>(
  path: string,
  privateFile = true,
): Promise<T | undefined> {
  const text = await readProtected(path, privateFile);
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
): Promise<void> {
  for (let attempt = 0; attempt < 3; attempt++) {
    const text = await readProtected(path);
    let old: T | undefined;
    try {
      old = text === undefined ? undefined : (JSON.parse(text) as T);
    } catch {
      throw new Error("INVALID_CONFIG_JSON");
    }
    try {
      await writeProtected(
        path,
        JSON.stringify(update(old), null, 2) + "\n",
        text === undefined ? null : contentHash(text),
      );
      return;
    } catch (e) {
      if ((e as Error).message !== "CONCURRENT_SAVE" || attempt === 2) throw e;
    }
  }
}
