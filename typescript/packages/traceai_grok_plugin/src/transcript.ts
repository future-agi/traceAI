import { constants, promises as fs } from "node:fs";
import { basename, dirname, join, relative, resolve } from "node:path";
import { homedir } from "node:os";
import { safeParents } from "@traceai/agent-core/files";

/** Only the host-supplied updates.jsonl inside this user's Grok session directory. */
export class TranscriptCursor {
  private offset = 0;
  private inode?: number;
  private path?: string;
  gap = false;
  lastReadBytes = 0;
  constructor(
    private readonly sessionId: string,
    private readonly home = homedir(),
  ) {}
  async read(path: string, emit: (row: unknown) => void): Promise<void> {
    const root = join(this.home, ".grok", "sessions"),
      full = resolve(path),
      rel = relative(root, full);
    if (
      rel.startsWith("..") ||
      rel.startsWith("/") ||
      basename(full) !== "updates.jsonl" ||
      basename(dirname(full)) !== this.sessionId ||
      (this.path && this.path !== full)
    ) {
      this.gap = true;
      return;
    }
    await safeParents(dirname(full));
    const file = await fs.open(full, constants.O_RDONLY | constants.O_NOFOLLOW);
    try {
      const st = await file.stat();
      if (
        !st.isFile() ||
        st.nlink !== 1 ||
        (process.getuid && st.uid !== process.getuid())
      ) {
        this.gap = true;
        return;
      }
      if (
        (this.inode !== undefined && st.ino !== this.inode) ||
        st.size < this.offset
      ) {
        // Don't restart from zero and re-export a rewritten transcript.
        this.gap = true;
        return;
      }
      this.path = full;
      this.inode = st.ino;
      const bytes = Buffer.alloc(Math.min(st.size - this.offset, 1024 * 1024));
      const { bytesRead } = await file.read(
        bytes,
        0,
        bytes.length,
        this.offset,
      );
      this.lastReadBytes = bytesRead;
      const end = bytes.subarray(0, bytesRead).lastIndexOf(10);
      if (end < 0) {
        if (bytesRead >= 1024 * 1024) this.gap = true;
        return;
      }
      for (const line of bytes.subarray(0, end).toString("utf8").split("\n")) {
        if (Buffer.byteLength(line) > 256 * 1024) {
          this.gap = true;
          continue;
        }
        try {
          emit(JSON.parse(line));
        } catch {
          this.gap = true;
        }
      }
      this.offset += end + 1;
    } finally {
      await file.close();
    }
  }
}
