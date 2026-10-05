/**
 * Span model for one `query()` call.
 *
 * The Claude Agent SDK emits a message stream, not spans. `QueryTracer` turns
 * that stream into:
 *
 *   claude_agent.conversation            (conversation, CHAIN)
 *   └─ claude_agent.assistant_turn       (assistant_turn, LLM)
 *      └─ tool.<name>                    (tool_execution | mcp_tool, TOOL)
 *         └─ claude_agent.subagent.<t>   (subagent, AGENT)      Task/Agent tool only
 *            └─ claude_agent.assistant_turn ... (the subagent's own turns)
 *
 * Every public method swallows its own errors: tracing never fails the agent.
 */
import {
  Attributes,
  AttributeValue,
  Context,
  HrTime,
  Span,
  SpanKind,
  SpanStatusCode,
  Tracer,
  diag,
  trace,
} from "@opentelemetry/api";
import { getAttributesFromContext } from "@traceai/fi-core";
import {
  ClaudeAgentAttributes as A,
  ClaudeAgentSpanKind,
  ClaudeAgentSpanKindValue,
  FI_SPAN_KIND_BY_CLAUDE_KIND,
  SUBAGENT_TOOLS,
  SpanNames,
  TraceAIAttributes as T,
  getToolSource,
  parseMcpToolName,
} from "./attributes";
import {
  AbortSignalLike,
  AssistantMessageLike,
  ContentBlockLike,
  OptionsLike,
  QueryParamsLike,
  ResultMessageLike,
  SystemMessageLike,
  UserMessageLike,
} from "./types";

/** Which content the wrapper may copy onto spans. Both default to hidden. */
export interface ContentPolicy {
  hideInputs: boolean;
  hideOutputs: boolean;
}

/** How the wrapped iterator stopped. */
export type FinishOutcome =
  | { kind: "completed" }
  | { kind: "returned" }
  | { kind: "error"; error: unknown }
  | { kind: "aborted"; reason?: unknown };

const PROMPT_MAX = 2000;
const SYSTEM_PROMPT_MAX = 1000;
const CONTENT_MAX = 5000;
const MESSAGE_CONTENT_MAX = 2000;
const ERROR_MESSAGE_MAX = 500;
const TOOL_COMMAND_MAX = 500;

type Scope = string | null;

/**
 * One clock for every start and end time the wrapper sets. OTel falls back to
 * millisecond `Date.now()` for the end of a span whose start time was given,
 * so mixing explicit and implicit times can order a child after its parent.
 */
export function clockMs(): number {
  const perf = (globalThis as { performance?: { now?: () => number; timeOrigin?: number } }).performance;
  if (perf && typeof perf.now === "function" && typeof perf.timeOrigin === "number") {
    return perf.timeOrigin + perf.now();
  }
  return Date.now();
}

export function toHrTime(ms: number): HrTime {
  let seconds = Math.trunc(ms / 1000);
  let nanos = Math.round((ms - seconds * 1000) * 1e6);
  if (nanos >= 1e9) {
    seconds += 1;
    nanos -= 1e9;
  }
  return [seconds, nanos];
}

interface TurnState {
  span: Span;
  scope: Scope;
  messageId?: string;
  textParts: string[];
  toolUseCount: number;
  openToolIds: Set<string>;
  isError: boolean;
  ended: boolean;
}

interface ToolState {
  span: Span;
  name: string;
  startMs: number;
  turn?: TurnState;
}

interface SubagentState {
  span: Span;
  startMs: number;
  /** In the background (run_in_background, task_started, task_updated): ends at task_notification. */
  backgrounded: boolean;
  /** The app called Query.backgroundTasks() and it has not failed (yet). */
  backgroundRequested: boolean;
  toolEnded: boolean;
  toolIsError: boolean;
  status?: string;
  ended: boolean;
}

interface ScopeState {
  currentTurn?: TurnState;
  turnCount: number;
  nextTurnStartMs?: number;
}

/**
 * Running totals a result message carries for its session: `total_cost_usd`
 * and `modelUsage` summed over models (sdk.d.ts:5679, 5687). A field is absent
 * when the result did not carry it.
 */
export interface UsageTotals {
  costUsd?: number;
  inputTokens?: number;
  outputTokens?: number;
  cacheReadTokens?: number;
  cacheCreationTokens?: number;
}

const USAGE_FIELDS = ["costUsd", "inputTokens", "outputTokens", "cacheReadTokens", "cacheCreationTokens"] as const;

/**
 * Process-local map of session id -> the last running totals seen for it, so a
 * resumed, continued or forked session promotes only its new spend. Bounded
 * LRU: the oldest session is dropped past `capacity`.
 */
export class SessionUsageStore {
  private readonly entries = new Map<string, UsageTotals>();

  constructor(private readonly capacity: number) {}

  get size(): number {
    return this.entries.size;
  }

  get(sessionId: string): UsageTotals | undefined {
    const totals = this.entries.get(sessionId);
    if (!totals) return undefined;
    this.entries.delete(sessionId);
    this.entries.set(sessionId, totals);
    return { ...totals };
  }

  set(sessionId: string, totals: UsageTotals): void {
    this.entries.delete(sessionId);
    this.entries.set(sessionId, { ...totals });
    while (this.entries.size > this.capacity) {
      const oldest = this.entries.keys().next().value as string;
      this.entries.delete(oldest);
    }
  }

  clear(): void {
    this.entries.clear();
  }
}

const SESSION_USAGE_CAPACITY = 1000;
const sharedSessionUsage = new SessionUsageStore(SESSION_USAGE_CAPACITY);

/** The store every QueryTracer in this process shares. */
export function sessionUsageStore(): SessionUsageStore {
  return sharedSessionUsage;
}

/** Truncate like the Python package: keep `max` chars, ending in "...". */
export function truncate(value: string, max: number): string {
  return value.length > max ? `${value.slice(0, max - 3)}...` : value;
}

/** Python `safe_json_serialize`: strings as-is, objects as JSON, truncated. */
export function safeJson(value: unknown, max = CONTENT_MAX): string {
  if (value === undefined || value === null) {
    return "";
  }
  let text: string;
  try {
    text = typeof value === "string" ? value : JSON.stringify(value) ?? String(value);
  } catch {
    text = String(value);
  }
  return truncate(text, max);
}

function contentBlocks(content: unknown): ContentBlockLike[] {
  return Array.isArray(content) ? (content as ContentBlockLike[]) : [];
}

/** Flatten a tool_result `content` (string or block list) to text. */
function flattenToolResult(content: unknown): string {
  if (content === undefined || content === null) {
    return "";
  }
  if (typeof content === "string") {
    return content;
  }
  if (Array.isArray(content)) {
    return content
      .map((block: ContentBlockLike) => {
        if (block && typeof block.text === "string") return block.text;
        if (block && block.type === "image") return "[image]";
        return safeJson(block);
      })
      .join("\n");
  }
  return safeJson(content);
}

/** Decide `gen_ai.provider.name` from the environment the CLI subprocess sees. */
export function resolveProviderName(options: OptionsLike | undefined): string {
  // Options.env REPLACES the subprocess environment; when it is absent the
  // subprocess inherits process.env.
  const env =
    options?.env ??
    (typeof process !== "undefined" ? (process.env as Record<string, string | undefined>) : {});
  const baseUrl = env?.ANTHROPIC_BASE_URL;
  if (!baseUrl) {
    return "anthropic";
  }
  try {
    const host = new URL(baseUrl).hostname.toLowerCase();
    if (host === "anthropic.com" || host.endsWith(".anthropic.com")) {
      return "anthropic";
    }
  } catch {
    // An unparsable base URL is not the Anthropic host.
  }
  return "custom";
}

function isAbortLike(error: unknown, signal?: AbortSignalLike): boolean {
  if (signal?.aborted) {
    return true;
  }
  return (
    typeof error === "object" &&
    error !== null &&
    (error as { name?: unknown }).name === "AbortError"
  );
}

function errorParts(error: unknown): { type: string; message: string } {
  if (error instanceof Error) {
    return { type: error.name || error.constructor?.name || "Error", message: error.message };
  }
  return { type: typeof error, message: String(error) };
}

export class QueryTracer {
  private readonly tracer: Tracer;
  private readonly policy: ContentPolicy;
  private readonly params: QueryParamsLike;
  private readonly options: OptionsLike | undefined;
  private readonly parentContext: Context;
  private readonly startTimeMs: number;
  private readonly contextAttributes: Attributes;
  private readonly appSetSession: boolean;
  private readonly providerName: string;
  private readonly mcpServerNames = new Set<string>();

  private conversation?: Span;
  private conversationContext?: Context;
  private conversationIsError = false;
  private resultSeen = false;
  private sessionId?: string;
  private finished = false;
  private abortListener?: () => void;

  private readonly scopes = new Map<Scope, ScopeState>();
  private readonly tools = new Map<string, ToolState>();
  private readonly subagents = new Map<string, SubagentState>();
  /** task_id -> tool_use_id from task_started; task_updated carries only task_id. */
  private readonly taskToolUseIds = new Map<string, string>();

  /** How this query relates to an earlier session, for the usage baseline. */
  private readonly usageMode: "new" | "resume" | "fork" | "continue" | "continue-fork";
  private readonly resumeId?: string;
  /** undefined: no counted result yet; null: baseline unknown for this query. */
  private usagePrev?: UsageTotals | null;
  /** New spend counted in this query (sum of deltas), written to the promoted keys. */
  private readonly usageCounted: UsageTotals = {};

  constructor(args: {
    tracer: Tracer;
    policy: ContentPolicy;
    params: QueryParamsLike;
    parentContext: Context;
    startTimeMs?: number;
  }) {
    this.tracer = args.tracer;
    this.policy = args.policy;
    this.params = args.params ?? ({} as QueryParamsLike);
    this.options = this.params.options;
    this.parentContext = args.parentContext;
    this.startTimeMs = args.startTimeMs ?? clockMs();
    this.contextAttributes = safeContextAttributes(args.parentContext);
    this.appSetSession = this.contextAttributes[T.SESSION_ID] != null;
    this.providerName = resolveProviderName(this.options);
    for (const name of Object.keys(this.options?.mcpServers ?? {})) {
      this.mcpServerNames.add(name);
    }
    if (typeof this.options?.sessionId === "string" && this.options.sessionId) {
      this.sessionId = this.options.sessionId;
    } else if (typeof this.options?.resume === "string" && this.options.resume && !this.options.forkSession) {
      // A plain resume continues the same session id.
      this.sessionId = this.options.resume;
    }
    const resume = typeof this.options?.resume === "string" && this.options.resume ? this.options.resume : undefined;
    const fork = this.options?.forkSession === true;
    this.resumeId = resume;
    this.usageMode = resume
      ? fork
        ? "fork"
        : "resume"
      : this.options?.continue === true
        ? fork
          ? "continue-fork"
          : "continue"
        : "new";
  }

  get isFinished(): boolean {
    return this.finished;
  }

  /** Open the conversation span. Called on the first `next()`; idempotent. */
  start(): void {
    if (this.conversation || this.finished) {
      return;
    }
    try {
      const span = this.tracer.startSpan(
        SpanNames.CONVERSATION,
        { kind: SpanKind.CLIENT, startTime: toHrTime(this.startTimeMs) },
        this.parentContext,
      );
      this.conversation = span;
      this.conversationContext = trace.setSpan(this.parentContext, span);
      span.setAttributes(this.baseAttributes(ClaudeAgentSpanKind.CONVERSATION));
      span.setAttributes(this.conversationStartAttributes());
      this.attachAbortListener();
    } catch (error) {
      diag.debug(`@traceai/claude-agent-sdk: failed to start conversation span: ${error}`);
    }
  }

  /** Record one SDK message. Never throws and never mutates the message. */
  onMessage(message: unknown): void {
    if (this.finished || !this.conversation || !message || typeof message !== "object") {
      return;
    }
    try {
      const type = (message as { type?: unknown }).type;
      if (type === "assistant") {
        this.onAssistant(message as AssistantMessageLike);
      } else if (type === "user") {
        this.onUser(message as UserMessageLike);
      } else if (type === "result") {
        this.onResult(message as ResultMessageLike);
      } else if (type === "system") {
        this.onSystem(message as SystemMessageLike);
      }
    } catch (error) {
      diag.debug(`@traceai/claude-agent-sdk: failed to record message: ${error}`);
    }
  }

  /** End every open span for this query. Idempotent. */
  finish(outcome: FinishOutcome): void {
    if (this.finished) {
      return;
    }
    this.finished = true;
    this.detachAbortListener();
    if (!this.conversation) {
      return;
    }
    try {
      const signal = this.options?.abortController?.signal;
      const cancelled =
        outcome.kind === "aborted" ||
        (outcome.kind === "error" && isAbortLike(outcome.error, signal));
      const failed = outcome.kind === "error" && !cancelled;

      // One end time for everything closed here, so no child ends after its parent.
      const endMs = clockMs();
      const closeChild = (span: Span, notCompletedMessage: string) => {
        if (cancelled) {
          span.setAttribute(T.CANCELLED, true);
          span.setStatus({ code: SpanStatusCode.ERROR, message: "cancelled" });
        } else {
          span.setStatus({ code: SpanStatusCode.ERROR, message: notCompletedMessage });
        }
        span.end(toHrTime(endMs));
      };

      for (const sub of this.subagents.values()) {
        if (!sub.ended) {
          sub.ended = true;
          closeChild(sub.span, "Subagent span not completed");
        }
      }
      for (const [id, tool] of this.tools) {
        this.tools.delete(id);
        closeChild(tool.span, "Tool span not completed (conversation ended)");
      }
      for (const scope of this.scopes.values()) {
        const turn = scope.currentTurn;
        if (turn && !turn.ended) {
          if (cancelled || failed) {
            if (cancelled) turn.span.setAttribute(T.CANCELLED, true);
            turn.span.setStatus({
              code: SpanStatusCode.ERROR,
              message: cancelled ? "cancelled" : "query failed",
            });
            turn.ended = true;
            turn.span.end(toHrTime(endMs));
          } else {
            this.endTurn(turn, endMs);
          }
        }
      }

      const conversation = this.conversation;
      if (cancelled) {
        conversation.setAttribute(T.CANCELLED, true);
        conversation.setStatus({ code: SpanStatusCode.ERROR, message: "cancelled" });
      } else if (failed) {
        const { type, message } = errorParts((outcome as { error: unknown }).error);
        conversation.setAttribute(A.ERROR_TYPE, type);
        conversation.setAttribute(A.ERROR_MESSAGE, truncate(message, ERROR_MESSAGE_MAX));
        if ((outcome as { error: unknown }).error instanceof Error) {
          conversation.recordException((outcome as { error: Error }).error);
        }
        conversation.setStatus({ code: SpanStatusCode.ERROR, message: truncate(message, ERROR_MESSAGE_MAX) });
      } else if (!this.conversationIsError && (outcome.kind === "completed" || this.resultSeen)) {
        conversation.setStatus({ code: SpanStatusCode.OK });
      }
      conversation.end(toHrTime(endMs));
    } catch (error) {
      diag.debug(`@traceai/claude-agent-sdk: failed to finish spans: ${error}`);
    }
  }

  // --------------------------------------------------------------------------
  // Message handlers
  // --------------------------------------------------------------------------

  private onAssistant(message: AssistantMessageLike): void {
    const scope: Scope = message.parent_tool_use_id ?? null;
    const scopeState = this.scope(scope);
    const messageId = message.message?.id;

    let turn = scopeState.currentTurn;
    if (!turn || turn.ended || !messageId || turn.messageId !== messageId) {
      if (turn && !turn.ended) {
        this.endTurn(turn);
      }
      turn = this.startTurn(scope, scopeState, message);
    }

    const content = message.message?.content;
    const blocks = typeof content === "string" ? [{ type: "text", text: content }] : contentBlocks(content);
    for (const block of blocks) {
      if (!block || typeof block !== "object") continue;
      if (block.type === "text" && typeof block.text === "string") {
        turn.textParts.push(block.text);
      } else if (
        (block.type === "tool_use" || block.type === "server_tool_use" || block.type === "mcp_tool_use") &&
        typeof block.id === "string"
      ) {
        turn.textParts.push(`[Tool: ${block.name ?? "unknown"}]`);
        turn.toolUseCount += 1;
        this.startTool(block, scope, turn);
      } else if (
        typeof block.tool_use_id === "string" &&
        typeof block.type === "string" &&
        block.type.endsWith("tool_result")
      ) {
        // Server-side tool results arrive inside the assistant message.
        this.endTool(block.tool_use_id, block.content, block.is_error === true);
      }
    }

    turn.span.setAttribute(A.MESSAGE_HAS_TOOL_USE, turn.toolUseCount > 0);
    turn.span.setAttribute(A.MESSAGE_TOOL_USE_COUNT, turn.toolUseCount);
    if (!this.policy.hideOutputs && turn.textParts.length > 0) {
      const text = truncate(turn.textParts.join(" "), MESSAGE_CONTENT_MAX);
      turn.span.setAttribute(A.MESSAGE_CONTENT, text);
      turn.span.setAttribute(T.OUTPUT_VALUE, text);
      turn.span.setAttribute(T.OUTPUT_MIME_TYPE, "text/plain");
    }
    if (typeof message.error === "string" && message.error) {
      turn.isError = true;
      turn.span.setAttribute(A.ERROR_TYPE, message.error);
      turn.span.setStatus({ code: SpanStatusCode.ERROR, message: message.error });
    }
  }

  private onUser(message: UserMessageLike): void {
    const scope: Scope = message.parent_tool_use_id ?? null;
    const affectedTurns = new Set<TurnState>();
    for (const block of contentBlocks(message.message?.content)) {
      if (block && block.type === "tool_result" && typeof block.tool_use_id === "string") {
        const turn = this.endTool(block.tool_use_id, block.content, block.is_error === true);
        if (turn) affectedTurns.add(turn);
      }
    }
    // The model is called again after a user message: the next turn in this
    // scope starts now (Python TurnTracker.mark_next_start).
    this.scope(scope).nextTurnStartMs = clockMs();
    for (const turn of affectedTurns) {
      if (!turn.ended && turn.openToolIds.size === 0) {
        this.endTurn(turn);
      }
    }
  }

  private onResult(message: ResultMessageLike): void {
    const conversation = this.conversation!;
    this.resultSeen = true;

    const main = this.scopes.get(null)?.currentTurn;
    if (main && !main.ended) {
      this.endTurn(main);
    }
    // Streaming input: the next main-loop turn (if any) answers a user message
    // sent after this result, so it cannot start before now.
    this.scope(null).nextTurnStartMs = clockMs();

    if (typeof message.session_id === "string" && message.session_id) {
      this.setSessionId(message.session_id);
    }

    this.recordUsage(cumulativeUsage(message));
    if (typeof message.duration_ms === "number") {
      conversation.setAttribute(A.DURATION_MS, message.duration_ms);
    }
    if (typeof message.duration_api_ms === "number") {
      conversation.setAttribute(A.DURATION_API_MS, message.duration_api_ms);
    }
    if (typeof message.ttft_ms === "number") {
      conversation.setAttribute(A.TIME_TO_FIRST_TOKEN_MS, message.ttft_ms);
    }
    if (typeof message.num_turns === "number") {
      conversation.setAttribute(A.AGENT_NUM_TURNS, message.num_turns);
    }

    const subtype = typeof message.subtype === "string" ? message.subtype : undefined;
    const isError = message.is_error === true || (subtype !== undefined && subtype.startsWith("error"));
    conversation.setAttribute(A.IS_ERROR, isError);
    if (isError) {
      this.conversationIsError = true;
      const errorType = subtype ?? "error";
      conversation.setAttribute(A.ERROR_TYPE, errorType);
      if (Array.isArray(message.errors) && message.errors.length > 0) {
        conversation.setAttribute(
          A.ERROR_MESSAGE,
          truncate(message.errors.map(String).join("; "), ERROR_MESSAGE_MAX),
        );
      }
      conversation.setStatus({ code: SpanStatusCode.ERROR, message: errorType });
    }

    if (!this.policy.hideOutputs && typeof message.result === "string") {
      conversation.setAttribute(T.OUTPUT_VALUE, truncate(message.result, CONTENT_MAX));
      conversation.setAttribute(T.OUTPUT_MIME_TYPE, "text/plain");
    }
  }

  private onSystem(message: SystemMessageLike): void {
    const conversation = this.conversation!;
    switch (message.subtype) {
      case "init": {
        if (typeof message.session_id === "string" && message.session_id) {
          this.setSessionId(message.session_id);
        }
        if (typeof message.model === "string" && message.model) {
          conversation.setAttribute(A.AGENT_MODEL, message.model);
          conversation.setAttribute(T.GEN_AI_REQUEST_MODEL, message.model);
        }
        if (typeof message.permissionMode === "string" && message.permissionMode && !this.options?.permissionMode) {
          conversation.setAttribute(A.AGENT_PERMISSION_MODE, message.permissionMode);
        }
        if (Array.isArray(message.mcp_servers)) {
          for (const server of message.mcp_servers) {
            if (server && typeof server.name === "string") this.mcpServerNames.add(server.name);
          }
        }
        break;
      }
      case "task_started": {
        if (typeof message.task_id === "string" && typeof message.tool_use_id === "string") {
          this.taskToolUseIds.set(message.task_id, message.tool_use_id);
        }
        const sub = message.tool_use_id ? this.subagents.get(message.tool_use_id) : undefined;
        if (sub && !sub.ended) {
          if (typeof message.task_id === "string") {
            sub.span.setAttribute(T.SUBAGENT_TASK_ID, message.task_id);
          }
          if (message.is_backgrounded === true) {
            sub.backgrounded = true;
          }
        }
        break;
      }
      case "task_updated": {
        // A foreground task moved to the background (sdk.d.ts:6059). Its tool_result
        // becomes a "running in the background" placeholder; the subagent keeps
        // running until task_notification.
        const id = typeof message.task_id === "string" ? this.taskToolUseIds.get(message.task_id) : undefined;
        const sub = id ? this.subagents.get(id) : undefined;
        if (sub && !sub.ended && message.patch?.is_backgrounded === true) {
          sub.backgrounded = true;
        }
        break;
      }
      case "task_notification": {
        const id = message.tool_use_id;
        const sub = id ? this.subagents.get(id) : undefined;
        if (sub && !sub.ended && typeof message.status === "string") {
          sub.status = message.status;
          sub.span.setAttribute(T.SUBAGENT_STATUS, message.status);
          // A backgrounded subagent outlives its tool_result; it ends here.
          if (sub.backgrounded || sub.backgroundRequested || sub.toolEnded) {
            this.endSubagent(id!, sub.toolIsError);
          }
        }
        break;
      }
      default:
        break;
    }
  }

  // --------------------------------------------------------------------------
  // Span lifecycle
  // --------------------------------------------------------------------------

  private scope(scope: Scope): ScopeState {
    let state = this.scopes.get(scope);
    if (!state) {
      state = { turnCount: 0 };
      this.scopes.set(scope, state);
    }
    return state;
  }

  /** Context the children of `scope` are created in. */
  private scopeParentContext(scope: Scope): Context {
    if (scope !== null) {
      const parent = this.subagents.get(scope)?.span ?? this.tools.get(scope)?.span;
      if (parent) {
        return trace.setSpan(this.parentContext, parent);
      }
    }
    return this.conversationContext!;
  }

  private startTurn(scope: Scope, scopeState: ScopeState, message: AssistantMessageLike): TurnState {
    scopeState.turnCount += 1;
    const startTime =
      scopeState.nextTurnStartMs ??
      (scope === null ? this.startTimeMs : this.subagents.get(scope)?.startMs) ??
      clockMs();
    scopeState.nextTurnStartMs = undefined;

    const span = this.tracer.startSpan(
      SpanNames.ASSISTANT_TURN,
      { kind: SpanKind.INTERNAL, startTime: toHrTime(startTime) },
      this.scopeParentContext(scope),
    );
    span.setAttributes(this.baseAttributes(ClaudeAgentSpanKind.ASSISTANT_TURN));
    span.setAttribute(A.AGENT_NUM_TURNS, scopeState.turnCount);
    span.setAttribute(A.MESSAGE_TYPE, "assistant");
    span.setAttribute(A.MESSAGE_ROLE, "assistant");
    span.setAttribute(T.GEN_AI_PROVIDER_NAME, this.providerName);
    const model = message.message?.model;
    if (typeof model === "string" && model) {
      span.setAttribute(A.AGENT_MODEL, model);
      span.setAttribute(T.GEN_AI_REQUEST_MODEL, model);
    }
    if (scope !== null) {
      span.setAttribute(A.PARENT_TOOL_USE_ID, scope);
    }

    const turn: TurnState = {
      span,
      scope,
      messageId: message.message?.id,
      textParts: [],
      toolUseCount: 0,
      openToolIds: new Set(),
      isError: false,
      ended: false,
    };
    scopeState.currentTurn = turn;
    return turn;
  }

  private endTurn(turn: TurnState, endMs: number = clockMs()): void {
    if (turn.ended) return;
    turn.ended = true;
    if (!turn.isError) {
      turn.span.setStatus({ code: SpanStatusCode.OK });
    }
    turn.span.end(toHrTime(endMs));
    const scopeState = this.scopes.get(turn.scope);
    if (scopeState?.currentTurn === turn) {
      scopeState.currentTurn = undefined;
    }
  }

  private startTool(block: ContentBlockLike, scope: Scope, turn: TurnState): void {
    const id = block.id as string;
    if (this.tools.has(id)) {
      return;
    }
    const name = typeof block.name === "string" && block.name ? block.name : "unknown_tool";
    const mcp =
      block.type === "mcp_tool_use" && typeof block.server_name === "string"
        ? { server: block.server_name, tool: name }
        : parseMcpToolName(name, this.mcpServerNames);
    const source = mcp ? "mcp" : getToolSource(name, this.mcpServerNames);
    const kind = mcp ? ClaudeAgentSpanKind.MCP_TOOL : ClaudeAgentSpanKind.TOOL_EXECUTION;

    const startMs = clockMs();
    const span = this.tracer.startSpan(
      SpanNames.toolSpan(name),
      { kind: SpanKind.INTERNAL, startTime: toHrTime(startMs) },
      trace.setSpan(this.parentContext, turn.span),
    );
    span.setAttributes(this.baseAttributes(kind));
    span.setAttribute(A.GEN_AI_TOOL_NAME, name);
    span.setAttribute(T.GEN_AI_TOOL_NAME, name);
    span.setAttribute(A.TOOL_USE_ID, id);
    span.setAttribute(A.TOOL_SOURCE, source);
    if (mcp) {
      span.setAttribute(A.MCP_SERVER_NAME, mcp.server);
      span.setAttribute(T.MCP_TOOL_NAME, mcp.tool);
    }
    if (scope !== null) {
      span.setAttribute(A.PARENT_TOOL_USE_ID, scope);
    }
    const input = block.input;
    if (!this.policy.hideInputs && input !== undefined && input !== null) {
      const serialized = safeJson(input);
      span.setAttribute(A.TOOL_INPUT, serialized);
      span.setAttribute(T.INPUT_VALUE, serialized);
      span.setAttribute(T.INPUT_MIME_TYPE, typeof input === "string" ? "text/plain" : "application/json");
      setToolSpecificAttributes(span, name, input);
    }

    const tool: ToolState = { span, name, startMs, turn };
    this.tools.set(id, tool);
    turn.openToolIds.add(id);

    if (!mcp && SUBAGENT_TOOLS.has(name)) {
      this.startSubagent(id, input, scope, span);
    }
  }

  /** End a tool span. Returns the turn that issued the tool, if any. */
  private endTool(id: string, content: unknown, isError: boolean): TurnState | undefined {
    const tool = this.tools.get(id);
    if (!tool) {
      return undefined;
    }
    this.tools.delete(id);

    const sub = this.subagents.get(id);
    if (sub && !sub.ended) {
      sub.toolEnded = true;
      sub.toolIsError = isError;
      if (!sub.backgrounded && !sub.backgroundRequested) {
        this.endSubagent(id, isError);
      }
    }

    const span = tool.span;
    const endMs = clockMs();
    span.setAttribute(A.TOOL_DURATION_MS, endMs - tool.startMs);
    span.setAttribute(A.TOOL_IS_ERROR, isError);
    const output = flattenToolResult(content);
    if (!this.policy.hideOutputs) {
      const serialized = truncate(output, CONTENT_MAX);
      span.setAttribute(A.TOOL_OUTPUT, serialized);
      span.setAttribute(T.OUTPUT_VALUE, serialized);
      span.setAttribute(T.OUTPUT_MIME_TYPE, "text/plain");
    }
    if (isError) {
      const message = this.policy.hideOutputs ? "tool error" : truncate(output, ERROR_MESSAGE_MAX);
      if (!this.policy.hideOutputs) {
        span.setAttribute(A.TOOL_ERROR_MESSAGE, message);
      }
      span.setStatus({ code: SpanStatusCode.ERROR, message });
    } else {
      span.setStatus({ code: SpanStatusCode.OK });
    }
    span.end(toHrTime(endMs));

    tool.turn?.openToolIds.delete(id);
    return tool.turn;
  }

  private startSubagent(toolUseId: string, input: unknown, scope: Scope, toolSpan: Span): void {
    const fields = (input && typeof input === "object" ? input : {}) as Record<string, unknown>;
    const subagentType =
      typeof fields.subagent_type === "string" && fields.subagent_type ? fields.subagent_type : "unknown";
    const startMs = clockMs();
    const span = this.tracer.startSpan(
      SpanNames.subagentSpan(subagentType),
      { kind: SpanKind.INTERNAL, startTime: toHrTime(startMs) },
      trace.setSpan(this.parentContext, toolSpan),
    );
    span.setAttributes(this.baseAttributes(ClaudeAgentSpanKind.SUBAGENT));
    span.setAttribute(A.TOOL_USE_ID, toolUseId);
    span.setAttribute(A.SUBAGENT_TYPE, subagentType);
    if (typeof fields.name === "string" && fields.name) {
      span.setAttribute(A.SUBAGENT_NAME, fields.name);
    }
    if (typeof fields.model === "string" && fields.model) {
      span.setAttribute(A.AGENT_MODEL, fields.model);
    }
    if (scope !== null) {
      span.setAttribute(A.PARENT_TOOL_USE_ID, scope);
    }
    if (!this.policy.hideInputs) {
      if (typeof fields.description === "string" && fields.description) {
        span.setAttribute(A.SUBAGENT_DESCRIPTION, truncate(fields.description, 500));
      }
      if (typeof fields.prompt === "string" && fields.prompt) {
        span.setAttribute(A.SUBAGENT_PROMPT, truncate(fields.prompt, PROMPT_MAX));
        span.setAttribute(T.INPUT_VALUE, truncate(fields.prompt, PROMPT_MAX));
        span.setAttribute(T.INPUT_MIME_TYPE, "text/plain");
      }
    }
    this.subagents.set(toolUseId, {
      span,
      startMs,
      backgrounded: fields.run_in_background === true,
      backgroundRequested: false,
      toolEnded: false,
      toolIsError: false,
      ended: false,
    });
  }

  /**
   * The app called `Query.backgroundTasks(toolUseId?)` (sdk.d.ts:3234). Mark the
   * matching foreground subagents as background now: their "running in the
   * background" tool_result may arrive before any task_updated. Returns the ids
   * marked, for `cancelBackgroundRequest` if the call fails.
   */
  markBackgroundRequested(toolUseId?: string): string[] {
    const marked: string[] = [];
    if (this.finished) return marked;
    try {
      for (const [id, sub] of this.subagents) {
        if (sub.ended || sub.toolEnded || sub.backgrounded || sub.backgroundRequested) continue;
        if (toolUseId !== undefined && id !== toolUseId) continue;
        sub.backgroundRequested = true;
        marked.push(id);
      }
    } catch (error) {
      diag.debug(`@traceai/claude-agent-sdk: failed to record backgroundTasks(): ${error}`);
    }
    return marked;
  }

  /** `backgroundTasks()` rejected or matched nothing: those subagents stay foreground. */
  cancelBackgroundRequest(ids: string[]): void {
    if (this.finished) return;
    try {
      for (const id of ids) {
        const sub = this.subagents.get(id);
        if (!sub || sub.ended || !sub.backgroundRequested) continue;
        sub.backgroundRequested = false;
        if (sub.toolEnded && !sub.backgrounded) {
          this.endSubagent(id, sub.toolIsError);
        }
      }
    } catch (error) {
      diag.debug(`@traceai/claude-agent-sdk: failed to record backgroundTasks() failure: ${error}`);
    }
  }

  private endSubagent(toolUseId: string, isError: boolean): void {
    const sub = this.subagents.get(toolUseId);
    if (!sub || sub.ended) return;
    // Close the subagent's own open turn first so no child outlives it.
    const scopeState = this.scopes.get(toolUseId);
    if (scopeState?.currentTurn && !scopeState.currentTurn.ended) {
      this.endTurn(scopeState.currentTurn);
    }
    sub.ended = true;
    // task_notification status failed / stopped is an error even when the
    // tool_result itself was not.
    const failed = isError || (sub.status !== undefined && sub.status !== "completed");
    const endMs = clockMs();
    sub.span.setAttribute(A.TOOL_DURATION_MS, endMs - sub.startMs);
    sub.span.setAttribute(A.TOOL_IS_ERROR, failed);
    sub.span.setStatus(
      failed
        ? { code: SpanStatusCode.ERROR, message: sub.status ? `subagent ${sub.status}` : "subagent error" }
        : { code: SpanStatusCode.OK },
    );
    sub.span.end(toHrTime(endMs));
  }

  // --------------------------------------------------------------------------
  // Attributes
  // --------------------------------------------------------------------------

  private baseAttributes(kind: ClaudeAgentSpanKindValue): Attributes {
    const fiKind = FI_SPAN_KIND_BY_CLAUDE_KIND[kind];
    const attributes: Attributes = {
      ...this.contextAttributes,
      [A.SPAN_KIND]: kind,
      [T.GEN_AI_SPAN_KIND]: fiKind,
      [T.FI_SPAN_KIND]: fiKind,
    };
    Object.assign(attributes, this.sessionAttributes());
    return attributes;
  }

  private sessionAttributes(): Attributes {
    if (!this.sessionId) return {};
    const attributes: Attributes = {
      [A.GEN_AI_CONVERSATION_ID]: this.sessionId,
      [A.AGENT_SESSION_ID]: this.sessionId,
    };
    if (!this.appSetSession) {
      attributes[T.SESSION_ID] = this.sessionId;
    }
    return attributes;
  }

  private setSessionId(sessionId: string): void {
    if (this.sessionId === sessionId) return;
    this.sessionId = sessionId;
    this.conversation?.setAttributes(this.sessionAttributes());
  }

  /**
   * Record one result's running totals. The collector promotes gen_ai.usage.*
   * and gen_ai.cost.total on any span and Observe sums them per trace and per
   * session, so the promoted keys carry only the spend new since the session's
   * last known totals. Without a known baseline nothing is promoted.
   */
  private recordUsage(cumulative: UsageTotals | undefined): void {
    if (!cumulative) return;
    const conversation = this.conversation!;
    const store = sessionUsageStore();

    const first = this.usagePrev === undefined;
    if (first) {
      this.usagePrev = this.resolveUsageBaseline();
    }
    let prev = this.usagePrev;
    if (prev) {
      const dropped = USAGE_FIELDS.some(
        (f) => cumulative[f] !== undefined && prev![f] !== undefined && cumulative[f]! < prev![f]!,
      );
      if (dropped && first) {
        // Below the saved baseline on the first result (e.g. resumeSessionAt an
        // earlier message): the earlier share is unknown.
        prev = null;
      } else {
        // Within one query a drop is a /clear: the running total restarted at 0.
        for (const f of USAGE_FIELDS) {
          const value = cumulative[f];
          const base = dropped ? 0 : prev[f];
          if (value === undefined || base === undefined) continue;
          this.usageCounted[f] = (this.usageCounted[f] ?? 0) + (value - base);
        }
        prev = { ...prev, ...definedUsage(cumulative) };
      }
      this.usagePrev = prev;
    }
    if (this.sessionId) {
      const stored = prev ?? { ...(store.get(this.sessionId) ?? {}), ...definedUsage(cumulative) };
      store.set(this.sessionId, stored);
    }

    const setIf = (key: string, value: number | undefined) => {
      if (value !== undefined) conversation.setAttribute(key, value);
    };
    setIf(T.CUMULATIVE_COST_USD, cumulative.costUsd);
    setIf(T.CUMULATIVE_INPUT_TOKENS, cumulative.inputTokens);
    setIf(T.CUMULATIVE_OUTPUT_TOKENS, cumulative.outputTokens);
    setIf(T.CUMULATIVE_CACHE_READ_TOKENS, cumulative.cacheReadTokens);
    setIf(T.CUMULATIVE_CACHE_CREATION_TOKENS, cumulative.cacheCreationTokens);
    conversation.setAttribute(T.USAGE_BASELINE_UNKNOWN, prev === null);
    if (prev === null) return;

    const counted = this.usageCounted;
    setIf(A.USAGE_INPUT_TOKENS, counted.inputTokens);
    setIf(A.USAGE_OUTPUT_TOKENS, counted.outputTokens);
    if (counted.inputTokens !== undefined || counted.outputTokens !== undefined) {
      conversation.setAttribute(A.USAGE_TOTAL_TOKENS, (counted.inputTokens ?? 0) + (counted.outputTokens ?? 0));
    }
    setIf(A.USAGE_CACHE_READ_TOKENS, counted.cacheReadTokens);
    setIf(A.USAGE_CACHE_CREATION_TOKENS, counted.cacheCreationTokens);
    setIf(A.COST_TOTAL_USD, counted.costUsd);
    setIf(T.GEN_AI_COST_TOTAL, counted.costUsd);
  }

  /** The session totals this query starts from, or null when they are not known here. */
  private resolveUsageBaseline(): UsageTotals | null {
    const store = sessionUsageStore();
    switch (this.usageMode) {
      case "new":
        return { costUsd: 0, inputTokens: 0, outputTokens: 0, cacheReadTokens: 0, cacheCreationTokens: 0 };
      case "resume":
      case "fork":
        // A fork starts from the parent's saved totals under a new session id.
        return (this.resumeId && store.get(this.resumeId)) || null;
      case "continue":
        return (this.sessionId && store.get(this.sessionId)) || null;
      case "continue-fork":
      default:
        // The parent is "the most recent session in cwd": not identifiable here.
        return null;
    }
  }

  private conversationStartAttributes(): Attributes {
    const options = this.options ?? {};
    const attributes: Record<string, AttributeValue> = {
      [T.GEN_AI_PROVIDER_NAME]: this.providerName,
    };
    if (typeof options.model === "string" && options.model) {
      attributes[A.AGENT_MODEL] = options.model;
      attributes[T.GEN_AI_REQUEST_MODEL] = options.model;
    }
    if (typeof options.permissionMode === "string" && options.permissionMode) {
      attributes[A.AGENT_PERMISSION_MODE] = options.permissionMode;
    }
    if (Array.isArray(options.allowedTools) && options.allowedTools.length > 0) {
      attributes[A.AGENT_ALLOWED_TOOLS] = safeJson(options.allowedTools);
    }

    const resume = typeof options.resume === "string" && options.resume ? options.resume : undefined;
    // options.continue (sdk.d.ts:1594) resumes the most recent session in cwd.
    const continuing = resume !== undefined || options.continue === true;
    const fork = continuing && options.forkSession === true;
    if (continuing) {
      attributes[A.AGENT_IS_RESUMED] = true;
    }
    if (resume) {
      attributes[A.AGENT_RESUME_SESSION_ID] = resume;
    }
    attributes[A.SESSION_IS_RESUMED] = continuing && !fork;
    attributes[A.SESSION_IS_NEW] = !continuing || fork;
    if (resume && !fork) {
      attributes[A.SESSION_PREVIOUS_ID] = resume;
    }
    if (fork && resume) {
      attributes[A.SESSION_FORK_FROM] = resume;
    }

    if (!this.policy.hideInputs) {
      if (typeof this.params.prompt === "string") {
        const prompt = truncate(this.params.prompt, PROMPT_MAX);
        attributes[A.AGENT_PROMPT] = prompt;
        attributes[T.INPUT_VALUE] = prompt;
        attributes[T.INPUT_MIME_TYPE] = "text/plain";
      }
      const systemPrompt = options.systemPrompt;
      if (typeof systemPrompt === "string" && systemPrompt) {
        attributes[A.AGENT_SYSTEM_PROMPT] = truncate(systemPrompt, SYSTEM_PROMPT_MAX);
      } else if (systemPrompt && typeof systemPrompt === "object") {
        attributes[A.AGENT_SYSTEM_PROMPT] = safeJson(systemPrompt, SYSTEM_PROMPT_MAX);
      }
    }
    return attributes;
  }

  // --------------------------------------------------------------------------
  // Abort
  // --------------------------------------------------------------------------

  private attachAbortListener(): void {
    const signal = this.options?.abortController?.signal;
    if (!signal || typeof signal.addEventListener !== "function") return;
    if (signal.aborted) {
      this.finish({ kind: "aborted", reason: signal.reason });
      return;
    }
    const listener = () => this.finish({ kind: "aborted", reason: signal.reason });
    this.abortListener = listener;
    signal.addEventListener("abort", listener);
  }

  private detachAbortListener(): void {
    const signal = this.options?.abortController?.signal;
    if (this.abortListener && signal && typeof signal.removeEventListener === "function") {
      signal.removeEventListener("abort", this.abortListener);
    }
    this.abortListener = undefined;
  }
}

function safeContextAttributes(ctx: Context): Attributes {
  try {
    return getAttributesFromContext(ctx);
  } catch {
    return {};
  }
}

function finiteNumber(value: unknown): number | undefined {
  return typeof value === "number" && Number.isFinite(value) ? value : undefined;
}

function definedUsage(totals: UsageTotals): UsageTotals {
  const out: UsageTotals = {};
  for (const f of USAGE_FIELDS) {
    if (totals[f] !== undefined) out[f] = totals[f];
  }
  return out;
}

/**
 * Running totals from a result: cost from `total_cost_usd`, tokens from
 * `modelUsage` summed over models (sdk.d.ts:5679, 5687). `result.usage` is not
 * read: it is main-loop only and per turn (sdk.d.ts:5683). Returns undefined
 * when the result carries nothing, or only zeros (a crash/startup-error result).
 */
function cumulativeUsage(message: ResultMessageLike): UsageTotals | undefined {
  const totals: UsageTotals = {};
  const cost = finiteNumber(message.total_cost_usd);
  if (cost !== undefined) totals.costUsd = cost;
  const models = message.modelUsage;
  if (models && typeof models === "object") {
    let seen = false;
    let input = 0;
    let output = 0;
    let cacheRead = 0;
    let cacheCreation = 0;
    for (const entry of Object.values(models)) {
      if (!entry || typeof entry !== "object") continue;
      seen = true;
      input += finiteNumber(entry.inputTokens) ?? 0;
      output += finiteNumber(entry.outputTokens) ?? 0;
      cacheRead += finiteNumber(entry.cacheReadInputTokens) ?? 0;
      cacheCreation += finiteNumber(entry.cacheCreationInputTokens) ?? 0;
    }
    if (seen) {
      totals.inputTokens = input;
      totals.outputTokens = output;
      totals.cacheReadTokens = cacheRead;
      totals.cacheCreationTokens = cacheCreation;
    }
  }
  const values = USAGE_FIELDS.map((f) => totals[f]).filter((v): v is number => v !== undefined);
  return values.some((v) => v !== 0) ? totals : undefined;
}

/** Python `_set_tool_specific_attributes`. Only called when inputs are visible. */
function setToolSpecificAttributes(span: Span, toolName: string, input: unknown): void {
  if (!input || typeof input !== "object") return;
  const fields = input as Record<string, unknown>;
  const str = (key: string) => (typeof fields[key] === "string" ? (fields[key] as string) : undefined);
  switch (toolName) {
    case "Read":
    case "Write":
    case "Edit": {
      const filePath = str("file_path");
      if (filePath) span.setAttribute(A.TOOL_FILE_PATH, filePath);
      break;
    }
    case "Bash": {
      const command = str("command");
      if (command) span.setAttribute(A.TOOL_COMMAND, truncate(command, TOOL_COMMAND_MAX));
      break;
    }
    case "Glob":
    case "Grep": {
      const pattern = str("pattern");
      if (pattern) span.setAttribute(A.TOOL_PATTERN, pattern);
      break;
    }
    case "WebSearch": {
      const query = str("query");
      if (query) span.setAttribute(A.TOOL_SEARCH_QUERY, query);
      break;
    }
    case "WebFetch": {
      const url = str("url");
      if (url) span.setAttribute(A.TOOL_URL, url);
      break;
    }
    default:
      break;
  }
}
