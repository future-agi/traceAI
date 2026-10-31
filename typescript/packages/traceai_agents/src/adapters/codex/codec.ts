import {
  sessionId,
  workId,
  spanId,
  traceId,
  usageId,
  planId,
  minimize,
  type CanonicalFact,
  type WorkKind,
} from "@traceai/agent-core";
import type { NormalizeContext } from "../../adapters.js";
import { AgentError } from "../../security/files.js";

export const CODEX_SCHEMA = "codex-rollout-0165-0166-v1";
export const CODEX_VERSIONS = ["0.165.0", "0.166.0"] as const;
export const NAMESPACE = "openai.codex/cli-conversation";
export const MODELS = [
  "gpt-5",
  "gpt-5-codex",
  "gpt-5.1",
  "gpt-5.1-codex",
  "gpt-5.2",
  "gpt-5.2-codex",
  "gpt-5.3-codex",
  "gpt-5.4",
  "fixture-model",
];
export const TOOLS = [
  "exec_command",
  "write_stdin",
  "spawn_agent",
  "send_input",
  "wait",
  "apply_patch",
  "update_plan",
  "command_execution",
  "dynamic_tool",
  "agent_tool",
  "mcp_tool",
  "file_change",
  "web_search",
  "image_view",
];
const MAX_TURNS = 64,
  MAX_FACTS = 128;
const id = (v: unknown): v is string =>
  typeof v === "string" && /^[a-zA-Z0-9_-]{1,256}$/.test(v);
const integer = (v: unknown): v is number =>
  Number.isSafeInteger(v) && Number(v) >= 0;
function timestamp(v: unknown): string {
  if (typeof v !== "string" || !/^\d{4}-\d\d-\d\dT/.test(v))
    throw new AgentError("INVALID_SOURCE_TIME", 5);
  const ms = Date.parse(v);
  if (!integer(ms)) throw new AgentError("INVALID_SOURCE_TIME", 5);
  return (BigInt(ms) * 1_000_000n).toString();
}
function milliseconds(v: unknown, fallback: string): string {
  return integer(v) ? (BigInt(v) * 1_000_000n).toString() : fallback;
}
interface Turn {
  native: string;
  root: CanonicalFact;
  facts: Map<string, CanonicalFact>;
  tools: Map<string, CanonicalFact>;
  terminal: boolean;
  parent?: Origin;
  last: string;
}
interface Origin {
  session: string;
  thread: string;
  turn: string;
}
const originKey = (o: Origin) => JSON.stringify([o.session, o.thread, o.turn]);
/** Bounded, in-memory join of explicitly observed files. Never searches for parents
 * by time/path. Imports must explicitly select the parent and child rollouts. */
export class CodexGraph {
  private turns = new Map<string, { root: CanonicalFact; parent?: Origin }>();
  private spawns = new Map<string, CanonicalFact>();
  terminal(origin: Origin, root: CanonicalFact, parent?: Origin): void {
    if (this.turns.size >= 1024 && !this.turns.has(originKey(origin)))
      throw new AgentError("SOURCE_GRAPH_LIMIT", 4);
    this.turns.set(originKey(origin), { root, parent });
  }
  spawn(origin: Origin, child: string, fact: CanonicalFact): void {
    const key = originKey(origin) + JSON.stringify(child);
    const old = this.spawns.get(key);
    if (old && old.sourceWorkId !== fact.sourceWorkId)
      throw new AgentError("HELD_SPAWN_CONFLICT", 3);
    if (!old && this.spawns.size >= 1024)
      throw new AgentError("SOURCE_GRAPH_LIMIT", 4);
    this.spawns.set(key, fact);
  }
  topology(
    origin: Origin,
    seen = new Set<string>(),
  ): Pick<CanonicalFact, "traceId" | "parentSpanId" | "links"> | undefined {
    const key = originKey(origin),
      node = this.turns.get(key);
    if (!node || seen.has(key) || seen.size >= 16) return undefined;
    if (!node.parent) return { traceId: node.root.traceId };
    const spawn = this.spawns.get(
      originKey(node.parent) + JSON.stringify(origin.thread),
    );
    if (!spawn) return undefined;
    seen.add(key);
    const parent = this.topology(node.parent, seen);
    if (!parent) return undefined;
    // Foreground containment needs the proven spawning interval, not merely an
    // overlapping parent turn. Async spawn_agent usually returns before its child.
    const contained =
      spawn.timing === "message_interval" &&
      node.root.timing === "message_interval" &&
      BigInt(spawn.startNs) < BigInt(spawn.endNs) &&
      BigInt(spawn.startNs) <= BigInt(node.root.startNs) &&
      BigInt(spawn.endNs) >= BigInt(node.root.endNs);
    return contained
      ? { traceId: parent.traceId, parentSpanId: spawn.spanId }
      : {
          traceId: node.root.traceId,
          links: [{ traceId: parent.traceId, spanId: spawn.spanId }],
        };
  }
}
/** Pinned durable rollout schema. No callback FIFO pairing, token_count deltas,
 * message-to-request guessing, content hashes or file coordinates in identity. */
export class CodexCodec {
  private meta?: {
    session: string;
    thread: string;
    version: string;
    parent?: string;
  };
  private turns = new Map<string, Turn>();
  private observed = 0;
  private retained = 0;
  private historyStart?: number;
  constructor(
    private readonly context: NormalizeContext,
    private readonly expectedSession?: string,
    private readonly graph = new CodexGraph(),
  ) {
    if (context.sourceSchema !== CODEX_SCHEMA)
      throw new AgentError("UNKNOWN_SOURCE_SCHEMA", 5);
  }
  private base(
    turn: string,
    unit: WorkKind,
    anchor: string | undefined,
    at: string,
  ): CanonicalFact {
    const m = this.meta!;
    const session = [m.session],
      origin = [m.thread, turn];
    const tw = workId(
      this.context.identityKey,
      NAMESPACE,
      "turn",
      session,
      origin,
    );
    const w =
      unit === "turn"
        ? tw
        : workId(this.context.identityKey, NAMESPACE, unit, session, origin, [
            m.thread,
            turn,
            anchor!,
          ]);
    return {
      schemaVersion: 1,
      sourceWorkId: w,
      identityProfile: "codex-response-v1",
      sessionId: sessionId(this.context.identityKey, NAMESPACE, session),
      turnId: tw,
      traceId: traceId(tw),
      spanId: spanId(w),
      ...(unit === "turn" ? {} : { parentSpanId: spanId(tw) }),
      agent: "codex",
      surface: "cli",
      agentVersion: m.version,
      source: "transcript",
      sourceSchema: CODEX_SCHEMA,
      reconstructionVersion: "1",
      policyRevision: this.context.policyRevision,
      unit,
      timing: "unknown",
      startNs: at,
      endNs: at,
      outcome: "incomplete",
      inputCompleteness: "unavailable",
      outputCompleteness: "unavailable",
    };
  }
  private turn(native: unknown, at: string): Turn {
    if (!id(native)) throw new AgentError("HELD_IDENTITY", 3);
    let t = this.turns.get(native);
    if (!t) {
      if (this.turns.size >= MAX_TURNS)
        throw new AgentError("SOURCE_TURN_LIMIT", 4);
      t = {
        native,
        root: this.base(native, "turn", undefined, at),
        facts: new Map(),
        tools: new Map(),
        terminal: false,
        last: at,
      };
      t.root.incomplete = true;
      this.turns.set(native, t);
    }
    if (BigInt(at) > BigInt(t.last)) t.last = at;
    return t;
  }
  private add(t: Turn, f: CanonicalFact): void {
    // Identical replay is harmless. Changed immutable evidence holds, never reprices.
    const clean = minimize(f);
    const previous = t.facts.get(clean.sourceWorkId);
    if (previous && JSON.stringify(previous) !== JSON.stringify(clean))
      throw new AgentError("HELD_SOURCE_CONFLICT", 3);
    if (!previous && t.facts.size >= MAX_FACTS)
      throw new AgentError("SOURCE_FACT_LIMIT", 4);
    if (!previous && ++this.retained > 1024)
      throw new AgentError("SOURCE_FACT_LIMIT", 4);
    t.facts.set(clean.sourceWorkId, clean);
  }
  push(record: unknown): void {
    if (!record || typeof record !== "object")
      throw new AgentError("UNKNOWN_SOURCE_SCHEMA", 5);
    const r = record as any,
      p = r.payload;
    if (!p || typeof p !== "object")
      throw new AgentError("UNKNOWN_SOURCE_SCHEMA", 5);
    const at = timestamp(r.timestamp);
    if (r.type === "session_meta") {
      if (
        !id(p.id) ||
        !id(p.session_id) ||
        !CODEX_VERSIONS.includes(p.cli_version)
      )
        throw new AgentError("UNSUPPORTED_CODEX_VERSION", 5);
      // Fork/revert/paginated history needs origin receipts; do not rebill inherited history.
      if (p.forked_from_id || p.history_base || p.history_mode !== "paginated")
        throw new AgentError("HELD_HISTORY_MODE", 3);
      if (this.expectedSession && this.expectedSession !== p.session_id)
        throw new AgentError("SOURCE_SESSION_MISMATCH", 3);
      const meta = {
        session: p.session_id,
        thread: p.id,
        version: p.cli_version,
        ...(id(p.parent_thread_id) ? { parent: p.parent_thread_id } : {}),
      };
      if (this.meta && JSON.stringify(meta) !== JSON.stringify(this.meta))
        throw new AgentError("SOURCE_CHANGED", 6);
      if (p.subagent_history_start_ordinal !== undefined) {
        if (!integer(p.subagent_history_start_ordinal))
          throw new AgentError("HELD_HISTORY_MODE", 3);
        this.historyStart = p.subagent_history_start_ordinal;
      }
      this.meta = meta;
      return;
    }
    if (!this.meta) throw new AgentError("HELD_IDENTITY", 3);
    if (this.historyStart !== undefined) {
      if (!integer(r.ordinal)) throw new AgentError("HELD_HISTORY_MODE", 3);
      if (r.ordinal < this.historyStart) return;
    }
    if (r.type === "turn_context") {
      const t = this.turn(p.turn_id, at);
      if (MODELS.includes(p.model)) t.root.model = p.model;
      return;
    }
    if (r.type === "token_usage_record") {
      if (
        p.thread_id !== this.meta.thread ||
        p.session_id !== this.meta.session ||
        !id(p.response_id)
      )
        throw new AgentError("HELD_IDENTITY", 3);
      const t = this.turn(p.turn_id, at),
        u = p.usage;
      if (
        !u ||
        ![u.input_tokens, u.output_tokens, u.total_tokens].every(integer) ||
        u.total_tokens !== u.input_tokens + u.output_tokens
      )
        throw new AgentError("UNRESOLVED_USAGE", 3);
      const f = this.base(t.native, "request", p.response_id, at);
      f.model = t.root.model;
      f.outcome = "success";
      const fields = {
        cacheRead: u.cached_input_tokens,
        cacheWrite: u.cache_write_input_tokens,
        reasoning: u.reasoning_output_tokens,
      };
      for (const value of Object.values(fields))
        if (value !== undefined && !integer(value))
          throw new AgentError("UNRESOLVED_USAGE", 3);
      if (
        (fields.cacheRead ?? 0) > u.input_tokens ||
        (fields.reasoning ?? 0) > u.output_tokens
      )
        throw new AgentError("UNRESOLVED_USAGE", 3);
      f.usage = {
        scope: "request",
        contribution: "request",
        planId: planId(f.turnId),
        factIds: [
          usageId(
            this.context.identityKey,
            f.sourceWorkId,
            "request",
            "tokens",
            [p.response_id],
          ),
        ],
        input: u.input_tokens,
        output: u.output_tokens,
        total: u.total_tokens,
        ...fields,
        inclusions: "inclusive",
        complete: true,
      };
      this.add(t, f);
      this.observed++;
      return;
    }
    if (
      r.type === "response_item" &&
      [
        "function_call",
        "custom_tool_call",
        "function_call_output",
        "custom_tool_call_output",
      ].includes(p.type)
    ) {
      const origin = p.internal_chat_message_metadata_passthrough?.turn_id;
      if (!id(origin) || !id(p.call_id))
        throw new AgentError("HELD_TOOL_IDENTITY", 3);
      const t = this.turn(origin, at);
      const candidate = this.base(origin, "tool", p.call_id, at);
      const prior = t.tools.get(candidate.sourceWorkId);
      if (!prior && t.tools.size >= MAX_FACTS)
        throw new AgentError("SOURCE_FACT_LIMIT", 4);
      if (!prior && ++this.retained > 1024)
        throw new AgentError("SOURCE_FACT_LIMIT", 4);
      const f = prior ?? candidate;
      if (p.type.endsWith("_output")) {
        f.endNs = at;
        // A result string is not a structured success/error outcome. Do not
        // parse shell output text or pretend an execution was successful.
        f.incomplete = true;
        if (prior) f.timing = "message_interval";
      } else {
        f.startNs = at;
        const names: Record<string, string> = {
          exec_command: "command_execution",
          write_stdin: "command_execution",
          spawn_agent: "agent_tool",
          send_input: "agent_tool",
          wait: "agent_tool",
          apply_patch: "file_change",
        };
        f.tool = TOOLS.includes(p.name)
          ? p.name
          : (names[p.name] ?? "dynamic_tool");
      }
      t.tools.set(f.sourceWorkId, f);
      return;
    }
    if (r.type !== "event_msg") return; // response_item text/chunks and compacted history never become requests
    if (p.type === "thread_rolled_back")
      throw new AgentError("HELD_HISTORY_MODE", 3);
    if (p.type === "task_started") {
      const t = this.turn(p.turn_id, at);
      t.root.startNs = at;
      delete t.root.incomplete;
      t.root.timing = "message_interval";
      const parentTurn = p.turn_attribution?.parent_turn_id;
      if (this.meta.parent && id(parentTurn))
        t.parent = {
          session: this.meta.session,
          thread: this.meta.parent,
          turn: parentTurn,
        };
      else if (this.meta.parent) throw new AgentError("HELD_SPAWN_CONTEXT", 3);
      return;
    }
    if (p.type === "task_complete" || p.type === "turn_aborted") {
      const t = this.turn(p.turn_id, at);
      t.root.endNs = at;
      t.root.outcome = p.error
        ? "error"
        : p.type === "turn_aborted"
          ? p.reason === "interrupted"
            ? "interrupted"
            : "cancelled"
          : "success";
      if (BigInt(t.root.startNs) > BigInt(at))
        throw new AgentError("INVALID_SOURCE_TIME", 5);
      if (this.meta.parent && !t.parent)
        throw new AgentError("HELD_SPAWN_CONTEXT", 3);
      t.terminal = true;
      this.graph.terminal(
        {
          session: this.meta.session,
          thread: this.meta.thread,
          turn: t.native,
        },
        t.root,
        t.parent,
      );
      return;
    }
    if (p.type !== "item_completed") return;
    if (p.thread_id !== this.meta.thread || !p.item || !id(p.item.id))
      throw new AgentError("HELD_IDENTITY", 3);
    const i = p.item;
    const kinds: Record<string, string> = {
      CommandExecution: "command_execution",
      DynamicToolCall: "dynamic_tool",
      CollabAgentToolCall: "agent_tool",
      McpToolCall: "mcp_tool",
      FileChange: "file_change",
      WebSearch: "web_search",
      ImageView: "image_view",
    };
    if (!kinds[i.type] && i.type !== "ContextCompaction") return;
    const t = this.turn(p.turn_id, at);
    const f = this.base(
      t.native,
      i.type === "ContextCompaction" ? "compaction" : "tool",
      i.id,
      at,
    );
    f.startNs = milliseconds(p.started_at_ms, at);
    f.endNs = milliseconds(p.completed_at_ms, at);
    if (BigInt(f.startNs) > BigInt(f.endNs))
      throw new AgentError("INVALID_SOURCE_TIME", 5);
    f.timing =
      integer(p.started_at_ms) && integer(p.completed_at_ms)
        ? "message_interval"
        : "unknown";
    if (f.unit === "tool") f.tool = kinds[i.type];
    f.outcome =
      i.status === "interrupted"
        ? "interrupted"
        : i.status === "failed" ||
            i.status === "declined" ||
            i.error ||
            (Number.isInteger(i.exit_code) && i.exit_code !== 0)
          ? i.status === "declined"
            ? "permission_denied"
            : "error"
          : "success";
    this.add(t, f);
    if (i.type === "CollabAgentToolCall" && i.tool === "spawn_agent") {
      if (
        i.sender_thread_id !== this.meta.thread ||
        !Array.isArray(i.receiver_thread_ids) ||
        i.receiver_thread_ids.length > 64 ||
        !i.receiver_thread_ids.every(id)
      )
        throw new AgentError("HELD_SPAWN_CONTEXT", 3);
      for (const child of i.receiver_thread_ids)
        this.graph.spawn(
          {
            session: this.meta.session,
            thread: this.meta.thread,
            turn: t.native,
          },
          child,
          f,
        );
    }
  }
  get pendingAncestry(): number {
    return [...this.turns.values()].filter(
      (t) =>
        t.terminal &&
        !this.graph.topology({
          session: this.meta!.session,
          thread: this.meta!.thread,
          turn: t.native,
        }),
    ).length;
  }
  /** Only explicit terminal evidence finalizes a root. EOF, an import snapshot,
   * and a quiet callback do not prove that a source can no longer append. */
  facts(): CanonicalFact[] {
    const out: CanonicalFact[] = [];
    for (const t of this.turns.values()) {
      if (!t.terminal) continue;
      const topology = this.graph.topology({
        session: this.meta!.session,
        thread: this.meta!.thread,
        turn: t.native,
      });
      if (!topology) continue;
      const root = { ...t.root, ...topology };
      const children = [
        ...t.facts.values(),
        ...[...t.tools]
          .filter(([id]) => !t.facts.has(id))
          .map(([, f]) => minimize(f)),
      ];
      out.push(
        minimize(root),
        ...children.map((f) => minimize({ ...f, traceId: root.traceId })),
      );
    }
    return out;
  }
  get pendingTurns(): number {
    return [...this.turns.values()].filter((t) => !t.terminal).length;
  }
  get summary(): { requests: number; turns: number } {
    return { requests: this.observed, turns: this.turns.size };
  }
}
export function importCodex(
  bytes: Buffer,
  context: NormalizeContext,
): readonly CanonicalFact[] {
  if (!bytes.length || bytes[bytes.length - 1] !== 10)
    throw new AgentError("PARTIAL_SOURCE_TAIL", 4);
  const graph = new CodexGraph(),
    codecs: CodexCodec[] = [];
  let codec: CodexCodec | undefined;
  for (const line of bytes.toString("utf8").split("\n")) {
    if (!line.trim()) continue;
    try {
      const record = JSON.parse(line);
      if (record.type === "session_meta") {
        if (codecs.length >= 16)
          throw new AgentError("IMPORT_SLICE_REQUIRED", 4);
        codec = new CodexCodec(context, undefined, graph);
        codecs.push(codec);
      }
      if (!codec) throw new AgentError("HELD_IDENTITY", 3);
      codec.push(record);
    } catch (e) {
      if (e instanceof AgentError) throw e;
      throw new AgentError("UNKNOWN_SOURCE_SCHEMA", 5);
    }
  }
  if (codecs.some((c) => c.pendingAncestry))
    throw new AgentError("HELD_SPAWN_CONTEXT", 3);
  return codecs.flatMap((c) => c.facts());
}
