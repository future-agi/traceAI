/**
 * Span model for one Codex turn (one `Thread.run()` or `Thread.runStreamed()`).
 *
 * The SDK yields `ThreadEvent`s, not spans. `TurnTracer` turns them into:
 *
 *   codex.turn                     (AGENT)   session.id = thread id, usage
 *   ├─ codex.reasoning             (CHAIN)
 *   ├─ codex.command_execution     (TOOL)
 *   ├─ codex.file_change           (TOOL)
 *   ├─ codex.mcp_tool_call         (TOOL)
 *   ├─ codex.web_search            (TOOL)
 *   ├─ codex.todo_list             (CHAIN)
 *   ├─ codex.agent_message         (LLM)
 *   └─ codex.error                 (CHAIN, codex.warning=true)   non-fatal error item
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
import { MimeType } from "@traceai/fi-semantic-conventions";
import {
  AGENT_NAME,
  CodexAttributes as C,
  ITEM_SPANS,
  PROVIDER_NAME,
  SpanNames,
  StandardAttributes as S,
  UNKNOWN_ITEM_SPAN,
} from "./attributes";
import { InputLike, ThreadEventLike, ThreadItemLike, ThreadOptionsLike, TurnOptionsLike, UsageLike } from "./types";

/** Which content the wrapper may copy onto spans. Both default to hidden. */
export interface ContentPolicy {
  hideInputs: boolean;
  hideOutputs: boolean;
  /** Error details remain hidden even when ordinary outputs are visible. */
  hideErrorDetails: boolean;
}

/** How the wrapped event stream stopped. */
export type FinishOutcome =
  | { kind: "completed" }
  | { kind: "returned" }
  | { kind: "error"; error: unknown };

const CONTENT_MAX = 5000;
const ERROR_MESSAGE_MAX = 500;
const LOG = "@traceai/codex-sdk";

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
  const seconds = Math.floor(ms / 1000);
  const nanos = Math.round((ms - seconds * 1000) * 1e6);
  return nanos >= 1e9 ? [seconds + 1, nanos - 1e9] : [seconds, nanos];
}

export function truncate(value: string, max: number): string {
  return value.length > max ? `${value.slice(0, max)}...[truncated]` : value;
}

export function safeJson(value: unknown, max = CONTENT_MAX): string {
  try {
    const text = typeof value === "string" ? value : JSON.stringify(value);
    return truncate(text ?? String(value), max);
  } catch {
    return truncate(String(value), max);
  }
}

/** Same normalization as the SDK's `normalizeInput`: text parts joined by a blank line. */
export function normalizeInput(input: InputLike | undefined): { prompt: string; images: number } {
  if (typeof input === "string") {
    return { prompt: input, images: 0 };
  }
  const parts: string[] = [];
  let images = 0;
  if (Array.isArray(input)) {
    for (const part of input) {
      if (part && part.type === "text" && typeof part.text === "string") parts.push(part.text);
      else if (part && part.type === "local_image") images += 1;
    }
  }
  return { prompt: parts.join("\n\n"), images };
}

function isAbortLike(error: unknown, signal?: TurnOptionsLike["signal"]): boolean {
  if (signal?.aborted) return true;
  const name = (error as { name?: unknown } | null)?.name;
  return name === "AbortError";
}

function errorMessage(error: unknown): string {
  if (error instanceof Error) return error.message;
  return typeof error === "string" ? error : safeJson(error, ERROR_MESSAGE_MAX);
}

function errorName(error: unknown): string {
  const name = (error as { name?: unknown } | null)?.name;
  return typeof name === "string" && name ? name : "Error";
}

/**
 * The SDK builds its thrown messages from raw data: `Codex Exec exited with
 * code 1: <the binary's stderr>` and `Failed to parse item: <the raw line>`
 * (dist/index.js of 0.162.1). Stderr can hold config lines, local paths and
 * the user name; the raw line can hold the prompt or command output. With
 * outputs hidden only this fixed summary is exported.
 */
export function errorSummary(error: unknown): string {
  const message = error instanceof Error ? error.message : typeof error === "string" ? error : "";
  const exited = /^Codex Exec exited with (code -?\d+|signal [A-Z0-9]+)/.exec(message);
  if (exited) return `Codex Exec exited with ${exited[1]}`;
  if (message.startsWith("Failed to parse item")) return "Failed to parse item";
  return errorName(error);
}

function finite(value: unknown): number | undefined {
  return typeof value === "number" && Number.isFinite(value) ? value : undefined;
}

type UsageTotals = {
  input_tokens?: number;
  cached_input_tokens?: number;
  cache_write_input_tokens?: number;
  output_tokens?: number;
  reasoning_output_tokens?: number;
};

const USAGE_KEYS = [
  "input_tokens",
  "cached_input_tokens",
  "cache_write_input_tokens",
  "output_tokens",
  "reasoning_output_tokens",
] as const;

function usageTotals(usage: UsageLike): UsageTotals {
  const totals: UsageTotals = {};
  for (const key of USAGE_KEYS) {
    const value = finite(usage[key]);
    if (value !== undefined) totals[key] = value;
  }
  return totals;
}

function maxUsage(a: UsageTotals, b: UsageTotals): UsageTotals {
  const out: UsageTotals = {};
  for (const key of USAGE_KEYS) {
    const value = Math.max(a[key] ?? 0, b[key] ?? 0);
    if (a[key] !== undefined || b[key] !== undefined) out[key] = value;
  }
  return out;
}

/** `current - previous` per key, or `undefined` when any key went down. */
function subtractUsage(current: UsageTotals, previous: UsageTotals): UsageTotals | undefined {
  const delta: UsageTotals = {};
  for (const key of USAGE_KEYS) {
    const value = current[key];
    if (value === undefined) continue;
    const diff = value - (previous[key] ?? 0);
    if (diff < 0) return undefined;
    delta[key] = diff;
  }
  return delta;
}

const MAX_TRACKED_THREADS = 1000;

/**
 * Last running total seen per thread id in this process (LRU, 1000 threads).
 * Exact while a thread's turns run in one process; see the README.
 */
class UsageBaselines {
  private readonly totals = new Map<string, UsageTotals>();

  get(threadId: string): UsageTotals | undefined {
    return this.totals.get(threadId);
  }

  set(threadId: string, totals: UsageTotals): void {
    this.totals.delete(threadId);
    this.totals.set(threadId, totals);
    while (this.totals.size > MAX_TRACKED_THREADS) {
      const oldest = this.totals.keys().next().value;
      if (oldest === undefined) break;
      this.totals.delete(oldest);
    }
  }

  clear(): void {
    this.totals.clear();
  }

  get size(): number {
    return this.totals.size;
  }
}

export const usageBaselines = new UsageBaselines();

interface ItemState {
  span: Span;
  type: string;
  last: ThreadItemLike;
}

export class TurnTracer {
  private readonly tracer: Tracer;
  private readonly policy: ContentPolicy;
  private readonly parentContext: Context;
  private readonly startTimeMs: number;
  private readonly input: InputLike | undefined;
  private readonly turnOptions: TurnOptionsLike | undefined;
  private readonly threadOptions: ThreadOptionsLike | undefined;
  private readonly contextAttributes: Attributes;
  private readonly appSetSession: boolean;
  private readonly resumed: boolean;

  private turn?: Span;
  private turnContext?: Context;
  private finished = false;
  private threadId?: string;
  private lastEventMs: number;
  private finalResponse?: string;
  private completed = false;
  /** `turn.failed` text; the turn failed. */
  private failedMessage?: string;
  /** Last `error` event text. Fatal only if the turn ends without `turn.completed`. */
  private streamErrorMessage?: string;
  private streamErrorCount = 0;
  private readonly items = new Map<string, ItemState>();
  private anonymousItems = 0;

  constructor(args: {
    tracer: Tracer;
    policy: ContentPolicy;
    parentContext: Context;
    input: InputLike | undefined;
    turnOptions?: TurnOptionsLike;
    threadOptions?: ThreadOptionsLike;
    threadId?: string | null;
    startTimeMs?: number;
  }) {
    this.tracer = args.tracer;
    this.policy = args.policy;
    this.parentContext = args.parentContext;
    this.input = args.input;
    this.turnOptions = args.turnOptions;
    this.threadOptions = args.threadOptions;
    this.startTimeMs = args.startTimeMs ?? clockMs();
    this.lastEventMs = this.startTimeMs;
    this.contextAttributes = safeContextAttributes(args.parentContext);
    this.appSetSession = this.contextAttributes[S.SESSION_ID] != null;
    // A thread with an id before its first event was resumed (resumeThread or a second run()).
    this.resumed = typeof args.threadId === "string" && args.threadId.length > 0;
    if (this.resumed) this.threadId = args.threadId as string;
  }

  private get hideErrorText(): boolean {
    return this.policy.hideOutputs || this.policy.hideErrorDetails;
  }

  get isFinished(): boolean {
    return this.finished;
  }

  /** Open the turn span. Called on the first `next()`; idempotent. */
  start(): void {
    if (this.turn || this.finished) return;
    try {
      const span = this.tracer.startSpan(
        SpanNames.TURN,
        { kind: SpanKind.INTERNAL, startTime: toHrTime(this.startTimeMs) },
        this.parentContext,
      );
      this.turn = span;
      this.turnContext = trace.setSpan(this.parentContext, span);
      span.setAttributes(this.contextAttributes);
      span.setAttributes(this.turnStartAttributes());
    } catch (error) {
      diag.debug(`${LOG}: failed to start turn span: ${error}`);
    }
  }

  /** Record one SDK event. Never throws and never mutates the event. */
  onEvent(event: unknown): void {
    if (this.finished || !this.turn || !event || typeof event !== "object") return;
    try {
      const nowMs = clockMs();
      const e = event as ThreadEventLike;
      switch (e.type) {
        case "thread.started":
          if (typeof e.thread_id === "string" && e.thread_id) this.setThreadId(e.thread_id);
          break;
        case "turn.completed":
          this.completed = true;
          this.setUsage(e.usage);
          break;
        case "turn.failed":
          this.failedMessage = typeof e.error?.message === "string" ? e.error.message : "turn failed";
          break;
        case "error":
          // Codex also sends `error` for errors it retries ("Reconnecting... 1/5"),
          // and the turn can still complete.
          this.streamErrorCount += 1;
          this.streamErrorMessage = typeof e.message === "string" ? e.message : "stream error";
          this.turn.addEvent(
            "codex.stream_error",
            this.hideErrorText ? {} : { message: truncate(this.streamErrorMessage, ERROR_MESSAGE_MAX) },
          );
          break;
        case "item.started":
          this.onItemStarted(e.item, nowMs);
          break;
        case "item.updated":
          this.onItemUpdated(e.item);
          break;
        case "item.completed":
          this.onItemCompleted(e.item, nowMs);
          break;
        default:
          break;
      }
      this.lastEventMs = nowMs;
    } catch (error) {
      diag.debug(`${LOG}: failed to record event: ${error}`);
    }
  }

  /**
   * End every open span for this turn, the turn span last. Idempotent.
   * The spans always end, even if recording their attributes throws: item
   * spans that already ended were exported with the turn as their parent.
   */
  finish(outcome: FinishOutcome): void {
    if (this.finished) return;
    this.finished = true;
    const turn = this.turn;
    if (!turn) return;
    const endMs = clockMs();
    try {
      this.recordFinish(turn, outcome, endMs);
    } catch (error) {
      diag.debug(`${LOG}: failed to finish turn: ${error}`);
    } finally {
      for (const [id, state] of this.items) {
        this.items.delete(id);
        endSpan(state.span, endMs);
      }
      endSpan(turn, endMs);
    }
  }

  /** Statuses and attributes for `finish()`; closes open item spans. */
  private recordFinish(turn: Span, outcome: FinishOutcome, endMs: number): void {
    // A stream error the turn recovered from (turn.completed followed) is not a failure.
    const failedText =
      this.failedMessage ?? (!this.completed && this.streamErrorMessage !== undefined ? this.streamErrorMessage : undefined);
    const failedLabel = this.failedMessage !== undefined ? "turn failed" : "stream error";
    const cancelled =
      (outcome.kind === "returned" && !this.completed && failedText === undefined) ||
      (outcome.kind === "error" && isAbortLike(outcome.error, this.turnOptions?.signal));
    const errored = outcome.kind === "error" && !cancelled;
    if (this.streamErrorCount > 0) turn.setAttribute(C.STREAM_ERROR_COUNT, this.streamErrorCount);

    for (const [id, state] of this.items) {
      this.items.delete(id);
      try {
        if (cancelled) {
          state.span.setAttribute(C.CANCELLED, true);
          state.span.setStatus({ code: SpanStatusCode.ERROR, message: "cancelled" });
        } else if (errored || failedText !== undefined) {
          state.span.setStatus({ code: SpanStatusCode.ERROR, message: "item not completed (turn failed)" });
        } else {
          // todo_list completes with the turn; anything else still open is incomplete, not failed.
          if (state.type !== "todo_list") state.span.setAttribute(C.ITEM_INCOMPLETE, true);
          this.applyItemAttributes(state.span, state.type, state.last);
        }
      } catch (error) {
        diag.debug(`${LOG}: failed to record open item: ${error}`);
      } finally {
        endSpan(state.span, endMs);
      }
    }

    if (this.finalResponse !== undefined && !this.policy.hideOutputs) {
      turn.setAttribute(S.OUTPUT_VALUE, truncate(this.finalResponse, CONTENT_MAX));
      turn.setAttribute(S.OUTPUT_MIME_TYPE, MimeType.TEXT);
    }

    if (cancelled) {
      turn.setAttribute(C.CANCELLED, true);
      turn.setStatus({ code: SpanStatusCode.ERROR, message: "cancelled" });
    } else if (errored) {
      const error = (outcome as { error: unknown }).error;
      turn.setAttribute(C.ERROR_TYPE, errorName(error));
      if (this.policy.hideErrorDetails) {
        turn.recordException({ name: errorName(error) });
        turn.setStatus({ code: SpanStatusCode.ERROR });
      } else if (this.policy.hideOutputs) {
        // Error text is content (stderr, raw lines, paths): a fixed summary, no stack trace.
        const summary = errorSummary(error);
        turn.setAttribute(C.ERROR_MESSAGE, summary);
        turn.recordException({ name: errorName(error), message: summary });
        turn.setStatus({ code: SpanStatusCode.ERROR, message: summary });
      } else {
        const message = truncate(errorMessage(error), ERROR_MESSAGE_MAX);
        turn.setAttribute(C.ERROR_MESSAGE, message);
        if (error instanceof Error) turn.recordException(error);
        turn.setStatus({ code: SpanStatusCode.ERROR, message });
      }
    } else if (failedText !== undefined) {
      // Provider error bodies and Codex messages can carry content: text only with outputs visible.
      const message = this.policy.hideOutputs ? failedLabel : truncate(failedText, ERROR_MESSAGE_MAX);
      if (!this.policy.hideErrorDetails) turn.setAttribute(C.ERROR_MESSAGE, message);
      turn.setStatus({ code: SpanStatusCode.ERROR, ...(this.policy.hideErrorDetails ? {} : { message }) });
    } else {
      turn.setStatus({ code: SpanStatusCode.OK });
    }
  }

  private turnStartAttributes(): Attributes {
    const attributes: Attributes = {
      [S.FI_SPAN_KIND]: "AGENT",
      [S.GEN_AI_SPAN_KIND]: "AGENT",
      [S.AGENT_NAME]: AGENT_NAME,
      [S.PROVIDER]: PROVIDER_NAME,
      [C.THREAD_RESUMED]: this.resumed,
    };
    const options = this.threadOptions ?? {};
    if (typeof options.model === "string" && options.model) attributes[S.MODEL_NAME] = options.model;
    if (typeof options.sandboxMode === "string") attributes[C.SANDBOX_MODE] = options.sandboxMode;
    if (typeof options.approvalPolicy === "string") attributes[C.APPROVAL_POLICY] = options.approvalPolicy;
    if (typeof options.modelReasoningEffort === "string") attributes[C.REASONING_EFFORT] = options.modelReasoningEffort;
    if (typeof options.webSearchMode === "string") attributes[C.WEB_SEARCH_MODE] = options.webSearchMode;

    const { prompt, images } = normalizeInput(this.input);
    attributes[C.INPUT_LENGTH] = prompt.length;
    if (images > 0) attributes[C.INPUT_IMAGE_COUNT] = images;
    if (!this.policy.hideInputs) {
      attributes[S.INPUT_VALUE] = truncate(prompt, CONTENT_MAX);
      attributes[S.INPUT_MIME_TYPE] = MimeType.TEXT;
    }
    if (this.threadId) Object.assign(attributes, this.sessionAttributes(this.threadId));
    return attributes;
  }

  private sessionAttributes(threadId: string): Attributes {
    const attributes: Attributes = { [C.THREAD_ID]: threadId, [S.CONVERSATION_ID]: threadId };
    // An app that set session.id through fi-core context keeps it.
    if (!this.appSetSession) attributes[S.SESSION_ID] = threadId;
    return attributes;
  }

  private setThreadId(threadId: string): void {
    this.threadId = threadId;
    this.turn?.setAttributes(this.sessionAttributes(threadId));
    // A new thread starts at zero, even if this first turn fails or is cancelled:
    // the next turn then promotes its delta (which includes this turn's spend).
    if (!this.resumed && !usageBaselines.get(threadId)) usageBaselines.set(threadId, {});
  }

  /**
   * `turn.completed.usage` is the thread's running total, not the turn's own
   * spend (measured on 0.162.1: each resumed turn reports the previous total
   * plus its own requests, also across processes). The promoted
   * `gen_ai.usage.*` keys carry only this turn's new spend: the total minus
   * the last total this process saw for the thread. A new thread starts at
   * zero. A resumed thread with no total seen in this process promotes nothing
   * (`codex.usage.baseline_unknown`); later turns promote their delta.
   * A failed or cancelled turn reports no usage, so its spend lands on the
   * thread's next completed turn. Running totals are always on
   * `codex.cumulative.*`.
   */
  private setUsage(usage: UsageLike | undefined): void {
    if (!usage || !this.turn) return;
    const cumulative = usageTotals(usage);
    const attributes: Record<string, AttributeValue> = {};
    for (const [key, value] of Object.entries(cumulative)) {
      if (value !== undefined) attributes[`${C.CUMULATIVE_PREFIX}${key}`] = value;
    }
    let baseline = this.threadId ? usageBaselines.get(this.threadId) : undefined;
    if (!baseline && !this.resumed) baseline = {};
    if (!baseline) {
      if (this.threadId) usageBaselines.set(this.threadId, cumulative);
      attributes[C.USAGE_BASELINE_UNKNOWN] = true;
      this.turn.setAttributes(attributes);
      return;
    }
    let turnUsage = subtractUsage(cumulative, baseline);
    if (turnUsage) {
      if (this.threadId) usageBaselines.set(this.threadId, cumulative);
    } else {
      // The total went down (a reset, or an SDK that reports per-turn usage): use it as
      // reported, and keep the larger baseline so a later turn is not counted twice.
      turnUsage = cumulative;
      attributes[C.USAGE_NON_MONOTONIC] = true;
      if (this.threadId) usageBaselines.set(this.threadId, maxUsage(baseline, cumulative));
    }
    const { input_tokens: input, output_tokens: output } = turnUsage;
    // OpenAI accounting: input_tokens includes cached tokens, output_tokens includes reasoning.
    if (input !== undefined) attributes[S.INPUT_TOKENS] = input;
    if (output !== undefined) attributes[S.OUTPUT_TOKENS] = output;
    if (input !== undefined && output !== undefined) attributes[S.TOTAL_TOKENS] = input + output;
    if (turnUsage.cached_input_tokens !== undefined) {
      attributes[S.CACHE_READ_TOKENS] = turnUsage.cached_input_tokens;
      attributes[C.USAGE_CACHED_INPUT_TOKENS] = turnUsage.cached_input_tokens;
    }
    if (turnUsage.cache_write_input_tokens !== undefined) {
      attributes[S.CACHE_WRITE_TOKENS] = turnUsage.cache_write_input_tokens;
      attributes[C.USAGE_CACHE_WRITE_INPUT_TOKENS] = turnUsage.cache_write_input_tokens;
    }
    if (turnUsage.reasoning_output_tokens !== undefined) {
      attributes[S.REASONING_TOKENS] = turnUsage.reasoning_output_tokens;
      attributes[C.USAGE_REASONING_OUTPUT_TOKENS] = turnUsage.reasoning_output_tokens;
    }
    this.turn.setAttributes(attributes);
  }

  private itemKey(item: ThreadItemLike): string {
    if (typeof item.id === "string" && item.id) return item.id;
    this.anonymousItems += 1;
    return `__anonymous_${this.anonymousItems}`;
  }

  private onItemStarted(item: ThreadItemLike | undefined, nowMs: number): void {
    if (!item || typeof item !== "object") return;
    const key = this.itemKey(item);
    if (this.items.has(key)) return;
    const state = this.startItemSpan(item, nowMs, true);
    if (state) this.items.set(key, state);
  }

  private onItemUpdated(item: ThreadItemLike | undefined): void {
    if (!item || typeof item !== "object" || typeof item.id !== "string") return;
    const state = this.items.get(item.id);
    if (state) state.last = item;
  }

  private onItemCompleted(item: ThreadItemLike | undefined, nowMs: number): void {
    if (!item || typeof item !== "object") return;
    const key = typeof item.id === "string" && item.id ? item.id : undefined;
    let state = key ? this.items.get(key) : undefined;
    if (key) this.items.delete(key);
    if (!state) {
      // Completed-only items (agent_message, reasoning) start at the previous event.
      state = this.startItemSpan(item, this.lastEventMs, false);
    }
    if (!state) return;
    state.last = item;
    if (item.type === "agent_message" && typeof item.text === "string") this.finalResponse = item.text;
    try {
      this.applyItemAttributes(state.span, state.type, item);
      this.applyItemStatus(state.span, state.type, item);
    } catch (error) {
      diag.debug(`${LOG}: failed to record item: ${error}`);
    } finally {
      endSpan(state.span, Math.max(nowMs, this.lastEventMs));
    }
  }

  private startItemSpan(item: ThreadItemLike, startMs: number, startedEvent: boolean): ItemState | undefined {
    const type = typeof item.type === "string" ? item.type : "unknown";
    const spec = ITEM_SPANS[type] ?? { name: `codex.${type}`, ...UNKNOWN_ITEM_SPAN };
    const attributes: Attributes = {
      [S.FI_SPAN_KIND]: spec.kind,
      [S.GEN_AI_SPAN_KIND]: spec.kind,
      [C.ITEM_TYPE]: type,
      [C.ITEM_STARTED_EVENT]: startedEvent,
    };
    if (typeof item.id === "string") {
      attributes[C.ITEM_ID] = item.id;
      if (spec.tool) attributes[S.TOOL_CALL_ID] = item.id;
    }
    if (this.threadId && !this.appSetSession) attributes[S.SESSION_ID] = this.threadId;
    if (spec.tool) {
      const toolName = type === "mcp_tool_call" && typeof item.tool === "string" && item.tool ? item.tool : type;
      attributes[S.TOOL_NAME] = toolName;
      attributes[S.GEN_AI_TOOL_NAME] = toolName;
    }
    if (type === "agent_message") {
      attributes[S.PROVIDER] = PROVIDER_NAME;
      const model = this.threadOptions?.model;
      if (typeof model === "string" && model) attributes[S.MODEL_NAME] = model;
    }
    const span = this.tracer.startSpan(
      spec.name,
      { kind: SpanKind.INTERNAL, startTime: toHrTime(startMs), attributes },
      this.turnContext,
    );
    return { span, type, last: item };
  }

  /** Structural fields always; content only when the policy allows it. */
  private applyItemAttributes(span: Span, type: string, item: ThreadItemLike): void {
    const attributes: Attributes = {};
    if (typeof item.status === "string") attributes[C.ITEM_STATUS] = item.status;
    const input = (value: string) => {
      if (!this.policy.hideInputs) {
        attributes[S.INPUT_VALUE] = truncate(value, CONTENT_MAX);
      }
    };
    const output = (value: string, mime: MimeType = MimeType.TEXT) => {
      if (!this.policy.hideOutputs) {
        attributes[S.OUTPUT_VALUE] = truncate(value, CONTENT_MAX);
        attributes[S.OUTPUT_MIME_TYPE] = mime;
      }
    };
    switch (type) {
      case "agent_message":
      case "reasoning":
        if (typeof item.text === "string") {
          attributes[C.TEXT_LENGTH] = item.text.length;
          output(item.text);
        }
        break;
      case "command_execution":
        if (typeof item.exit_code === "number") attributes[C.COMMAND_EXIT_CODE] = item.exit_code;
        if (typeof item.command === "string") input(item.command);
        if (typeof item.aggregated_output === "string") {
          attributes[C.COMMAND_OUTPUT_LENGTH] = item.aggregated_output.length;
          if (item.status !== "failed" || !this.policy.hideErrorDetails) output(item.aggregated_output);
        }
        break;
      case "file_change": {
        const changes = Array.isArray(item.changes) ? item.changes : [];
        attributes[C.FILE_CHANGE_COUNT] = changes.length;
        const kinds = [...new Set(changes.map((c) => c?.kind).filter((k): k is string => typeof k === "string"))];
        if (kinds.length) attributes[C.FILE_CHANGE_KINDS] = kinds.sort().join(",");
        // Paths can name private files: they are input content.
        if (changes.length) input(safeJson(changes));
        break;
      }
      case "mcp_tool_call": {
        if (typeof item.server === "string") attributes[C.MCP_SERVER] = item.server;
        if (typeof item.tool === "string") attributes[C.MCP_TOOL] = item.tool;
        // `result` is `null` on item.started and on a failed call (see ThreadItemLike).
        if (item.arguments != null) input(safeJson(item.arguments));
        const result = item.result;
        if (result != null && (item.status !== "failed" || !this.policy.hideErrorDetails)) {
          output(safeJson(result.content ?? result), MimeType.JSON);
        }
        break;
      }
      case "web_search":
        if (typeof item.query === "string") input(item.query);
        break;
      case "todo_list": {
        const todos = Array.isArray(item.items) ? item.items : [];
        attributes[C.TODO_COUNT] = todos.length;
        attributes[C.TODO_COMPLETED] = todos.filter((t) => t?.completed === true).length;
        if (todos.length) output(safeJson(todos), MimeType.JSON);
        break;
      }
      case "error":
        // A non-fatal error item: Codex sends warnings, deprecation notices and model
        // reroutes this way. Its text can quote config lines or paths: outputs only.
        attributes[C.WARNING] = true;
        if (typeof item.message === "string") {
          attributes[C.TEXT_LENGTH] = item.message.length;
          if (!this.hideErrorText) attributes[C.ERROR_MESSAGE] = truncate(item.message, ERROR_MESSAGE_MAX);
        }
        break;
      default:
        break;
    }
    span.setAttributes(attributes);
  }

  private applyItemStatus(span: Span, type: string, item: ThreadItemLike): void {
    if (type === "error") {
      // Non-fatal (`codex.warning=true`): status stays UNSET. Turn failures are on the turn span.
      return;
    }
    if (item.status === "failed") {
      let message = `${type} failed`;
      if (type === "command_execution" && typeof item.exit_code === "number") {
        message = `command failed (exit ${item.exit_code})`;
      } else if (type === "mcp_tool_call" && typeof item.error?.message === "string" && !this.hideErrorText) {
        // A tool's error text can carry tool output: only with outputs visible.
        message = truncate(item.error.message, ERROR_MESSAGE_MAX);
      }
      span.setStatus({ code: SpanStatusCode.ERROR, message });
      return;
    }
    span.setStatus({ code: SpanStatusCode.OK });
  }
}

function endSpan(span: Span, endMs: number): void {
  try {
    span.end(toHrTime(endMs));
  } catch (error) {
    diag.debug(`${LOG}: failed to end span: ${error}`);
  }
}

function safeContextAttributes(ctx: Context): Attributes {
  try {
    return getAttributesFromContext(ctx);
  } catch {
    return {};
  }
}

