/**
 * Span model for one Cursor SDK run (one `agent.send()`, including the run
 * inside `Agent.prompt()`).
 *
 *   Cursor Agent run              (AGENT)  session.id = agent id, model, usage, status
 *   ├─ Cursor thinking            (CHAIN)  one per thinking block (deltas joined)
 *   ├─ Cursor assistant message   (LLM)    one per run of consecutive text deltas
 *   └─ Cursor tool: <name>        (TOOL)   one per tool_call call_id, running -> completed | error
 *
 * The local runtime streams `assistant` and `thinking` events as deltas
 * (`text-delta`, `thinking-delta`; `thinking-completed` carries
 * `thinking_duration_ms` with empty text) and emits `tool_call` events at
 * start, on partial updates and at completion. This class joins them back
 * into one span per logical step.
 *
 * Every public method swallows its own errors: tracing never fails the agent.
 */
import { Attributes, Context, HrTime, Span, SpanKind, SpanStatusCode, Tracer, diag, trace } from "@opentelemetry/api";
import { getAttributesFromContext } from "@traceai/fi-core";
import { MimeType } from "@traceai/fi-semantic-conventions";
import {
  AGENT_NAME,
  CursorAttributes as C,
  PROVIDER_NAME,
  SpanNames,
  StandardAttributes as S,
} from "./attributes";
import {
  AgentLike,
  ModelSelectionLike,
  RunLike,
  RunResultLike,
  SDKMessageLike,
  SendOptionsLike,
  TokenUsageLike,
  UserMessageLike,
} from "./types";

/** Which content the wrapper may copy onto spans. Both default to hidden. */
export interface ContentPolicy {
  /** Drop prompts and tool arguments. */
  hideInputs: boolean;
  /** Drop assistant text, thinking text, task summaries and tool results. */
  hideOutputs: boolean;
}

/**
 * Error text is content: messages, stack traces and Cursor's run-error text
 * can carry local paths, usernames and prompt fragments. Unless both inputs
 * and outputs are opted in, errors are exported as their class name and
 * structural fields only.
 */
export function redactsErrors(policy: ContentPolicy): boolean {
  return policy.hideInputs || policy.hideOutputs;
}

/** How a run ended, as far as the wrapper could observe it. */
export type FinishOutcome =
  /** `run.wait()` (or `Agent.prompt()`) resolved with a `RunResult`. */
  | { kind: "result"; result: RunResultLike }
  /** The run handle reached a terminal status, or its stream ended cleanly. */
  | { kind: "status" }
  /** `send()`, `stream()` or `wait()` threw. */
  | { kind: "error"; error: unknown }
  /** The consumer stopped reading and the run's end cannot be observed. */
  | { kind: "detached" };

const CONTENT_MAX = 5000;
const ERROR_MESSAGE_MAX = 500;
const LOG = "@traceai/cursor-sdk";
const TERMINAL = new Set(["finished", "error", "cancelled"]);
/** `status` stream event -> run status. */
const STATUS_EVENT_TO_RUN: Record<string, string> = {
  FINISHED: "finished",
  ERROR: "error",
  CANCELLED: "cancelled",
  EXPIRED: "error",
};

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

export function isTerminalStatus(status: unknown): boolean {
  return typeof status === "string" && TERMINAL.has(status);
}

/** Terminal run status implied by a `status` stream event, if any. */
export function statusFromEvent(status: unknown): string | undefined {
  return typeof status === "string" ? STATUS_EVENT_TO_RUN[status] : undefined;
}

/** Prompt text and image count of an `agent.send()` message. */
export function normalizeMessage(message: unknown): { prompt: string; images: number } {
  if (typeof message === "string") return { prompt: message, images: 0 };
  if (message && typeof message === "object") {
    const m = message as UserMessageLike;
    return {
      prompt: typeof m.text === "string" ? m.text : "",
      images: Array.isArray(m.images) ? m.images.length : 0,
    };
  }
  return { prompt: "", images: 0 };
}

function finite(value: unknown): number | undefined {
  return typeof value === "number" && Number.isFinite(value) ? value : undefined;
}

function modelId(model: ModelSelectionLike | undefined): string | undefined {
  return model && typeof model === "object" && typeof model.id === "string" && model.id ? model.id : undefined;
}

function hasUsage(usage: TokenUsageLike | undefined): usage is TokenUsageLike {
  if (!usage || typeof usage !== "object") return false;
  return [usage.inputTokens, usage.outputTokens, usage.totalTokens, usage.cacheReadTokens, usage.cacheWriteTokens].some(
    (v) => finite(v) !== undefined,
  );
}

const USAGE_KEYS = ["inputTokens", "outputTokens", "cacheReadTokens", "cacheWriteTokens", "totalTokens", "reasoningTokens"] as const;

function addUsage(sum: TokenUsageLike, usage: TokenUsageLike): void {
  for (const key of USAGE_KEYS) {
    const value = finite(usage[key]);
    if (value !== undefined) sum[key] = (sum[key] ?? 0) + value;
  }
}

/** Fields of `CursorSdkError` (name, code, HTTP status, retryable, operation). Never content. */
export function sdkErrorAttributes(error: unknown): Attributes {
  const attributes: Attributes = {};
  if (!error || typeof error !== "object") return attributes;
  const e = error as { name?: unknown; code?: unknown; status?: unknown; isRetryable?: unknown; operation?: unknown };
  attributes[C.ERROR_NAME] = errorName(error);
  if (typeof e.code === "string" && e.code) attributes[C.ERROR_CODE] = e.code;
  if (finite(e.status) !== undefined) attributes[C.ERROR_STATUS] = e.status as number;
  if (typeof e.isRetryable === "boolean") attributes[C.ERROR_RETRYABLE] = e.isRetryable;
  if (typeof e.operation === "string" && e.operation) attributes[C.ERROR_OPERATION] = e.operation;
  return attributes;
}

export function errorMessage(error: unknown): string {
  if (error instanceof Error) return error.message;
  if (error && typeof error === "object" && typeof (error as { message?: unknown }).message === "string") {
    return (error as { message: string }).message;
  }
  return typeof error === "string" ? error : safeJson(error, ERROR_MESSAGE_MAX);
}

/** `bc-<uuid>` ids are cloud agents (the SDK routes on this prefix); everything else is local. */
export function runtimeOf(agentId: string | undefined): "cloud" | "local" | undefined {
  if (!agentId) return undefined;
  return agentId.startsWith("bc-") ? "cloud" : "local";
}

/** The error's class name (`error.name`, else the constructor's name). */
export function errorName(error: unknown): string {
  if (error && typeof error === "object") {
    const name = (error as { name?: unknown }).name;
    if (typeof name === "string" && name && name !== "Error") return name;
    const ctor = (error as { constructor?: { name?: unknown } }).constructor?.name;
    if (typeof ctor === "string" && ctor.length > 1 && ctor !== "Object") return ctor;
    if (typeof name === "string" && name) return name;
  }
  return "Error";
}

/** Fixed, content-free summary: `<ErrorName> (<http status or code>)`, e.g. `AuthenticationError (401)`. */
export function errorSummary(error: unknown): string {
  const name = errorName(error);
  const e = (error && typeof error === "object" ? error : {}) as { status?: unknown; code?: unknown };
  const detail = finite(e.status) !== undefined ? String(e.status) : typeof e.code === "string" && e.code ? e.code : undefined;
  return detail ? `${name} (${detail})` : name;
}

/** Status / `cursor.error.message` text for a thrown error under the policy. */
function thrownErrorMessage(error: unknown, policy: ContentPolicy): string {
  return redactsErrors(policy) ? errorSummary(error) : truncate(errorMessage(error), ERROR_MESSAGE_MAX);
}

/**
 * Record the exception event. Redacted: only `exception.type` (the class
 * name), no message and no stack trace. Opted in: the full error.
 */
function recordError(span: Span, error: unknown, policy: ContentPolicy): void {
  if (redactsErrors(policy)) {
    span.recordException({ name: errorName(error) });
  } else if (error instanceof Error) {
    span.recordException(error);
  } else {
    span.recordException({ name: errorName(error), message: errorMessage(error) });
  }
}

export function safeContextAttributes(ctx: Context): Attributes {
  try {
    return getAttributesFromContext(ctx);
  } catch {
    return {};
  }
}

interface ToolState {
  span: Span;
  name: string;
  last: SDKMessageLike;
}

interface TextBlockState {
  startMs: number;
  lastMs: number;
  parts: string[];
  length: number;
  toolUses: number;
}

export interface RunTracerArgs {
  tracer: Tracer;
  policy: ContentPolicy;
  parentContext: Context;
  agent?: AgentLike;
  message?: unknown;
  sendOptions?: SendOptionsLike;
  startTimeMs?: number;
}

export class RunTracer {
  private readonly tracer: Tracer;
  private readonly policy: ContentPolicy;
  private readonly startTimeMs: number;
  private readonly appSetSession: boolean;

  private readonly span: Span;
  private readonly runContext: Context;
  private finished = false;
  private agentId?: string;
  private runId?: string;
  private modelName?: string;
  private lastEventMs: number;
  private lastStatusEvent?: string;
  private lastStatusMessage?: string;
  private readonly tools = new Map<string, ToolState>();
  private readonly endedToolIds = new Set<string>();
  private anonymousTools = 0;
  private assistant?: TextBlockState;
  private thinking?: TextBlockState;
  private readonly assistantText: string[] = [];
  private readonly usageSum: TokenUsageLike = {};
  private usageEvents = 0;

  constructor(args: RunTracerArgs) {
    this.tracer = args.tracer;
    this.policy = args.policy;
    this.startTimeMs = args.startTimeMs ?? clockMs();
    this.lastEventMs = this.startTimeMs;
    const contextAttributes = safeContextAttributes(args.parentContext);
    this.appSetSession = contextAttributes[S.SESSION_ID] != null;

    this.span = this.tracer.startSpan(
      SpanNames.RUN,
      { kind: SpanKind.INTERNAL, startTime: toHrTime(this.startTimeMs) },
      args.parentContext,
    );
    this.runContext = trace.setSpan(args.parentContext, this.span);
    try {
      this.span.setAttributes(contextAttributes);
      this.span.setAttributes(this.startAttributes(args));
    } catch (error) {
      diag.debug(`${LOG}: failed to set run attributes: ${error}`);
    }
  }

  get isFinished(): boolean {
    return this.finished;
  }

  /** The terminal run status the stream announced (`status` event), if any. */
  get statusFromStream(): string | undefined {
    return statusFromEvent(this.lastStatusEvent);
  }

  /** Copy ids and model from the `Run` handle `send()` resolved with. */
  attachRun(run: RunLike): void {
    if (this.finished || !run || typeof run !== "object") return;
    try {
      const attributes: Attributes = {};
      if (typeof run.id === "string" && run.id) {
        this.runId = run.id;
        attributes[C.RUN_ID] = run.id;
      }
      if (typeof run.requestId === "string" && run.requestId) attributes[C.REQUEST_ID] = run.requestId;
      this.span.setAttributes(attributes);
      if (typeof run.agentId === "string" && run.agentId) this.setAgentId(run.agentId);
      this.setModel(run.model);
    } catch (error) {
      diag.debug(`${LOG}: failed to attach run: ${error}`);
    }
  }

  /** Record one stream event. Never throws and never mutates the event. */
  onEvent(event: unknown): void {
    if (this.finished || !event || typeof event !== "object") return;
    try {
      const nowMs = clockMs();
      const e = event as SDKMessageLike;
      if (typeof e.agent_id === "string" && e.agent_id && e.agent_id !== this.agentId) this.setAgentId(e.agent_id);
      if (typeof e.run_id === "string" && e.run_id && !this.runId) {
        this.runId = e.run_id;
        this.span.setAttribute(C.RUN_ID, e.run_id);
      }
      if (e.type !== "assistant") this.closeAssistant();
      switch (e.type) {
        case "system":
          this.onSystem(e);
          break;
        case "assistant":
          this.onAssistant(e, nowMs);
          break;
        case "thinking":
          this.onThinking(e, nowMs);
          break;
        case "tool_call":
          this.onToolCall(e, nowMs);
          break;
        case "usage":
          if (e.usage && typeof e.usage === "object") {
            addUsage(this.usageSum, e.usage);
            this.usageEvents += 1;
          }
          break;
        case "status":
          if (typeof e.status === "string") {
            this.lastStatusEvent = e.status;
            this.span.addEvent("cursor.status", { [C.LAST_STATUS_EVENT]: e.status }, toHrTime(nowMs));
            // Cursor's run-error text: exported only when error text is opted in (see finish()).
            if (typeof e.message === "string" && e.message) {
              this.lastStatusMessage = truncate(e.message, ERROR_MESSAGE_MAX);
            }
          }
          break;
        case "request":
          if (typeof e.request_id === "string" && e.request_id) this.span.setAttribute(C.REQUEST_ID, e.request_id);
          break;
        case "task": {
          const attributes: Attributes = {};
          if (typeof e.status === "string") attributes["cursor.task.status"] = e.status;
          if (typeof e.text === "string" && !this.policy.hideOutputs) {
            attributes["cursor.task.text"] = truncate(e.text, CONTENT_MAX);
          }
          this.span.addEvent("cursor.task", attributes, toHrTime(nowMs));
          break;
        }
        default:
          break;
      }
      this.lastEventMs = nowMs;
    } catch (error) {
      diag.debug(`${LOG}: failed to record event: ${error}`);
    }
  }

  /**
   * The consumer stopped iterating `run.stream()` before it ended. Later
   * events of this run are not seen, so open child spans end now, marked
   * incomplete. The run span stays open until the run's end is observed.
   */
  detach(): void {
    if (this.finished) return;
    try {
      const endMs = clockMs();
      this.closeAssistant();
      this.closeThinking(endMs);
      for (const [id, state] of this.tools) {
        this.tools.delete(id);
        this.endedToolIds.add(id);
        this.applyToolAttributes(state.span, state.last);
        state.span.setAttribute(C.TOOL_INCOMPLETE, true);
        state.span.end(toHrTime(endMs));
      }
      this.span.setAttribute(C.RUN_STREAM_DETACHED, true);
    } catch (error) {
      diag.debug(`${LOG}: failed to detach: ${error}`);
    }
  }

  /** End every open span for this run. Idempotent. */
  finish(outcome: FinishOutcome, run?: RunLike): void {
    if (this.finished) return;
    this.finished = true;
    const span = this.span;
    try {
      const endMs = Math.max(clockMs(), this.lastEventMs);
      this.closeAssistant();
      this.closeThinking(endMs);

      const result = outcome.kind === "result" && outcome.result && typeof outcome.result === "object" ? outcome.result : undefined;
      const handle = run && typeof run === "object" ? run : undefined;
      const read = <K extends keyof RunResultLike>(key: K): RunResultLike[K] | undefined => {
        const fromResult = result?.[key];
        if (fromResult !== undefined) return fromResult;
        try {
          return handle?.[key as keyof RunLike] as RunResultLike[K] | undefined;
        } catch {
          return undefined;
        }
      };

      const handleStatus = read("status");
      let status: string | undefined = isTerminalStatus(handleStatus) ? handleStatus : this.statusFromStream;
      // A stream that ended cleanly ended with its run.
      if (!isTerminalStatus(status) && outcome.kind === "status") status = "finished";
      if (outcome.kind === "error") status = "error";
      // Detached before the end: keep what the handle says ("running"), status stays UNSET.
      if (!status && typeof handleStatus === "string") status = handleStatus;

      const attributes: Attributes = {};
      if (typeof status === "string") attributes[C.RUN_STATUS] = status;
      if (this.lastStatusEvent) attributes[C.LAST_STATUS_EVENT] = this.lastStatusEvent;
      const id = read("id");
      if (typeof id === "string" && id && !this.runId) attributes[C.RUN_ID] = id;
      const requestId = read("requestId");
      if (typeof requestId === "string" && requestId) attributes[C.REQUEST_ID] = requestId;
      const durationMs = finite(read("durationMs"));
      if (durationMs !== undefined) attributes[C.RUN_DURATION_MS] = durationMs;
      this.setModel(read("model"));
      Object.assign(attributes, this.usageAttributes(read("usage")));

      const output = read("result");
      const text = typeof output === "string" && output ? output : this.assistantText.join("");
      if (text && !this.policy.hideOutputs) {
        attributes[S.OUTPUT_VALUE] = truncate(text, CONTENT_MAX);
        attributes[S.OUTPUT_MIME_TYPE] = MimeType.TEXT;
      }

      const failed = status === "error";
      const cancelled = status === "cancelled";
      for (const [toolId, state] of this.tools) {
        this.tools.delete(toolId);
        this.endedToolIds.add(toolId);
        this.applyToolAttributes(state.span, state.last);
        state.span.setAttribute(C.TOOL_INCOMPLETE, true);
        if (cancelled) state.span.setStatus({ code: SpanStatusCode.ERROR, message: "cancelled" });
        else if (failed) state.span.setStatus({ code: SpanStatusCode.ERROR, message: "tool not completed (run failed)" });
        state.span.end(toHrTime(endMs));
      }

      span.setAttributes(attributes);
      if (outcome.kind === "error") {
        const message = thrownErrorMessage(outcome.error, this.policy);
        span.setAttributes({ ...sdkErrorAttributes(outcome.error), [C.ERROR_MESSAGE]: message });
        recordError(span, outcome.error, this.policy);
        span.setStatus({ code: SpanStatusCode.ERROR, message });
      } else if (cancelled) {
        span.setAttribute(C.RUN_CANCELLED, true);
        span.setStatus({ code: SpanStatusCode.ERROR, message: "cancelled" });
      } else if (failed) {
        const runError = read("error");
        const code = runError && typeof runError.code === "string" ? runError.code : undefined;
        if (code) span.setAttribute(C.RUN_ERROR_CODE, code);
        const raw = runError && typeof runError.message === "string" && runError.message ? runError.message : this.lastStatusMessage;
        // Cursor's run-error text can quote paths or prompt text: a fixed summary unless opted in.
        const message = redactsErrors(this.policy)
          ? code
            ? `RunError (${code})`
            : "RunError"
          : truncate(raw ?? "run failed", ERROR_MESSAGE_MAX);
        span.setAttribute(C.ERROR_MESSAGE, message);
        span.setStatus({ code: SpanStatusCode.ERROR, message });
      } else if (status === "finished") {
        span.setStatus({ code: SpanStatusCode.OK });
      }
      span.end(toHrTime(endMs));
    } catch (error) {
      diag.debug(`${LOG}: failed to finish run: ${error}`);
      try {
        span.end();
      } catch {
        /* already ended */
      }
    }
  }

  private startAttributes(args: RunTracerArgs): Attributes {
    const attributes: Attributes = {
      [S.FI_SPAN_KIND]: "AGENT",
      [S.GEN_AI_SPAN_KIND]: "AGENT",
      [S.AGENT_NAME]: AGENT_NAME,
      [S.PROVIDER]: PROVIDER_NAME,
    };
    const options = args.sendOptions && typeof args.sendOptions === "object" ? args.sendOptions : undefined;
    if (typeof options?.mode === "string") attributes[C.SEND_MODE] = options.mode;
    let agentModel: ModelSelectionLike | undefined;
    let agentId: string | undefined;
    try {
      agentModel = args.agent?.model;
      agentId = args.agent?.agentId;
    } catch {
      /* getters that throw are ignored */
    }
    const model = modelId(options?.model) ? options?.model : agentModel;
    const id = modelId(model);
    if (id) {
      this.modelName = id;
      attributes[S.MODEL_NAME] = id;
      if (Array.isArray(model?.params) && model.params.length) attributes[C.MODEL_PARAMS] = safeJson(model.params, 1000);
    }
    const { prompt, images } = normalizeMessage(args.message);
    attributes[C.INPUT_LENGTH] = prompt.length;
    if (images > 0) attributes[C.INPUT_IMAGE_COUNT] = images;
    if (!this.policy.hideInputs) {
      attributes[S.INPUT_VALUE] = truncate(prompt, CONTENT_MAX);
      attributes[S.INPUT_MIME_TYPE] = MimeType.TEXT;
    }
    if (typeof agentId === "string" && agentId) {
      this.agentId = agentId;
      Object.assign(attributes, this.agentAttributes(agentId));
    }
    return attributes;
  }

  private agentAttributes(agentId: string): Attributes {
    const attributes: Attributes = { [C.AGENT_ID]: agentId, [S.CONVERSATION_ID]: agentId };
    const runtime = runtimeOf(agentId);
    if (runtime) attributes[C.RUNTIME] = runtime;
    // An app that set session.id through fi-core context keeps it.
    if (!this.appSetSession) attributes[S.SESSION_ID] = agentId;
    return attributes;
  }

  private setAgentId(agentId: string): void {
    this.agentId = agentId;
    this.span.setAttributes(this.agentAttributes(agentId));
  }

  private setModel(model: ModelSelectionLike | undefined): void {
    const id = modelId(model);
    if (!id || id === this.modelName) return;
    this.modelName = id;
    this.span.setAttribute(S.MODEL_NAME, id);
    if (Array.isArray(model?.params) && model.params.length) this.span.setAttribute(C.MODEL_PARAMS, safeJson(model.params, 1000));
  }

  private childAttributes(kind: string): Attributes {
    const attributes: Attributes = { [S.FI_SPAN_KIND]: kind, [S.GEN_AI_SPAN_KIND]: kind };
    if (this.agentId) {
      attributes[C.AGENT_ID] = this.agentId;
      if (!this.appSetSession) attributes[S.SESSION_ID] = this.agentId;
    }
    if (this.runId) attributes[C.RUN_ID] = this.runId;
    return attributes;
  }

  private onSystem(e: SDKMessageLike): void {
    this.setModel(e.model);
    if (Array.isArray(e.tools)) {
      const tools = e.tools.filter((t): t is string => typeof t === "string");
      if (tools.length) this.span.setAttribute(C.TOOLS, tools);
    }
  }

  private onAssistant(e: SDKMessageLike, nowMs: number): void {
    const body = e.message && typeof e.message === "object" ? e.message : undefined;
    const content = Array.isArray(body?.content) ? body!.content : [];
    let text = "";
    let toolUses = 0;
    for (const block of content) {
      if (!block || typeof block !== "object") continue;
      if (block.type === "text" && typeof block.text === "string") text += block.text;
      else if (block.type === "tool_use") toolUses += 1;
    }
    if (!this.assistant) {
      this.assistant = { startMs: this.lastEventMs, lastMs: nowMs, parts: [], length: 0, toolUses: 0 };
    }
    const block = this.assistant;
    block.lastMs = nowMs;
    block.toolUses += toolUses;
    if (text) {
      block.length += text.length;
      if (!this.policy.hideOutputs) block.parts.push(text);
      this.assistantText.push(text);
    }
  }

  /** End the open assistant text block, if any, as one LLM span. */
  private closeAssistant(): void {
    const block = this.assistant;
    if (!block) return;
    this.assistant = undefined;
    const attributes = this.childAttributes("LLM");
    attributes[S.PROVIDER] = PROVIDER_NAME;
    if (this.modelName) attributes[S.MODEL_NAME] = this.modelName;
    attributes[C.TEXT_LENGTH] = block.length;
    if (block.toolUses) attributes[C.ASSISTANT_TOOL_USE_COUNT] = block.toolUses;
    if (!this.policy.hideOutputs && block.parts.length) {
      attributes[S.OUTPUT_VALUE] = truncate(block.parts.join(""), CONTENT_MAX);
      attributes[S.OUTPUT_MIME_TYPE] = MimeType.TEXT;
    }
    const span = this.tracer.startSpan(
      SpanNames.ASSISTANT_MESSAGE,
      { kind: SpanKind.INTERNAL, startTime: toHrTime(block.startMs), attributes },
      this.runContext,
    );
    span.setStatus({ code: SpanStatusCode.OK });
    span.end(toHrTime(block.lastMs));
  }

  private onThinking(e: SDKMessageLike, nowMs: number): void {
    const text = typeof e.text === "string" ? e.text : "";
    const duration = finite(e.thinking_duration_ms);
    if (!this.thinking && (text || duration !== undefined)) {
      const startMs = !text && duration !== undefined ? Math.max(this.startTimeMs, nowMs - duration) : this.lastEventMs;
      this.thinking = { startMs, lastMs: nowMs, parts: [], length: 0, toolUses: 0 };
    }
    const block = this.thinking;
    if (!block) return;
    block.lastMs = nowMs;
    if (text) {
      block.length += text.length;
      if (!this.policy.hideOutputs) block.parts.push(text);
    }
    // `thinking-completed` carries the duration (and empty text): the block is done.
    if (duration !== undefined) this.closeThinking(nowMs, duration);
  }

  private closeThinking(endMs: number, durationMs?: number): void {
    const block = this.thinking;
    if (!block) return;
    this.thinking = undefined;
    const attributes = this.childAttributes("CHAIN");
    attributes[C.TEXT_LENGTH] = block.length;
    if (durationMs !== undefined) attributes[C.THINKING_DURATION_MS] = durationMs;
    if (!this.policy.hideOutputs && block.parts.length) {
      attributes[S.OUTPUT_VALUE] = truncate(block.parts.join(""), CONTENT_MAX);
      attributes[S.OUTPUT_MIME_TYPE] = MimeType.TEXT;
    }
    const span = this.tracer.startSpan(
      SpanNames.THINKING,
      { kind: SpanKind.INTERNAL, startTime: toHrTime(block.startMs), attributes },
      this.runContext,
    );
    span.setStatus({ code: SpanStatusCode.OK });
    span.end(toHrTime(Math.max(endMs, block.startMs)));
  }

  private toolKey(e: SDKMessageLike): string {
    if (typeof e.call_id === "string" && e.call_id) return e.call_id;
    this.anonymousTools += 1;
    return `__anonymous_${this.anonymousTools}`;
  }

  private onToolCall(e: SDKMessageLike, nowMs: number): void {
    const key = this.toolKey(e);
    // A completion event repeated after the span ended (or after detach) is ignored.
    if (this.endedToolIds.has(key)) return;
    let state = this.tools.get(key);
    if (!state) {
      const startedEvent = e.status === "running" || e.status === undefined;
      state = this.startTool(e, startedEvent ? nowMs : this.lastEventMs, startedEvent);
      this.tools.set(key, state);
    }
    state.last = e;
    if (e.status === "completed" || e.status === "error") {
      this.tools.delete(key);
      this.endedToolIds.add(key);
      this.applyToolAttributes(state.span, e);
      if (e.status === "error") {
        // A tool's error result can carry tool output: only with outputs visible.
        const message =
          !this.policy.hideOutputs && e.result !== undefined ? truncate(safeJson(e.result), ERROR_MESSAGE_MAX) : `${state.name} failed`;
        state.span.setStatus({ code: SpanStatusCode.ERROR, message });
      } else {
        state.span.setStatus({ code: SpanStatusCode.OK });
      }
      state.span.end(toHrTime(nowMs));
    }
  }

  private startTool(e: SDKMessageLike, startMs: number, startedEvent: boolean): ToolState {
    const name = typeof e.name === "string" && e.name ? e.name : "unknown";
    const attributes = this.childAttributes("TOOL");
    attributes[S.TOOL_NAME] = name;
    attributes[S.GEN_AI_TOOL_NAME] = name;
    attributes[C.TOOL_STARTED_EVENT] = startedEvent;
    if (typeof e.call_id === "string" && e.call_id) {
      attributes[C.TOOL_CALL_ID] = e.call_id;
      attributes[S.TOOL_CALL_ID] = e.call_id;
    }
    const span = this.tracer.startSpan(
      `${SpanNames.TOOL_PREFIX}${name}`,
      { kind: SpanKind.INTERNAL, startTime: toHrTime(startMs), attributes },
      this.runContext,
    );
    return { span, name, last: e };
  }

  /** Structural fields always; arguments and results only when the policy allows them. */
  private applyToolAttributes(span: Span, e: SDKMessageLike): void {
    const attributes: Attributes = {};
    if (typeof e.status === "string") attributes[C.TOOL_STATUS] = e.status;
    if (e.truncated && typeof e.truncated === "object") {
      if (typeof e.truncated.args === "boolean") attributes[C.TOOL_ARGS_TRUNCATED] = e.truncated.args;
      if (typeof e.truncated.result === "boolean") attributes[C.TOOL_RESULT_TRUNCATED] = e.truncated.result;
    }
    if (e.args !== undefined && !this.policy.hideInputs) {
      attributes[S.INPUT_VALUE] = safeJson(e.args);
      attributes[S.INPUT_MIME_TYPE] = typeof e.args === "string" ? MimeType.TEXT : MimeType.JSON;
    }
    if (e.result !== undefined && !this.policy.hideOutputs) {
      attributes[S.OUTPUT_VALUE] = safeJson(e.result);
      attributes[S.OUTPUT_MIME_TYPE] = typeof e.result === "string" ? MimeType.TEXT : MimeType.JSON;
    }
    span.setAttributes(attributes);
  }

  /**
   * Token counts: the run's cumulative usage from the SDK (`RunResult.usage`
   * / `run.usage`) when present, else the sum of this run's `usage` events.
   * Promoted with Cursor's own semantics: `totalTokens` as reported (it
   * excludes `reasoningTokens`, a subset of output).
   */
  private usageAttributes(runUsage: TokenUsageLike | undefined): Attributes {
    let usage: TokenUsageLike | undefined;
    let source: "run" | "events" | undefined;
    if (hasUsage(runUsage)) {
      usage = runUsage;
      source = "run";
    } else if (this.usageEvents > 0 && hasUsage(this.usageSum)) {
      usage = this.usageSum;
      source = "events";
    }
    const attributes: Attributes = {};
    if (!usage || !source) return attributes;
    attributes[C.USAGE_SOURCE] = source;
    const input = finite(usage.inputTokens);
    const output = finite(usage.outputTokens);
    const total = finite(usage.totalTokens);
    const cacheRead = finite(usage.cacheReadTokens);
    const cacheWrite = finite(usage.cacheWriteTokens);
    const reasoning = finite(usage.reasoningTokens);
    if (input !== undefined) {
      attributes[S.INPUT_TOKENS] = input;
      attributes[C.USAGE_INPUT_TOKENS] = input;
    }
    if (output !== undefined) {
      attributes[S.OUTPUT_TOKENS] = output;
      attributes[C.USAGE_OUTPUT_TOKENS] = output;
    }
    if (total !== undefined) {
      attributes[S.TOTAL_TOKENS] = total;
      attributes[C.USAGE_TOTAL_TOKENS] = total;
    } else if (input !== undefined && output !== undefined) {
      attributes[S.TOTAL_TOKENS] = input + output;
    }
    if (cacheRead !== undefined) {
      attributes[S.CACHE_READ_TOKENS] = cacheRead;
      attributes[C.USAGE_CACHE_READ_TOKENS] = cacheRead;
    }
    if (cacheWrite !== undefined) {
      attributes[S.CACHE_WRITE_TOKENS] = cacheWrite;
      attributes[C.USAGE_CACHE_WRITE_TOKENS] = cacheWrite;
    }
    if (reasoning !== undefined) {
      attributes[S.REASONING_TOKENS] = reasoning;
      attributes[C.USAGE_REASONING_TOKENS] = reasoning;
    }
    return attributes;
  }
}

/**
 * One ERROR span for an `Agent.create()` / `Agent.resume()` that threw (for
 * example `AuthenticationError` for a bad API key). Successful calls emit no
 * span: their agent id is on every later run span.
 */
export function recordFactoryError(args: {
  tracer: Tracer;
  policy: ContentPolicy;
  name: string;
  parentContext: Context;
  startTimeMs: number;
  error: unknown;
  agentId?: string;
  model?: ModelSelectionLike;
  runtime?: "local" | "cloud";
}): void {
  try {
    const contextAttributes = safeContextAttributes(args.parentContext);
    const attributes: Attributes = {
      ...contextAttributes,
      [S.FI_SPAN_KIND]: "AGENT",
      [S.GEN_AI_SPAN_KIND]: "AGENT",
      [S.AGENT_NAME]: AGENT_NAME,
      [S.PROVIDER]: PROVIDER_NAME,
    };
    const model = modelId(args.model);
    if (model) attributes[S.MODEL_NAME] = model;
    if (args.runtime) attributes[C.RUNTIME] = args.runtime;
    if (args.agentId) {
      attributes[C.AGENT_ID] = args.agentId;
      if (contextAttributes[S.SESSION_ID] == null) attributes[S.SESSION_ID] = args.agentId;
    }
    const message = thrownErrorMessage(args.error, args.policy);
    Object.assign(attributes, sdkErrorAttributes(args.error), { [C.ERROR_MESSAGE]: message });
    const span = args.tracer.startSpan(
      args.name,
      { kind: SpanKind.INTERNAL, startTime: toHrTime(args.startTimeMs), attributes },
      args.parentContext,
    );
    recordError(span, args.error, args.policy);
    span.setStatus({ code: SpanStatusCode.ERROR, message });
    span.end(toHrTime(Math.max(clockMs(), args.startTimeMs)));
  } catch (error) {
    diag.debug(`${LOG}: failed to record ${args.name} error: ${error}`);
  }
}
