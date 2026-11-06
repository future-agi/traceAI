import { constants, promises as fs } from "node:fs";
import { dirname, relative, resolve, sep, join } from "node:path";
import { createHash } from "node:crypto";
import {
  safeParents,
  readJson,
  writeProtected,
  ensurePrivateDirectory,
} from "@traceai/agent-core/files";
import type { ResolvedConfig } from "@traceai/agent-core/config";
import { cleanText, toolFailed } from "./events";
export interface Message {
  at: number;
  turnId?: string;
  role: string;
  text?: string;
}
/** Bounded, incremental JSONL reader. Only the host-provided rollout below CODEX_HOME/sessions is eligible.
 * No transcript search, auth-cache reads, raw journal, or content persisted to disk by this package.
 */
export class RolloutReader {
  private offset = 0;
  private inode?: number;
  private tail: Buffer = Buffer.alloc(0);
  private turn?: string;
  private loaded = false;
  private safeCursor = false;
  private skippingLine = false;
  hasMore = false;
  targetSeen = false;
  private cursorPath(): string {
    return join(
      this.config.paths.stateDir,
      "codex-cursor-" +
        createHash("sha256")
          .update(
            JSON.stringify([
              this.config.generation,
              this.config.endpoint,
              this.config.projectName,
              this.session,
              this.path,
              this.targetTurn,
            ]),
          )
          .digest("hex")
          .slice(0, 24) +
        ".json",
    );
  }
  gap = false;
  readonly messages: Message[] = [];
  readonly toolResults = new Map<string, { failed: boolean }>();
  readonly completed = new Map<string, { at: number; output?: string }>();
  constructor(
    private path: string,
    private session: string,
    private config: ResolvedConfig,
    private targetTurn?: string,
  ) {}
  matches(path: string, session: string, turn: string): boolean {
    return (
      this.path === path && this.session === session && this.targetTurn === turn
    );
  }
  private get retaining(): boolean {
    return !this.targetTurn || this.turn === this.targetTurn;
  }
  async read(): Promise<void> {
    this.safeCursor = false;
    this.hasMore = false;
    const root = resolve(dirname(this.config.paths.user), "sessions");
    const rel = relative(root, this.path);
    if (
      rel.startsWith(".." + sep) ||
      rel === ".." ||
      !rel ||
      resolve(this.path) === root ||
      !this.path.endsWith(".jsonl")
    ) {
      this.gap = true;
      return;
    }
    let handle;
    try {
      await safeParents(dirname(this.path));
      handle = await fs.open(
        this.path,
        constants.O_RDONLY | constants.O_NOFOLLOW,
      );
      const st = await handle.stat();
      if (
        !st.isFile() ||
        st.nlink !== 1 ||
        (process.getuid && st.uid !== process.getuid())
      )
        throw new Error("UNSAFE_ROLLOUT");
      if (!this.loaded) {
        const header = Buffer.alloc(65536),
          { bytesRead: n } = await handle.read(header, 0, header.length, 0);
        const meta = JSON.parse(
          header.subarray(0, n).toString().split("\n")[0],
        );
        if (meta.type !== "session_meta" || meta.payload?.id !== this.session)
          throw new Error("ROLLOUT_SESSION_MISMATCH");
        const cursor = await readJson<{
          offset: number;
          inode: number;
          turn?: string;
          skippingLine?: boolean;
        }>(this.cursorPath());
        if (
          cursor &&
          Number.isSafeInteger(cursor.offset) &&
          cursor.offset >= 0
        ) {
          this.offset = cursor.offset;
          this.inode = cursor.inode;
          this.turn = cursor.turn;
          this.targetSeen ||=
            !!this.targetTurn && this.turn === this.targetTurn;
          this.skippingLine = cursor.skippingLine === true;
        }
        this.loaded = true;
      }
      if (
        this.inode !== undefined &&
        (st.ino !== this.inode || st.size < this.offset)
      ) {
        this.gap = true;
        return;
      }
      this.inode = st.ino;
      const bytes = Buffer.alloc(
        Math.min(st.size - this.offset, 4 * 1024 * 1024),
      );
      const { bytesRead } = await handle.read(
        bytes,
        0,
        bytes.length,
        this.offset,
      );
      this.offset += bytesRead;
      this.hasMore = this.offset < st.size;
      this.safeCursor = true;
      let combined = Buffer.concat([this.tail, bytes.subarray(0, bytesRead)]);
      if (this.skippingLine) {
        const end = combined.indexOf(10);
        if (end < 0) {
          this.tail = Buffer.alloc(0);
          return;
        }
        combined = combined.subarray(end + 1);
        this.skippingLine = false;
      }
      const boundary = combined.lastIndexOf(10);
      const lines = combined
        .subarray(0, boundary + 1)
        .toString("utf8")
        .split("\n");
      this.tail = combined.subarray(boundary + 1);
      for (const line of lines) {
        if (!line || line.length > 262144) {
          if (line && this.retaining) this.gap = true;
          continue;
        }
        try {
          const r = JSON.parse(line),
            p = r.payload;
          if (r.type === "session_meta" && p?.id !== this.session) {
            this.gap = true;
            this.messages.length = 0;
            this.safeCursor = false;
            return;
          }
          if (r.type === "turn_context") this.turn = p?.turn_id;
          if (r.type === "event_msg" && p?.type === "task_started")
            this.turn = p.turn_id;
          this.targetSeen ||=
            !!this.targetTurn &&
            (this.turn === this.targetTurn ||
              (r.type === "event_msg" &&
                p?.type === "task_complete" &&
                p.turn_id === this.targetTurn) ||
              p?.internal_chat_message_metadata_passthrough?.turn_id ===
                this.targetTurn);
          if (
            r.type === "event_msg" &&
            p?.type === "task_complete" &&
            typeof p.turn_id === "string" &&
            (!this.targetTurn || p.turn_id === this.targetTurn)
          )
            this.completed.set(p.turn_id, {
              at: Date.parse(r.timestamp),
              output: cleanText(p.last_agent_message, this.config, true),
            });
          if (this.completed.size > 512)
            this.completed.delete(this.completed.keys().next().value!);
          if (r.type !== "response_item") continue;
          const messageTurn =
            p?.internal_chat_message_metadata_passthrough?.turn_id ?? this.turn;
          // A per-turn cursor may scan old history, but unrelated messages and
          // tool results must neither consume this turn's budget nor mark gaps.
          if (this.targetTurn && messageTurn !== this.targetTurn) continue;
          if (
            p?.type === "function_call_output" &&
            typeof p.call_id === "string"
          ) {
            this.toolResults.set(p.call_id, {
              failed: toolFailed(
                typeof p.output === "string"
                  ? p.output
                  : JSON.stringify(p.output),
              ),
            });
            if (this.toolResults.size > 512)
              this.toolResults.delete(this.toolResults.keys().next().value!);
          }
          const call = p?.type === "function_call";
          if (
            !call &&
            (p?.type !== "message" || !["assistant", "user"].includes(p.role))
          )
            continue;
          const at = Date.parse(r.timestamp);
          if (!Number.isFinite(at)) continue;
          // Sanitize the original JSON arguments BEFORE serializing the wrapper.
          // Escaped quotes in a twice-serialized value bypass text-field redaction.
          let argumentsValue: unknown;
          if (call && typeof p.arguments === "string") {
            try {
              argumentsValue = JSON.parse(p.arguments);
            } catch {
              argumentsValue = "[UNPARSEABLE_ARGUMENTS]";
              this.gap = true;
            }
          } else if (call) argumentsValue = p.arguments;
          const text = call
            ? cleanText(
                { tool: p.name, arguments: argumentsValue },
                this.config,
                true,
              )
            : Array.isArray(p.content)
              ? p.content
                  .map((c: any) => (typeof c.text === "string" ? c.text : ""))
                  .join("\n")
              : undefined;
          const role = call ? "assistant" : p.role;
          this.messages.push({
            at,
            turnId: messageTurn,
            role,
            text: cleanText(text, this.config, role === "assistant"),
          });
          if (this.messages.length > 512) {
            this.messages.shift();
            this.gap = true;
          }
        } catch {
          if (this.retaining) this.gap = true;
        }
      }
      // Attribute a partial oversize row after processing preceding boundaries.
      if (this.tail.length > 262144) {
        this.tail = Buffer.alloc(0);
        this.skippingLine = true;
        if (this.retaining) this.gap = true;
      }
    } catch {
      this.gap = true;
    } finally {
      await handle?.close();
    }
  }
  async checkpoint(): Promise<void> {
    // Content loss is reported via gap, but must not permanently wedge progress.
    // Unsafe file/session identity is the only reason to reject a cursor.
    if (!this.loaded || !this.safeCursor) return;
    await ensurePrivateDirectory(this.config.paths.stateDir);
    await writeProtected(
      this.cursorPath(),
      JSON.stringify({
        offset: this.offset - this.tail.length,
        inode: this.inode,
        turn: this.turn,
        skippingLine: this.skippingLine,
      }),
    );
  }
}
