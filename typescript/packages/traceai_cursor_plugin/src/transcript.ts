import { constants, promises as fs } from "node:fs";
import { dirname, isAbsolute, join, relative, resolve } from "node:path";
import {
  contentHash,
  redact,
  safeParents,
  type ResolvedConfig,
} from "@traceai/agent-core";
import { object, filtered, toolResultFailed } from "./events.js";

export interface Message {
  id: string;
  input?: string;
  output?: string;
  /** Only a text-only final response can prove completion without turn_ended. */
  finalTextHash?: string;
}
export interface TranscriptTurn {
  promptHash: string;
  messages: Message[];
  ended: boolean;
  prompt?: string;
  context?: string;
}
export interface TranscriptResult {
  turns: TranscriptTurn[];
  gap?: string;
  evictedPrompts?: readonly string[];
}
/** No home scans. Only host-supplied JSONL paths inside Cursor's transcript tree are eligible.
 * Cursors and partial bytes live in bounded memory; no raw-content recovery journal exists.
 */
export class TranscriptReader {
  private identity?: string;
  private offset = 0;
  private prefix?: string;
  private pending = Buffer.alloc(0);
  private turns: TranscriptTurn[] = [];
  private gap?: string;
  private contentBytes = 0;
  private evictedPrompts = new Set<string>();
  private chain = Promise.resolve();
  constructor(private readonly config: ResolvedConfig) {}
  read(file: string): Promise<TranscriptResult> {
    let result: TranscriptResult = { turns: [], gap: "TRANSCRIPT_UNAVAILABLE" };
    const next = this.chain.then(async () => {
      result = await this.consume(file);
    });
    this.chain = next.catch(() => {});
    return next.then(() => result);
  }
  /** stop can precede Cursor's final disk write. Poll only this supplied path, never scan home. */
  async readSettled(
    file: string,
    prompt: string | undefined,
    finalOutput?: string,
    options: {
      budgetMs?: number;
      intervalMs?: number;
      enabled?: () => boolean;
    } = {},
  ): Promise<TranscriptResult> {
    // Brief foreground settle for IDE writes. CLI writes can arrive much later;
    // the hook runtime hands those snapshots to a separate bounded finisher.
    const deadline =
      Date.now() + Math.min(5000, Math.max(0, options.budgetMs ?? 1500));
    let result = await this.read(file);
    while (!correlate(result, prompt, finalOutput) && Date.now() < deadline) {
      if (options.enabled && !options.enabled()) break;
      if (
        result.gap &&
        ![
          "TRANSCRIPT_UNAVAILABLE",
          "TRANSCRIPT_PARTIAL_TAIL",
          "TRANSCRIPT_READ_BUDGET",
        ].includes(result.gap)
      )
        break;
      await new Promise((r) =>
        setTimeout(
          r,
          Math.min(
            Math.max(1, options.intervalMs ?? 100),
            deadline - Date.now(),
          ),
        ),
      );
      if (options.enabled && !options.enabled()) break;
      result = await this.read(file);
    }
    return result;
  }
  private async consume(file: string): Promise<TranscriptResult> {
    if (!this.config.captureContent)
      return { turns: [], gap: "CONTENT_DISABLED" };
    const base = join(dirname(this.config.paths.user), "projects");
    const rel = relative(base, resolve(file));
    if (
      !isAbsolute(file) ||
      rel.startsWith("..") ||
      isAbsolute(rel) ||
      !rel.split("/").includes("agent-transcripts") ||
      !file.endsWith(".jsonl")
    )
      return { turns: [], gap: "TRANSCRIPT_PATH_REJECTED" };
    let handle: fs.FileHandle | undefined;
    try {
      await safeParents(dirname(file));
      handle = await fs.open(file, constants.O_RDONLY | constants.O_NOFOLLOW);
      const stat = await handle.stat();
      if (
        !stat.isFile() ||
        stat.nlink !== 1 ||
        (process.getuid && stat.uid !== process.getuid()) ||
        stat.mode & 0o022
      )
        return { turns: [], gap: "TRANSCRIPT_PATH_REJECTED" };
      const identity = `${stat.dev}:${stat.ino}:${file}`;
      const prefixBytes = Buffer.alloc(Math.min(128, this.offset || stat.size));
      await handle.read(prefixBytes, 0, prefixBytes.length, 0);
      const prefix = contentHash(prefixBytes.toString("hex"));
      if (
        this.identity &&
        (this.identity !== identity ||
          stat.size < this.offset ||
          (this.prefix && this.offset >= 128 && prefix !== this.prefix))
      ) {
        // Never re-emit earlier messages under new byte identities after truncation/replacement.
        this.gap = "TRANSCRIPT_TRUNCATED";
        this.turns = [];
        this.contentBytes = 0;
        this.evictedPrompts.clear();
        this.pending = Buffer.alloc(0);
        return { turns: [], gap: this.gap };
      }
      if (this.gap === "TRANSCRIPT_TRUNCATED")
        return { turns: [], gap: this.gap };
      this.identity = identity;
      const amount = Math.min(512 * 1024, Math.max(0, stat.size - this.offset));
      const buffer = Buffer.alloc(amount),
        before = this.offset;
      const { bytesRead } = await handle.read(buffer, 0, amount, this.offset);
      this.offset += bytesRead;
      const bytes = Buffer.concat([
        this.pending,
        buffer.subarray(0, bytesRead),
      ]);
      let start = 0,
        lines = 0;
      const baseOffset = before - this.pending.length;
      for (
        let end = bytes.indexOf(10);
        end !== -1;
        end = bytes.indexOf(10, start)
      ) {
        if (++lines > 4096) {
          this.gap = "TRANSCRIPT_LIMIT";
          break;
        }
        if (end - start > 65536) this.gap = "TRANSCRIPT_LINE_LIMIT";
        else
          try {
            const row = object(
              JSON.parse(bytes.subarray(start, end).toString("utf8")),
            );
            if (row) {
              const msg = object(row.message),
                blocks = Array.isArray(msg?.content) ? msg.content : [];
              // Preserve each original JSON boundary for structural redaction.
              // Joining blocks (or tool calls) first turns valid JSON into opaque text.
              const value = (
                typeof msg?.content === "string"
                  ? [msg.content]
                  : blocks
                      .map((b) =>
                        object(b)?.type === "text" ? object(b)?.text : "",
                      )
                      .filter((t): t is string => typeof t === "string")
              )
                .map((text) => redact(text))
                .join("\n");
              const toolResults = blocks.filter(
                (b) => object(b)?.type === "tool_result",
              );
              if (
                (row.role === "tool" ||
                  (row.role === "user" && toolResults.length && !value)) &&
                this.turns.length
              ) {
                const turn = this.turns[this.turns.length - 1];
                const failed =
                  toolResultFailed(row) || toolResultFailed(msg?.content);
                const result = filtered(
                  toolResults.length ? toolResults : msg?.content,
                  this.config,
                  true,
                  failed,
                );
                if (result)
                  turn.context = (
                    (turn.context ?? "") +
                    "\nTool: " +
                    result
                  ).slice(-10000);
              } else if (row.role === "user") {
                const clean = value.replace(
                  /^\s*<timestamp>[\s\S]*?<\/timestamp>\s*/,
                  "",
                );
                const prompt = (
                  /<user_query>\s*([\s\S]*?)\s*<\/user_query>/.exec(
                    clean,
                  )?.[1] ?? clean
                ).trim();
                this.turns.push({
                  promptHash: contentHash(filtered(prompt, this.config) ?? ""),
                  prompt: filtered(prompt, this.config),
                  messages: [],
                  ended: false,
                });
                if (this.turns.length > 64) {
                  const removed = this.turns.shift()!;
                  this.contentBytes -= removed.messages.reduce((n, m) => n +
                    Buffer.byteLength(m.input ?? "") + Buffer.byteLength(m.output ?? ""), 0);
                  // Preserve ambiguity evidence without retaining historical content.
                  if (this.evictedPrompts.size < 4096) this.evictedPrompts.add(removed.promptHash);
                  else this.gap = "TRANSCRIPT_HISTORY_IDENTITY_LIMIT";
                }
              } else if (row.role === "assistant" && this.turns.length) {
                const turn = this.turns[this.turns.length - 1];
                const calls = blocks
                  .filter((b) => object(b)?.type === "tool_use")
                  .map((b) => {
                    const call = object(b)!;
                    return {
                      name: typeof call.name === "string" ? call.name : "tool",
                      arguments: call.input,
                    };
                  });
                const output = filtered(
                  [
                    value,
                    ...(calls.length
                      ? [filtered({ tool_calls: calls }, this.config, true)]
                      : []),
                  ]
                    .filter(Boolean)
                    .join("\n"),
                  this.config,
                  true,
                );
                // Never re-expose hidden outputs through the next generation's input.
                const input = filtered(
                  turn.context
                    ? `${turn.prompt?.slice(0, 9000) ?? ""}\n${turn.context}`
                    : turn.prompt,
                  this.config,
                );
                const size =
                  Buffer.byteLength(output ?? "") +
                  Buffer.byteLength(input ?? "");
                if (
                  turn.messages.length < 128 &&
                  this.contentBytes + size <= 2 * 1024 * 1024
                ) {
                  this.contentBytes += size;
                  turn.messages.push({
                    id: contentHash(`${identity}:${baseOffset + start}`),
                    input,
                    output,
                    finalTextHash:
                      !calls.length && output
                        ? contentHash(output.trim())
                        : undefined,
                  });
                  if (output)
                    turn.context = (
                      (turn.context ?? "") +
                      "\nAssistant: " +
                      output
                    ).slice(-10000);
                } else this.gap = "TRANSCRIPT_MESSAGE_LIMIT";
              } else if (row.type === "turn_ended" && this.turns.length)
                this.turns[this.turns.length - 1].ended = true;
            }
          } catch {
            this.gap = "TRANSCRIPT_MALFORMED_LINE";
          }
        start = end + 1;
      }
      this.pending = bytes.subarray(start);
      if (this.pending.length > 65536) {
        this.pending = Buffer.alloc(0);
        this.gap = "TRANSCRIPT_LINE_LIMIT";
      }
      if (stat.size > this.offset) this.gap = "TRANSCRIPT_READ_BUDGET";
      else if (this.gap === "TRANSCRIPT_READ_BUDGET") this.gap = undefined;
      if (this.offset >= 128) {
        const fixed = Buffer.alloc(128);
        await handle.read(fixed, 0, 128, 0);
        this.prefix = contentHash(fixed.toString("hex"));
      }
      return {
        turns: this.turns.map((t) => ({ ...t, messages: [...t.messages] })),
        evictedPrompts: [...this.evictedPrompts],
        gap:
          this.gap ??
          (this.pending.length ? "TRANSCRIPT_PARTIAL_TAIL" : undefined),
      };
    } catch {
      return { turns: [], gap: "TRANSCRIPT_UNAVAILABLE" };
    } finally {
      await handle?.close();
    }
  }
}
/** No position/last-turn fallback. Repeated matching prompts are ambiguous without source ids. */
export function correlate(
  result: TranscriptResult,
  prompt: string | undefined,
  finalOutput?: string,
): TranscriptTurn | undefined {
  if (prompt === undefined || result.gap) return undefined;
  const hash = contentHash(prompt.trim());
  if (result.evictedPrompts?.includes(hash)) return undefined;
  const matching = result.turns.filter((t) => t.promptHash === hash);
  if (matching.length !== 1) return undefined;
  const turn = matching[0];
  const finalMatches =
    finalOutput?.trim() &&
    turn.messages.at(-1)?.finalTextHash === contentHash(finalOutput.trim());
  return turn.ended || finalMatches ? turn : undefined;
}
