import { constants, promises as fs } from "node:fs";
import { dirname, resolve, sep, basename } from "node:path";
import { safeParents, contentHash } from "@traceai/agent-core/files";
import { redact, redactValue } from "@traceai/agent-core/privacy";
import type { ResolvedConfig } from "@traceai/agent-core/config";
import type { UsageInput } from "@traceai/agent-core/tracer";
export const VERSION = "0.1.0"; // x-release-please-version
export const TESTED_HOST = "2.1.318";
// Updates within the qualified major line retain the hook/transcript contract.
export const supportedHost = (version: unknown): version is string =>
  typeof version === "string" && /^2\.\d+\.\d+$/.test(version);
export interface ChildCursor {
  path?: string;
  offset: number;
  contentFloor: number;
}

export const EVENTS = [
  "SessionStart",
  "UserPromptSubmit",
  "PreToolUse",
  "PostToolUse",
  "PostToolUseFailure",
  "SubagentStart",
  "SubagentStop",
  "PreCompact",
  "PostCompact",
  "Stop",
  "StopFailure",
  "SessionEnd",
] as const;
export interface ToolTiming {
  start: number;
  end?: number;
  failed?: boolean;
}
export interface SessionState {
  schemaVersion: 1;
  offset: number;
  captureBinding?: string;
  promptId?: string;
  start?: number;
  ended?: boolean;
  tools: Record<string, ToolTiming>;
  compactions: number[];
  subagents: Array<{
    id: string;
    path: string;
    offset?: number;
    contentFloor?: number;
  }>;
  childCursors?: Record<string, ChildCursor>;
  agentParents?: Record<string, { toolId: string; parentAgentId?: string }>;
  completed: string[];
  pendingPrompts?: string[];
  gap?: string;
  contentFloor?: number;
  privacyPending?: boolean;
  privateTurn?: boolean;
  stopped?: boolean;
  stopTime?: number;
  checkpoint?: string[];
  terminal?: "success" | "error" | "interrupted";
  terminals?: Record<
    string,
    {
      outcome: "success" | "error";
      textHash?: string;
      time: number;
    }
  >;
  // Hash only: terminal content must never enter persistent session metadata.
  terminalTextHash?: string;
  terminalPromptId?: string;
}
interface Block {
  type?: string;
  text?: string;
  id?: string;
  name?: string;
  input?: unknown;
  content?: unknown;
  tool_use_id?: string;
  is_error?: boolean;
}
interface Row {
  byteOffset?: number;
  type?: string;
  subtype?: string;
  isMeta?: boolean;
  promptSource?: string;
  turnOrigin?: string;
  promptId?: string;
  uuid?: string;
  timestamp?: string;
  sessionId?: string;
  version?: string;
  requestRef?: string;
  parentUuid?: string;
  sourceToolAssistantUUID?: string;
  parentToolUseID?: string;
  agentId?: string;
  toolUseResult?: { agentId?: string };
  isApiErrorMessage?: boolean;
  message?: {
    id?: string;
    role?: string;
    model?: string;
    content?: string | Block[];
    usage?: Record<string, unknown>;
    stop_reason?: string;
  };
}
export const validId = (value: unknown): value is string =>
  typeof value === "string" && /^[a-zA-Z0-9_-]{1,128}$/.test(value);
const builtin = new Set([
  "Read",
  "Write",
  "Edit",
  "Bash",
  "Glob",
  "Grep",
  "Agent",
  "Task",
  "WebFetch",
  "WebSearch",
  "NotebookEdit",
  "TodoWrite",
  "ToolSearch",
  "Skill",
  "AskUserQuestion",
  "EnterPlanMode",
  "ExitPlanMode",
]);
export const safeTool = (name: unknown): string =>
  typeof name === "string" && builtin.has(name)
    ? name
    : typeof name === "string" && name.startsWith("mcp__")
      ? "mcp_other"
      : "tool";
const ms = (value?: string): number | undefined => {
  const n = value ? Date.parse(value) : NaN;
  return Number.isFinite(n) ? n : undefined;
};
const blocks = (row: Row): Block[] =>
  Array.isArray(row.message?.content) ? row.message.content : [];
const isInterruption = (row: Row): boolean =>
  row.type === "user" &&
  (typeof row.message?.content === "string"
    ? /^\[Request interrupted by user(?: for tool use)?\]$/.test(row.message.content)
    : blocks(row).length === 1 &&
      blocks(row)[0].type === "text" &&
      /^\[Request interrupted by user(?: for tool use)?\]$/.test(blocks(row)[0].text ?? ""));
export const isHumanPrompt = (row: Row): boolean =>
  row.type === "user" &&
  !isInterruption(row) &&
  !row.isMeta &&
  row.promptSource !== "system" &&
  row.turnOrigin !== "task_notification" &&
  !blocks(row).some((b) => b.type === "tool_result");
function text(value: unknown, config: ResolvedConfig): string | undefined {
  if (value === undefined) return undefined;
  let s =
    typeof value === "string" ? value : JSON.stringify(redactValue(value));
  for (const key of [config.credentials?.apiKey, config.credentials?.secretKey])
    if (key) s = s.split(key).join("[REDACTED]");
  return redact(s).slice(0, 20000);
}
function resultContent(value: unknown): unknown {
  if (typeof value === "string") return value;
  if (Array.isArray(value))
    return value
      .filter((v) => v && v.type === "text" && typeof v.text === "string")
      .map((v) => ({ type: "text", text: v.text }));
  return undefined;
}
function messageContent(row: Row, config: ResolvedConfig): unknown {
  if (row.isApiErrorMessage) return "[provider error]";
  if (typeof row.message?.content === "string") return row.message.content;
  // Never export thinking, image/base64 blocks, signatures, or host bookkeeping.
  return blocks(row)
    .filter((b) => ["text", "tool_use", "tool_result"].includes(b.type ?? ""))
    .map((b) =>
      b.type === "text"
        ? { type: "text", text: b.text }
        : b.type === "tool_use"
          ? { type: "tool_use", name: safeTool(b.name), input: b.input }
          : {
              type: "tool_result",
              content:
                b.is_error && config.hideErrorDetails
                  ? "[tool error]"
                  : resultContent(b.content),
              is_error: b.is_error,
            },
    );
}
function usage(row: Row): UsageInput | undefined {
  const raw = row.message?.usage;
  if (!raw) return undefined;
  const read = (key: string): number | undefined =>
    typeof raw[key] === "number" &&
    Number.isSafeInteger(raw[key]) &&
    (raw[key] as number) >= 0
      ? (raw[key] as number)
      : undefined;
  return {
    input: read("input_tokens"),
    output: read("output_tokens"),
    cacheRead: read("cache_read_input_tokens"),
    cacheWrite: read("cache_creation_input_tokens"),
    cacheInInput: false,
    reasoningInOutput: true,
  };
}

async function openTranscript(path: string, config: ResolvedConfig) {
  const root = resolve(dirname(config.paths.user), "projects");
  const file = resolve(path);
  if (!file.startsWith(root + sep) || !basename(file).endsWith(".jsonl"))
    throw new Error("TRANSCRIPT_OUTSIDE_HOST");
  await safeParents(dirname(file));
  const handle = await fs.open(file, constants.O_RDONLY | constants.O_NOFOLLOW);
  try {
    const st = await handle.stat();
    if (
      !st.isFile() ||
      st.nlink !== 1 ||
      (process.getuid && st.uid !== process.getuid())
    )
      throw new Error("UNSAFE_TRANSCRIPT");
    return { handle, size: st.size };
  } catch (error) {
    await handle.close();
    throw error;
  }
}

/** Tail-only attachment/recovery. A huge prior conversation never requires an unbounded read. */
export async function transcriptCheckpoint(
  path: string,
  config: ResolvedConfig,
  promptId?: string,
): Promise<number> {
  const { handle, size } = await openTranscript(path, config);
  try {
    if (!promptId) return size;
    const start = Math.max(0, size - 2 * 1024 * 1024);
    const buffer = Buffer.alloc(size - start);
    const { bytesRead } = await handle.read(buffer, 0, buffer.length, start);
    let cursor = start ? buffer.indexOf(10) + 1 : 0;
    while (cursor < bytesRead) {
      const end = buffer.indexOf(10, cursor);
      if (end < 0 || end >= bytesRead) break;
      try {
        const row = JSON.parse(buffer.subarray(cursor, end).toString("utf8"));
        if (row.promptId === promptId && isHumanPrompt(row))
          return start + cursor;
      } catch {
        /* A partial/oversized historical row is not an attachment boundary. */
      }
      cursor = end + 1;
    }
    return size;
  } finally {
    await handle.close();
  }
}

/** Bounded read of a host-provided transcript only, with metadata-only incremental checkpoint.
 * The preceding 2 MiB provides bounded visible conversation context; it is not the API/system prompt.
 */
export async function readTranscript(
  path: string,
  config: ResolvedConfig,
  offset: number,
): Promise<{ rows: Row[]; nextOffset: number; partial: boolean }> {
  const { handle, size } = await openTranscript(path, config);
  try {
    if (!Number.isSafeInteger(offset) || offset < 0 || offset > size)
      throw new Error("TRANSCRIPT_TRUNCATED");
    let start = Math.max(0, offset - 2 * 1024 * 1024);
    if (size - start > 8 * 1024 * 1024) throw new Error("TRANSCRIPT_LIMIT");
    const buffer = Buffer.alloc(size - start);
    const { bytesRead } = await handle.read(buffer, 0, buffer.length, start);
    const end = buffer.subarray(0, bytesRead).lastIndexOf(10);
    if (end < 0) return { rows: [], nextOffset: start, partial: true };
    const begin = start ? buffer.indexOf(10) + 1 : 0;
    const rows: Row[] = [];
    let partial = end + 1 !== bytesRead;
    let cursor = start + begin;
    for (const line of buffer
      .subarray(begin, end)
      .toString("utf8")
      .split("\n")) {
      const byteOffset = cursor;
      cursor += Buffer.byteLength(line) + 1;
      if (!line) continue;
      if (line.length > 1024 * 1024) {
        partial = true;
        continue;
      }
      try {
        const row = JSON.parse(line);
        if (row && typeof row === "object" && !Array.isArray(row))
          rows.push({ ...row, byteOffset });
      } catch {
        partial = true;
      }
      if (rows.length > 10000) throw new Error("TRANSCRIPT_LIMIT");
    }
    return { rows, nextOffset: start + end + 1, partial };
  } finally {
    await handle.close();
  }
}

/** A source not yet created is pending, not a permanent capture failure. */
export async function readPendingTranscript(
  path: string,
  config: ResolvedConfig,
  offset: number,
): Promise<Awaited<ReturnType<typeof readTranscript>>> {
  try {
    return await readTranscript(path, config, offset);
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
    return { rows: [], nextOffset: offset, partial: true };
  }
}

export interface TurnExport {
  offset: number;
  generations: number;
  tools: number;
  outcome: string;
  exported: number;
  dropped: number;
}
export async function exportTranscriptTurn(
  config: ResolvedConfig,
  state: SessionState,
  path: string,
  sessionId: string,
  terminal: "success" | "error" | "interrupted" | "incomplete",
  options: {
    terminalText?: string;
    terminalPromptId?: string;
    final?: boolean;
    beforeExport?: (
      checkpoint: string[],
      children: Record<string, ChildCursor>,
    ) => Promise<void>;
  } = {},
): Promise<TurnExport> {
  if (state.privateTurn)
    config = {
      ...config,
      // resolveConfig intentionally keeps credentials/identity non-enumerable.
      credentials: config.credentials,
      identityKey: config.identityKey,
      revalidate: config.revalidate,
      captureContent: false,
      hideInputs: true,
      hideOutputs: true,
    };
  let read = await readPendingTranscript(path, config, state.offset);
  // A text match is not a completion marker: a tool-calling response can say the
  // exact same thing as the final answer. Require the latest response identity,
  // a completed non-tool stop reason, and no intervening user/tool-result row.
  // Keep this fence across SessionEnd retries even when that hook has no text.
  const expectedHash =
    options.terminalText === undefined
      ? state.terminalTextHash
      : contentHash(options.terminalText);
  const terminalPresent = () => {
    if (expectedHash === undefined) return true;
    const scoped = read.rows.filter((r) => r.sessionId === sessionId);
    const from = scoped.findIndex(
      (r) =>
        r.type === "user" &&
        r.promptId ===
          (options.terminalPromptId ??
            state.terminalPromptId ??
            state.promptId) &&
        !blocks(r).some((b) => b.type === "tool_result"),
    );
    if (from < 0) return false;
    const current = scoped.slice(from);
    const nextPrompt = current.findIndex((r, i) => i > 0 && isHumanPrompt(r));
    if (nextPrompt >= 0) current.splice(nextPrompt);
    if (current.some(isInterruption)) return true;
    const messages = current.filter(
      (r) => r.type === "user" || r.type === "assistant",
    );
    const last = messages.at(-1);
    if (
      last?.type !== "assistant" ||
      !validId(last.requestRef) ||
      !validId(last.message?.id) ||
      !["end_turn", "max_tokens", "stop_sequence", "refusal"].includes(
        last.message?.stop_reason ?? "",
      )
    )
      return false;
    const response = messages.filter(
      (r) =>
        r.type === "assistant" &&
        r.requestRef === last.requestRef &&
        r.message?.id === last.message?.id,
    );
    if (response.some((r) => blocks(r).some((b) => b.type === "tool_use")))
      return false;
    const output = response
      .map((r) =>
        typeof r.message?.content === "string"
          ? r.message.content
          : blocks(r)
              .filter((b) => b.type === "text")
              .map((b) => b.text ?? "")
              .join("\n"),
      )
      .join("\n");
    return contentHash(output) === expectedHash;
  };
  for (let i = 0; (read.partial || !terminalPresent()) && i < 30; i++) {
    await new Promise((r) => setTimeout(r, 100));
    read = await readPendingTranscript(path, config, state.offset);
  }
  if (read.partial || !terminalPresent())
    throw new Error("TERMINAL_TRANSCRIPT_PENDING");
  const rows = read.rows.filter(
    (r) =>
      r.sessionId === sessionId &&
      (r.type === "user" ||
        r.type === "assistant" ||
        r.subtype === "compact_boundary"),
  );
  const start = rows.findIndex(
    (r) =>
      r.type === "user" &&
      r.promptId === state.promptId &&
      !blocks(r).some((b) => b.type === "tool_result"),
  );
  if (start < 0) throw new Error("PROMPT_NOT_IN_TRANSCRIPT");
  const current = rows.slice(start);
  const qualified = (r: Row) =>
    supportedHost(r.version) &&
    (r.type !== "assistant" ||
      (validId(r.message?.id) &&
        validId(r.requestRef) &&
        r.message?.role === "assistant" &&
        (Array.isArray(r.message.content) ||
          typeof r.message.content === "string")));
  // Unknown records are gaps, not grounds for silently losing the whole turn.
  let schemaGap = current.some((r) => !r.isApiErrorMessage && !qualified(r));
  // Refuse to combine a later user prompt with this terminal callback.
  const nextPrompt = current.findIndex(
    (r, i) =>
      i > 0 && isHumanPrompt(r) && r.promptId && r.promptId !== state.promptId,
  );
  if (nextPrompt >= 0) current.splice(nextPrompt);
  // Resolve child sources and reserve all attempted source identities before any network write.
  // A killed hook must not replay already-accepted usage on the next Stop.
  const childSources = new Map<
    string,
    {
      rows: Row[];
      partial: boolean;
      offset: number;
      floor?: number;
      nextOffset: number;
      path: string;
    }
  >();
  for (const child of state.subagents.slice(0, 16)) {
    try {
      const sub = await readTranscript(child.path, config, child.offset ?? 0);
      // A hook timestamp is not a source boundary: Claude writes the delegated
      // prompt before SubagentStart. Resolve fresh invocations from the calling
      // tool below; persisted privacy floors always remain authoritative.
      const offset = child.offset ?? sub.nextOffset;
      const floor = child.contentFloor;
      childSources.set(child.id, {
        rows: sub.rows.filter(
          (r) =>
            r.agentId === child.id &&
            (r.type === "user" || r.type === "assistant"),
        ),
        partial: sub.partial,
        offset,
        floor,
        nextOffset: sub.nextOffset,
        path: child.path,
      });
    } catch {
      /* Mark unavailable children incomplete, without checkpointing them. */
    }
  }
  type Source = {
    records: Row[];
    prefix: string;
    parentId?: string;
    history: Row[];
    floor: number;
    childId?: string;
  };
  const sources: Source[] = [
    {
      records: current,
      prefix: "",
      history: rows.slice(0, start),
      floor: state.contentFloor ?? 0,
    },
  ];
  const scheduled = new Set<string>();
  const childCheckpoints: Record<string, ChildCursor> = {};
  // Resolve the full source graph parent-first, independent of SubagentStop order.
  for (let index = 0; index < sources.length; index++) {
    const source = sources[index];
    for (const row of source.records) {
      if (!qualified(row)) continue;
      for (const tool of blocks(row)) {
        if (tool.type !== "tool_use" || !validId(tool.id)) continue;
        const result = source.records.find((r) =>
          blocks(r).some(
            (b) => b.type === "tool_result" && b.tool_use_id === tool.id,
          ),
        );
        const id =
          result?.toolUseResult?.agentId ??
          Object.entries(state.agentParents ?? {}).find(
            ([, parent]) =>
              parent.toolId === tool.id &&
              parent.parentAgentId === source.childId,
          )?.[0];
        if (!validId(id) || scheduled.has(id)) continue;
        const child = childSources.get(id);
        if (!child || child.partial) {
          schemaGap = true;
          continue;
        }
        const delegated =
          tool.input && typeof tool.input === "object"
            ? (tool.input as Record<string, unknown>).prompt
            : undefined;
        const invocation = [...child.rows].reverse().find(
          (r) =>
            isHumanPrompt(r) &&
            (r.byteOffset ?? 0) >= child.offset &&
            (typeof delegated === "string"
              ? (typeof r.message?.content === "string"
                  ? r.message.content
                  : blocks(r)
                      .filter((b) => b.type === "text")
                      .map((b) => b.text ?? "")
                      .join("\n")) === delegated
              : r.parentToolUseID === tool.id),
        );
        const floor = child.floor ?? invocation?.byteOffset;
        if (floor === undefined) {
          schemaGap = true;
          continue; // Unknown invocation: preserve recovery, never backfill history.
        }
        const offset =
          child.floor === undefined
            ? Math.max(child.offset, floor)
            : child.offset;
        scheduled.add(id);
        sources.push({
          records: child.rows.filter((r) => (r.byteOffset ?? 0) >= offset),
          history: child.rows.filter((r) => (r.byteOffset ?? 0) < offset),
          prefix: `child:${id}:`,
          parentId: `${source.prefix}tool:${tool.id}`,
          floor,
          childId: id,
        });
        childCheckpoints[id] = {
          path: child.path,
          offset: child.nextOffset,
          contentFloor:
            config.captureContent && !state.privateTurn
              ? floor
              : child.nextOffset,
        };
      }
    }
  }
  if (state.subagents.some((child) => !scheduled.has(child.id)))
    schemaGap = true;
  const planned = new Set(state.checkpoint ?? []);
  const reserve = (records: Row[], prefix = "") => {
    for (const row of records) {
      if (
        !qualified(row) ||
        row.type !== "assistant" ||
        !validId(row.message?.id) ||
        !validId(row.requestRef)
      )
        continue;
      planned.add(contentHash(`${prefix}${row.requestRef}:${row.message!.id}`));
      for (const block of blocks(row))
        if (block.type === "tool_use" && validId(block.id))
          planned.add(contentHash(`${prefix}tool:${block.id}`));
    }
  };
  for (const source of sources) reserve(source.records, source.prefix);
  for (const [i] of state.compactions.slice(0, 32).entries())
    planned.add(contentHash(`compaction:${i}`));
  if (planned.size > 240) throw new Error("TRANSCRIPT_LIMIT");
  await options.beforeExport?.([...planned], childCheckpoints);
  const { createTurnTracer } = await import("@traceai/agent-core/tracer");
  const tracer = createTurnTracer({ config });
  const root = tracer.startTurn({
    sessionId,
    turnId: state.promptId!,
    input: text(messageContent(current[0], config), config),
    startTime: state.start ?? ms(current[0].timestamp),
    usagePlan: "request",
    deferRoot: options.final === false,
    checkpoint: state.checkpoint,
  });
  let generations = 0,
    tools = 0,
    lastOutput: string | undefined;
  let incomplete = read.partial || !!state.gap || schemaGap;
  function emit(
    records: Row[],
    parentId?: string,
    prefix = "",
    historyPrefix: Row[] = [],
    contentFloor = 0,
  ): void {
    const groups = new Map<string, Row[]>();
    for (const row of records) {
      if (
        !qualified(row) ||
        row.type !== "assistant" ||
        !validId(row.message?.id) ||
        !validId(row.requestRef)
      )
        continue;
      // Tool/text streaming fragments of one request share this identity; usage belongs to it once.
      const id = `${prefix}${row.requestRef}:${row.message!.id}`;
      const group = groups.get(id) ?? [];
      group.push(row);
      groups.set(id, group);
    }
    for (const [id, group] of groups) {
      const first = group[0],
        last = group.at(-1)!;
      const firstIndex = records.indexOf(first);
      const history = [...historyPrefix, ...records.slice(0, firstIndex)]
        .filter((r) => r.message && (r.byteOffset ?? 0) >= contentFloor)
        .map((r) => ({
          role: r.message!.role,
          content: messageContent(r, config),
        }));
      const output = group.flatMap((r) =>
        typeof r.message?.content === "string"
          ? [r.message.content]
          : [messageContent(r, config)],
      );
      const model =
        typeof first.message?.model === "string" &&
        /^[a-zA-Z0-9_.:/-]{1,128}$/.test(first.message.model)
          ? first.message.model
          : undefined;
      const generation = root.child({
        id,
        kind: "generation",
        name: "claude.generation",
        model,
        provider: "anthropic",
        parentId,
        input:
          (first.byteOffset ?? 0) >= contentFloor
            ? text(history, config)
            : undefined,
        fidelity: "reconstructed-message",
        timing: "reconstructed",
        startTime: ms(first.timestamp),
      });
      const contentAllowed = (first.byteOffset ?? 0) >= contentFloor;
      const generationOutput = contentAllowed
        ? text(output, config)
        : undefined;
      if (!prefix) {
        // The turn root shows the final reply as plain text, like other agents'
        // roots; the generation keeps its structured content blocks.
        const reply = group
          .flatMap((r) =>
            typeof r.message?.content === "string"
              ? [r.message.content]
              : blocks(r).filter((b) => b.type === "text").map((b) => b.text ?? ""),
          )
          .filter(Boolean)
          .join("\n");
        lastOutput = contentAllowed ? (reply ? text(reply, config) : generationOutput) : undefined;
      }
      generation.end({
        output: generationOutput,
        usage: usage(last),
        endTime: ms(last.timestamp),
      });
      generations++;
      for (const row of group)
        for (const b of blocks(row)) {
          if (b.type !== "tool_use" || !validId(b.id)) continue;
          const toolId = `${prefix}tool:${b.id}`;
          const endRow = records.find((r) =>
            blocks(r).some(
              (result) =>
                result.type === "tool_result" && result.tool_use_id === b.id,
            ),
          );
          const result =
            endRow &&
            blocks(endRow).find(
              (result) =>
                result.type === "tool_result" && result.tool_use_id === b.id,
            );
          const timing = state.tools[b.id];
          const tool = root.child({
            id: toolId,
            kind: "tool",
            name: safeTool(b.name),
            toolCallId: b.id,
            input: contentAllowed ? text(b.input, config) : undefined,
            parentId: id,
            fidelity: "reconstructed-message",
            timing: timing ? "source" : "reconstructed",
            startTime: timing?.start ?? ms(row.timestamp),
          });
          const outcome =
            result?.is_error || timing?.failed
              ? "error"
              : result
                ? "success"
                : "incomplete";
          if (outcome === "incomplete") incomplete = true;
          tool.end({
            output: contentAllowed
              ? text(resultContent(result?.content), config)
              : undefined,
            outcome,
            endTime: timing?.end ?? ms(endRow?.timestamp),
          });
          tools++;
        }
    }
  }
  try {
    // Prefix bounded visible history, but emit only generations after this prompt.
    for (const source of sources) {
      if (source.records.some((r) => !r.isApiErrorMessage && !qualified(r)))
        incomplete = true;
      emit(
        source.records,
        source.parentId,
        source.prefix,
        source.history,
        source.floor,
      );
    }
    for (const [i, time] of state.compactions.slice(0, 32).entries()) {
      root
        .child({
          id: `compaction:${i}`,
          kind: "compaction",
          name: "claude.compaction",
          startTime: time,
        })
        .end({ endTime: time });
    }
    if (!generations) incomplete = true;
    const interrupted = current.some(isInterruption);
    const lastModel = current.filter((r) => r.type === "assistant").at(-1);
    const outcome = interrupted
      ? "interrupted"
      : lastModel?.isApiErrorMessage
        ? "error"
        : terminal === "success" && incomplete
          ? "incomplete"
          : terminal;
    root.end({
      // An unfinished tool-call response is not the interrupted prompt's answer.
      output: interrupted || outcome === "incomplete" ? undefined : lastOutput,
      outcome,
      endTime: state.stopTime ?? Date.now(),
    });
    await tracer.shutdown();
    return {
      offset: read.nextOffset,
      generations,
      tools,
      outcome,
      exported: tracer.state.exported,
      dropped: tracer.state.dropped,
    };
  } finally {
    await tracer.shutdown();
  }
}
