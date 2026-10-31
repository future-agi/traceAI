import {
  constants,
  closeSync,
  fstatSync,
  lstatSync,
  openSync,
  readSync,
  realpathSync,
  opendirSync,
} from "node:fs";
import { isAbsolute, join, relative, resolve, sep } from "node:path";
import { AgentError } from "../../security/files.js";
import { CodexCodec } from "./codec.js";

/** A user-selected immutable import source may use Codex's native 0644/0755
 * permissions. It must still be owner-controlled, regular and symlink-free. */
export function readImportSnapshot(path: string): Buffer {
  if (!isAbsolute(path) || resolve(path) !== path)
    throw new AgentError("UNSAFE_SOURCE_PATH", 3);
  const st = lstatSync(path);
  if (!st.isDirectory()) return readImportFile(path);
  // Only this explicitly selected directory, no recursive discovery. This lets
  // a bounded parent/child slice carry its own causal evidence during replay.
  if (
    st.isSymbolicLink() ||
    st.uid !== process.getuid?.() ||
    st.mode & 0o022 ||
    realpathSync(path) !== path
  )
    throw new AgentError("UNSAFE_SOURCE_OWNER", 3);
  const names: string[] = [],
    dir = opendirSync(path);
  try {
    let entries = 0;
    for (let entry = dir.readSync(); entry; entry = dir.readSync()) {
      if (++entries > 64) throw new AgentError("IMPORT_SLICE_REQUIRED", 4);
      if (/^rollout-[a-zA-Z0-9T_.:-]+\.jsonl$/.test(entry.name))
        names.push(entry.name);
    }
  } finally {
    dir.closeSync();
  }
  names.sort();
  if (!names.length || names.length > 16)
    throw new AgentError("IMPORT_SLICE_REQUIRED", 4);
  const parts: Buffer[] = [];
  let size = 0;
  for (const name of names) {
    const part = readImportFile(join(path, name));
    if (part[part.length - 1] !== 10)
      throw new AgentError("PARTIAL_SOURCE_TAIL", 4);
    size += part.length;
    if (size > 1_048_576) throw new AgentError("IMPORT_SLICE_REQUIRED", 4);
    parts.push(part);
  }
  const after = lstatSync(path);
  if (
    after.ino !== st.ino ||
    after.dev !== st.dev ||
    after.mtimeMs !== st.mtimeMs
  )
    throw new AgentError("SOURCE_CHANGED", 6);
  return Buffer.concat(parts);
}
function readImportFile(path: string): Buffer {
  if (!isAbsolute(path) || resolve(path) !== path)
    throw new AgentError("UNSAFE_SOURCE_PATH", 3);
  const chain = (): string => {
    let at = "/";
    const states: string[] = [];
    for (const part of path.split(sep).filter(Boolean).slice(0, -1)) {
      at = join(at, part);
      const s = lstatSync(at);
      if (!s.isDirectory() || s.isSymbolicLink())
        throw new AgentError("UNSAFE_SOURCE_PATH", 3);
      states.push(s.dev + ":" + s.ino);
    }
    return states.join("/");
  };
  const before = chain(),
    fd = openSync(path, constants.O_RDONLY | constants.O_NOFOLLOW);
  try {
    const s = fstatSync(fd);
    if (
      !s.isFile() ||
      s.nlink !== 1 ||
      s.uid !== process.getuid?.() ||
      s.mode & 0o022
    )
      throw new AgentError("UNSAFE_SOURCE_OWNER", 3);
    if (s.size > 1_048_576) throw new AgentError("IMPORT_SLICE_REQUIRED", 4);
    if (
      chain() !== before ||
      realpathSync(path) !== path ||
      lstatSync(path).ino !== s.ino
    )
      throw new AgentError("SOURCE_CHANGED", 6);
    const bytes = Buffer.alloc(s.size);
    let offset = 0;
    while (offset < bytes.length) {
      const n = readSync(fd, bytes, offset, bytes.length - offset, offset);
      if (!n) break;
      offset += n;
    }
    const after = fstatSync(fd);
    if (
      offset !== s.size ||
      after.size !== s.size ||
      after.mtimeMs !== s.mtimeMs ||
      chain() !== before ||
      lstatSync(path).ino !== s.ino
    )
      throw new AgentError("SOURCE_CHANGED", 6);
    return bytes;
  } finally {
    closeSync(fd);
  }
}

/** Opens only the explicitly announced active rollout. No directory scans, auth
 * discovery, transcript copies, decompression or fallback journals. */
export class RolloutReader {
  private offset = 0;
  private tail = Buffer.alloc(0);
  private inode?: string;
  private size = 0;
  constructor(
    readonly root: string,
    readonly path: string,
    readonly codec: CodexCodec,
  ) {
    if (
      !isAbsolute(root) ||
      root !== resolve(root) ||
      !isAbsolute(path) ||
      path !== resolve(path)
    )
      throw new AgentError("UNSAFE_SOURCE_PATH", 3);
    const rel = relative(root, path);
    if (
      !rel ||
      rel.startsWith("..") ||
      isAbsolute(rel) ||
      !/^\d{4}\/\d{2}\/\d{2}\/rollout-[a-zA-Z0-9T_.:-]+\.jsonl$/.test(rel)
    )
      throw new AgentError("UNSAFE_SOURCE_PATH", 3);
  }
  private parents(): string {
    let at = "/";
    const states: string[] = [];
    for (const part of this.path.split(sep).filter(Boolean).slice(0, -1)) {
      at = join(at, part);
      const s = lstatSync(at);
      if (s.isSymbolicLink() || !s.isDirectory())
        throw new AgentError("UNSAFE_SOURCE_PATH", 3);
      if (
        (at === this.root || at.startsWith(this.root + sep)) &&
        (s.uid !== process.getuid?.() || s.mode & 0o022)
      )
        throw new AgentError("UNSAFE_SOURCE_OWNER", 3);
      states.push(s.dev + ":" + s.ino);
    }
    return states.join("/");
  }
  /** One <=1MiB slice. Complete records are minimized by the codec immediately;
   * the incomplete tail stays bounded in memory, never in runtime storage. */
  read(): { changed: boolean; caughtUp: boolean; coordinate: string } {
    const parents = this.parents();
    const fd = openSync(this.path, constants.O_RDONLY | constants.O_NOFOLLOW);
    try {
      const s = fstatSync(fd),
        identity = s.dev + ":" + s.ino;
      if (
        !s.isFile() ||
        s.nlink !== 1 ||
        s.uid !== process.getuid?.() ||
        s.mode & 0o022
      )
        throw new AgentError("UNSAFE_SOURCE_OWNER", 3);
      if (this.inode && (this.inode !== identity || s.size < this.size))
        throw new AgentError("HELD_ROTATED_SOURCE", 3);
      if (
        parents !== this.parents() ||
        realpathSync(this.path) !== this.path ||
        lstatSync(this.path).ino !== s.ino
      )
        throw new AgentError("SOURCE_CHANGED", 6);
      const buffer = Buffer.alloc(Math.min(1_048_576, s.size - this.offset));
      const count = buffer.length
        ? readSync(fd, buffer, 0, buffer.length, this.offset)
        : 0;
      const after = fstatSync(fd);
      if (
        after.size < s.size ||
        parents !== this.parents() ||
        lstatSync(this.path).ino !== s.ino
      )
        throw new AgentError("SOURCE_CHANGED", 6);
      this.inode = identity;
      this.size = after.size;
      this.offset += count;
      const pending = Buffer.concat([this.tail, buffer.subarray(0, count)]);
      let start = 0;
      for (;;) {
        const end = pending.indexOf(10, start);
        if (end < 0) break;
        if (end - start > 2_097_152)
          throw new AgentError("OVERSIZE_SOURCE_RECORD", 4);
        const line = pending.subarray(start, end);
        if (line.length) {
          try {
            this.codec.push(JSON.parse(line.toString("utf8")));
          } catch (e) {
            if (e instanceof AgentError) throw e;
            throw new AgentError("UNKNOWN_SOURCE_SCHEMA", 5);
          }
        }
        start = end + 1;
      }
      this.tail = Buffer.from(pending.subarray(start));
      if (this.tail.length > 2_097_152)
        throw new AgentError("OVERSIZE_SOURCE_RECORD", 4);
      return {
        changed: count > 0,
        caughtUp: this.offset >= after.size && this.tail.length === 0,
        coordinate: String(this.offset - this.tail.length),
      };
    } finally {
      closeSync(fd);
    }
  }
}
