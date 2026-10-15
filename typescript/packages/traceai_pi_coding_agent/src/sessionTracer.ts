/**
 * Turns the event stream of one Pi `AgentSession` into spans.
 *
 * Tree per agent run (one `session.prompt()`, including Pi's automatic
 * retries, continuations and compaction inside the run):
 *
 *   invoke_agent pi                     AGENT  agent_start .. agent_settled (see "Run end" below)
 *   ├─ pi.turn                          CHAIN  turn_start .. turn_end
 *   │  ├─ chat <model>                  LLM    assistant message_start .. message_end
 *   │  └─ execute_tool <tool>           TOOL   tool_execution_start .. tool_execution_end
 *   │     └─ execute_tool <nested>      TOOL   (event carries parentToolCallId)
 *   ├─ pi.compaction                    CHAIN  compaction_start .. compaction_end
 *   └─ pi.turn ...
 *
 * A compaction outside a run (before a prompt, or `session.compact()`) is its
 * own one-span trace with the same `session.id`.
 *
 * Run end: Pi 0.80.5 and later emit `agent_settled` once Pi will not continue
 * on its own; the run ends there. Older Pi has no `agent_settled`, so until
 * the session is known to settle, the run ends at an `agent_end` that does not
 * announce a retry (`willRetry !== true`), or at an `auto_retry_end` with
 * `success: false` (a retry cancelled during its backoff). Pi added
 * `AgentSession.waitForIdle()` in the same release as `agent_settled`, so a
 * session that has it is treated as settling from the start.
 *
 * Every span carries `session.id` = `session.sessionId`, so all runs of one
 * session group into one Future AGI session. Listeners must never throw into
 * Pi: every handler is wrapped.
 */
import { Context, Span, SpanStatusCode, context as otelContext, diag, trace } from "@opentelemetry/api";
import { FITracer } from "@traceai/fi-core";
import { MimeType } from "@traceai/fi-semantic-conventions";
import { Attr, EventNames, PI_AGENT_NAME, SpanKind, SpanNames } from "./attributes";
import { ContentPolicy, messageText, safeJson, toolCalls, toolResultText, truncate } from "./content";
import { PiCompactionResult, PiMessage, PiSessionEvent, PiSessionLike, PiUsage } from "./types";

const ERROR_TEXT_LIMIT = 1_000;

/** Status message of a failed tool when outputs are hidden. */
export const TOOL_ERROR_HIDDEN = "tool reported an error (result hidden)";

interface OpenSpan {
  span: Span;
  ctx: Context;
}

interface RunState extends OpenSpan {
  attempts: number;
  retries: number;
  turnCount: number;
  llmCount: number;
  toolCount: number;
  inputTokens: number;
  outputTokens: number;
  totalTokens: number;
  cost: number;
  inputText?: string;
  lastAssistant?: PiMessage;
}

interface TurnState extends OpenSpan {
  index: number;
}

function num(value: unknown): number | undefined {
  return typeof value === "number" && Number.isFinite(value) ? value : undefined;
}

function str(value: unknown): string | undefined {
  return typeof value === "string" && value.length > 0 ? value : undefined;
}

/**
 * Prompt tokens as OTel counts them: Pi's `usage.input` excludes cache reads
 * and cache writes, so add them back.
 */
export function promptTokens(usage: PiUsage): number | undefined {
  const input = num(usage.input);
  if (input === undefined) return undefined;
  return input + (num(usage.cacheRead) ?? 0) + (num(usage.cacheWrite) ?? 0);
}

/**
 * `gen_ai.response.finish_reasons` value for a Pi `stopReason`. Pi normalizes
 * provider reasons to `stop`, `length`, `toolUse`, `error`, `aborted`;
 * `toolUse` becomes the OTel `tool_calls`. `pending` (still streaming) has none.
 */
export function finishReason(stopReason: string | undefined): string | undefined {
  if (!stopReason || stopReason === "pending") return undefined;
  return stopReason === "toolUse" ? "tool_calls" : stopReason;
}

const HTTP_STATUS = /^\s*(?:HTTP\s*)?([45]\d\d)\b|\bstatus(?:\s*code)?\s*[:=]?\s*([45]\d\d)\b/i;
const PROVIDER_ERROR_TYPE = /"type"\s*:\s*"([A-Za-z][A-Za-z0-9_.-]{0,63})"/g;
const IDENTIFIER = /^[A-Za-z][A-Za-z0-9_.-]{0,63}$/;

/**
 * Error class and status message for a failed or aborted assistant message.
 *
 * Pi's `errorMessage` is the provider's raw error text. Gateways and
 * providers echo request fragments, account details and local paths in it,
 * so it is content: it is the status message only when outputs are shown.
 * Otherwise the message is built from the stop reason, the HTTP status the
 * text names (a leading `4xx`/`5xx`, or `status code NNN`) and the provider's
 * error `type` (an identifier such as `invalid_request_error`) or, without
 * one, the thrown error's class name from `diagnostics` (such as `TypeError`),
 * for example `error: HTTP 400 invalid_request_error`. All are numbers or
 * identifiers of at most 64 characters.
 */
export function errorDetails(
  message: PiMessage | undefined,
  showText: boolean,
): { type: string; message: string } {
  if (message?.stopReason === "aborted") {
    const raw = str(message.errorMessage);
    return { type: "aborted", message: showText && raw ? truncate(raw, ERROR_TEXT_LIMIT) : "aborted" };
  }
  const raw = str(message?.errorMessage);
  const match = raw ? HTTP_STATUS.exec(raw) : null;
  const status = match ? match[1] ?? match[2] : undefined;
  const providerType = raw ? providerErrorType(raw) : undefined;
  const errorClass = providerType ?? diagnosticErrorName(message) ?? status ?? "error";
  if (showText && raw) return { type: errorClass, message: truncate(raw, ERROR_TEXT_LIMIT) };
  const detail = [status ? `HTTP ${status}` : undefined, providerType ?? diagnosticErrorName(message)]
    .filter((part): part is string => part !== undefined)
    .join(" ");
  return { type: errorClass, message: detail ? `error: ${detail}` : "error" };
}

/**
 * Provider error types that do not end in `error` (OpenAI quota and rate-limit
 * kinds, Anthropic's 413). Any other `"type"` value must end in `error` /
 * `Error`, so an echoed request part such as `"type":"text"` is never taken
 * for the error class.
 */
const UNSUFFIXED_ERROR_TYPES = new Set(["insufficient_quota", "request_too_large", "tokens", "requests"]);

function providerErrorType(raw: string): string | undefined {
  for (const match of raw.matchAll(PROVIDER_ERROR_TYPE)) {
    const type = match[1];
    if (type === "error") continue;
    if (/error$/i.test(type) || UNSUFFIXED_ERROR_TYPES.has(type)) return type;
  }
  return undefined;
}

/** The thrown error's class name Pi recorded on the message, when it is more than `Error`. */
function diagnosticErrorName(message: PiMessage | undefined): string | undefined {
  const diagnostics = Array.isArray(message?.diagnostics) ? message!.diagnostics : [];
  for (let i = diagnostics.length - 1; i >= 0; i--) {
    const name = diagnostics[i]?.error?.name;
    if (typeof name === "string" && name !== "Error" && IDENTIFIER.test(name)) return name;
  }
  return undefined;
}

export class PiSessionTracer {
  private run?: RunState;
  private turn?: TurnState;
  private llm?: OpenSpan & { message: PiMessage };
  private compaction?: OpenSpan;
  private readonly tools = new Map<string, OpenSpan>();
  private anonymousToolSeq = 0;
  private pendingInput?: string;
  private unsubscribe?: () => void;
  private closed = false;
  /** True once the session is known to emit `agent_settled` (Pi 0.80.5+). */
  private settles: boolean;

  constructor(
    private readonly session: PiSessionLike,
    private readonly tracer: FITracer,
    private readonly policy: ContentPolicy,
  ) {
    this.settles = typeof (session as { waitForIdle?: unknown }).waitForIdle === "function";
  }

  /** Subscribe to the session. Returns the function that stops tracing it. */
  attach(): () => void {
    this.unsubscribe = this.session.subscribe((event) => this.onEvent(event));
    return () => this.detach();
  }

  /** Stop listening and end every open span, marked incomplete. Never throws. */
  detach(): void {
    if (this.closed) return;
    this.closed = true;
    try {
      this.unsubscribe?.();
    } catch (error) {
      diag.debug(`@traceai/pi-coding-agent: unsubscribe failed: ${error}`);
    }
    try {
      this.endAll(true);
    } catch (error) {
      diag.warn(`@traceai/pi-coding-agent: failed to end open spans: ${error}`);
    }
  }

  onEvent(event: PiSessionEvent): void {
    if (this.closed || !event || typeof event.type !== "string") return;
    try {
      this.dispatch(event);
    } catch (error) {
      diag.warn(`@traceai/pi-coding-agent: failed to trace ${event.type}: ${error}`);
    }
  }

  private dispatch(event: PiSessionEvent): void {
    switch (event.type) {
      case "agent_start":
        this.onAgentStart();
        break;
      case "agent_end":
        // Pi 0.80.5+ may retry or continue after agent_end; the run ends at
        // agent_settled. Older Pi never settles: end at an agent_end that does
        // not announce a retry.
        if (!this.settles && event.willRetry !== true) this.endRun(false);
        break;
      case "agent_settled":
        this.settles = true;
        this.endRun(event.aborted === true);
        break;
      case "turn_start":
        this.onTurnStart();
        break;
      case "turn_end":
        this.onTurnEnd(event);
        break;
      case "message_start":
        this.onMessageStart(event.message as PiMessage | undefined);
        break;
      case "message_end":
        this.onMessageEnd(event.message as PiMessage | undefined);
        break;
      case "tool_execution_start":
        this.onToolStart(event);
        break;
      case "tool_execution_end":
        this.onToolEnd(event);
        break;
      case "auto_retry_start":
        if (this.run) this.run.retries += 1;
        this.runEvent(EventNames.AUTO_RETRY_START, {
          "pi.attempt": num(event.attempt),
          "pi.max_attempts": num(event.maxAttempts),
          "pi.delay_ms": num(event.delayMs),
        });
        break;
      case "auto_retry_end":
        this.runEvent(EventNames.AUTO_RETRY_END, {
          "pi.attempt": num(event.attempt),
          "pi.success": event.success === true,
        });
        // Older Pi: abort() or abortRetry() during the retry backoff ends the
        // retry with success:false and no further agent_start, after an
        // agent_end that announced the retry. End the run here, or the next
        // prompt would join it. (When retries run out, the run already ended
        // at the agent_end without willRetry, so this is a no-op.)
        if (!this.settles && event.success === false) this.endRun(false);
        break;
      case "compaction_start":
        this.onCompactionStart(event);
        break;
      case "compaction_end":
        this.onCompactionEnd(event);
        break;
      default:
        // message_update, tool_execution_update, bash_execution_update, queue_update,
        // entry_appended, ...: streaming deltas and bookkeeping, not spans.
        break;
    }
  }

  // ---------------------------------------------------------------- spans

  private sessionAttributes(): Record<string, string> {
    const attrs: Record<string, string> = {};
    const sessionId = str(this.session.sessionId);
    if (sessionId) {
      attrs[Attr.SESSION_ID] = sessionId;
      attrs[Attr.PI_SESSION_ID] = sessionId;
    }
    return attrs;
  }

  private start(name: string, kind: string, parent: Context, attributes: Record<string, unknown>, startTime?: number): OpenSpan {
    const span = this.tracer.startSpan(name, startTime !== undefined ? { startTime } : {}, parent);
    span.setAttributes({
      [Attr.FI_SPAN_KIND]: kind,
      [Attr.GEN_AI_SPAN_KIND]: kind,
      ...this.sessionAttributes(),
    });
    setDefined(span, attributes);
    return { span, ctx: trace.setSpan(parent, span) };
  }

  private setError(span: Span, details: { type: string; message: string }): void {
    span.setAttribute(Attr.ERROR_TYPE, details.type);
    span.setStatus({ code: SpanStatusCode.ERROR, message: details.message });
  }

  private ensureRun(): RunState {
    if (this.run) return this.run;
    const sessionId = str(this.session.sessionId);
    const open = this.start(SpanNames.AGENT_RUN, SpanKind.AGENT, otelContext.active(), {
      [Attr.GEN_AI_OPERATION_NAME]: "invoke_agent",
      [Attr.GEN_AI_AGENT_NAME]: PI_AGENT_NAME,
      [Attr.GEN_AI_CONVERSATION_ID]: sessionId,
      // The session name is user-authored text (/name, --name, setSessionName()).
      [Attr.PI_SESSION_NAME]: this.policy.hideInputs ? undefined : str(this.session.sessionName),
    });
    this.run = {
      ...open,
      attempts: 0,
      retries: 0,
      turnCount: 0,
      llmCount: 0,
      toolCount: 0,
      inputTokens: 0,
      outputTokens: 0,
      totalTokens: 0,
      cost: 0,
      inputText: this.pendingInput,
    };
    this.pendingInput = undefined;
    return this.run;
  }

  private onAgentStart(): void {
    this.ensureRun().attempts += 1;
  }

  /** End the run's open children, deepest first. */
  private endChildren(incomplete: boolean): void {
    this.endTools(incomplete);
    this.endLlm(undefined, incomplete);
    this.endTurn(undefined, incomplete);
    this.endCompaction(incomplete);
  }

  private endRun(aborted: boolean, incomplete = false): void {
    const run = this.run;
    if (!run) return;
    this.endChildren(incomplete);
    const { span } = run;
    if (run.inputText !== undefined && !this.policy.hideInputs) {
      span.setAttribute(Attr.INPUT_VALUE, truncate(run.inputText, this.policy.maxContentLength));
      span.setAttribute(Attr.INPUT_MIME_TYPE, MimeType.TEXT);
    }
    const last = run.lastAssistant;
    if (last && !this.policy.hideOutputs) {
      span.setAttribute(Attr.OUTPUT_VALUE, truncate(messageText(last.content), this.policy.maxContentLength));
      span.setAttribute(Attr.OUTPUT_MIME_TYPE, MimeType.TEXT);
    }
    setDefined(span, {
      [Attr.PI_RUN_ATTEMPTS]: run.attempts,
      [Attr.PI_RUN_RETRY_COUNT]: run.retries,
      [Attr.PI_RUN_TURN_COUNT]: run.turnCount,
      [Attr.PI_RUN_LLM_CALL_COUNT]: run.llmCount,
      [Attr.PI_RUN_TOOL_CALL_COUNT]: run.toolCount,
      [Attr.PI_RUN_INPUT_TOKENS]: run.inputTokens,
      [Attr.PI_RUN_OUTPUT_TOKENS]: run.outputTokens,
      [Attr.PI_RUN_TOTAL_TOKENS]: run.totalTokens,
      [Attr.PI_RUN_COST_TOTAL]: run.cost > 0 ? run.cost : undefined,
      [Attr.PI_STOP_REASON]: last?.stopReason === "pending" ? undefined : str(last?.stopReason),
      [Attr.PI_ABORTED]: aborted || undefined,
      [Attr.PI_INCOMPLETE]: incomplete || undefined,
    });
    const failed = last?.stopReason === "error" || last?.stopReason === "aborted";
    if (aborted) {
      this.setError(span, { type: "aborted", message: "aborted" });
    } else if (failed) {
      this.setError(span, errorDetails(last, !this.policy.hideOutputs));
    } else if (!incomplete) {
      span.setStatus({ code: SpanStatusCode.OK });
    }
    span.end();
    this.run = undefined;
  }

  private onTurnStart(): void {
    const run = this.ensureRun();
    this.endTurn(undefined, true);
    const open = this.start(SpanNames.TURN, SpanKind.CHAIN, run.ctx, {
      [Attr.PI_TURN_INDEX]: run.turnCount,
    });
    this.turn = { ...open, index: run.turnCount };
    run.turnCount += 1;
  }

  private onTurnEnd(event: PiSessionEvent): void {
    this.endTurn(event, false);
  }

  private endTurn(event: PiSessionEvent | undefined, incomplete: boolean): void {
    const turn = this.turn;
    if (!turn) return;
    const message = event?.message as PiMessage | undefined;
    const toolResults = Array.isArray(event?.toolResults) ? (event?.toolResults as unknown[]) : undefined;
    setDefined(turn.span, {
      [Attr.PI_STOP_REASON]: message?.stopReason === "pending" ? undefined : str(message?.stopReason),
      [Attr.PI_TURN_TOOL_RESULT_COUNT]: toolResults?.length,
      [Attr.PI_INCOMPLETE]: incomplete || undefined,
    });
    if (message?.stopReason === "error" || message?.stopReason === "aborted") {
      this.setError(turn.span, errorDetails(message, !this.policy.hideOutputs));
    } else if (!incomplete) {
      turn.span.setStatus({ code: SpanStatusCode.OK });
    }
    turn.span.end();
    this.turn = undefined;
  }

  private parentContext(): Context {
    return this.turn?.ctx ?? this.ensureRun().ctx;
  }

  private onMessageStart(message: PiMessage | undefined): void {
    if (!message) return;
    if (message.role === "user") {
      // The prompt that starts the run, or a steering message inside it. A user
      // message seen before agent_start is kept for the run that follows.
      const text = messageText(message.content);
      if (this.run) {
        this.run.inputText = joinInput(this.run.inputText, text);
      } else {
        this.pendingInput = joinInput(this.pendingInput, text);
      }
      return;
    }
    if (message.role !== "assistant") return;
    this.endLlm(undefined, true);
    const startTime = num(message.timestamp);
    const open = this.start(SpanNames.llm(str(message.model)), SpanKind.LLM, this.parentContext(), {
      [Attr.GEN_AI_OPERATION_NAME]: "chat",
      [Attr.REQUEST_MODEL]: str(message.model),
      [Attr.PROVIDER_NAME]: str(message.provider),
      [Attr.PI_API]: str(message.api),
    }, startTime);
    this.llm = { ...open, message };
  }

  private onMessageEnd(message: PiMessage | undefined): void {
    if (!message || message.role !== "assistant") return;
    if (!this.llm) {
      // message_end without message_start (subscribed mid-stream).
      this.onMessageStart(message);
    }
    this.endLlm(message, false);
  }

  private endLlm(message: PiMessage | undefined, incomplete: boolean): void {
    const llm = this.llm;
    if (!llm) return;
    this.llm = undefined;
    const final = message ?? llm.message;
    const { span } = llm;
    const usage = (final.usage ?? {}) as PiUsage;
    const calls = toolCalls(final);
    const input = promptTokens(usage);
    // A message still streaming (`pending`) has no finish reason yet.
    const stopReason = incomplete || final.stopReason === "pending" ? undefined : str(final.stopReason);
    const finish = finishReason(stopReason);
    setDefined(span, {
      [Attr.REQUEST_MODEL]: str(final.model),
      [Attr.RESPONSE_MODEL]: str(final.responseModel) ?? str(final.model),
      [Attr.RESPONSE_ID]: str(final.responseId),
      [Attr.PROVIDER_NAME]: str(final.provider),
      [Attr.PI_THINKING_LEVEL]: str(final.thinkingLevel),
      [Attr.PI_STOP_REASON]: stopReason,
      [Attr.FINISH_REASONS]: finish ? [finish] : undefined,
      [Attr.PI_TOOL_CALL_COUNT]: calls.length,
      [Attr.PI_TOOL_CALL_NAMES]: calls.length ? calls.map((call) => String(call.name ?? "")) : undefined,
      [Attr.PI_INCOMPLETE]: incomplete || undefined,
    });
    if (!incomplete) {
      const output = num(usage.output);
      setUsage(span, usage);
      if (!this.policy.hideOutputs) {
        const text = messageText(final.content);
        // Tool-call arguments are the tool's input, so they follow hideInputs.
        const value = calls.length
          ? safeJson({
              text,
              toolCalls: calls.map((call) => ({
                id: call.id,
                name: call.name,
                ...(this.policy.hideInputs ? {} : { arguments: call.arguments }),
              })),
            })
          : text;
        span.setAttribute(Attr.OUTPUT_VALUE, truncate(value, this.policy.maxContentLength));
        span.setAttribute(Attr.OUTPUT_MIME_TYPE, calls.length ? MimeType.JSON : MimeType.TEXT);
      }
      const run = this.run;
      if (run) {
        run.llmCount += 1;
        run.inputTokens += input ?? 0;
        run.outputTokens += output ?? 0;
        run.totalTokens += num(usage.totalTokens) ?? 0;
        run.cost += num(usage.cost?.total) ?? 0;
        run.lastAssistant = final;
      }
    }
    if (final.stopReason === "error" || final.stopReason === "aborted") {
      this.setError(span, errorDetails(final, !this.policy.hideOutputs));
      if (final.stopReason === "aborted") span.setAttribute(Attr.PI_ABORTED, true);
    } else if (!incomplete) {
      span.setStatus({ code: SpanStatusCode.OK });
    }
    const start = num(final.timestamp);
    const duration = num(final.durationMs);
    span.end(start !== undefined && duration !== undefined && !incomplete ? start + duration : undefined);
  }

  private onToolStart(event: PiSessionEvent): string {
    const toolCallId = str(event.toolCallId);
    const toolName = str(event.toolName) ?? "unknown";
    const parentCallId = str(event.parentToolCallId);
    const parent = (parentCallId && this.tools.get(parentCallId)?.ctx) || this.parentContext();
    const open = this.start(SpanNames.tool(toolName), SpanKind.TOOL, parent, {
      [Attr.GEN_AI_OPERATION_NAME]: "execute_tool",
      [Attr.GEN_AI_TOOL_NAME]: toolName,
      [Attr.TOOL_NAME]: toolName,
      [Attr.GEN_AI_TOOL_CALL_ID]: toolCallId,
      [Attr.TOOL_CALL_ID]: toolCallId,
      [Attr.PI_TOOL_PARENT_CALL_ID]: parentCallId,
    });
    if (!this.policy.hideInputs && event.args !== undefined) {
      open.span.setAttribute(Attr.INPUT_VALUE, truncate(safeJson(event.args), this.policy.maxContentLength));
      open.span.setAttribute(Attr.INPUT_MIME_TYPE, MimeType.JSON);
    }
    if (this.run) this.run.toolCount += 1;
    // Pi tool call ids are unique within a session; a missing id still gets a span.
    const key = toolCallId ?? `${anonymousPrefix(toolName)}${this.anonymousToolSeq++}`;
    this.tools.set(key, open);
    return key;
  }

  private onToolEnd(event: PiSessionEvent): void {
    const toolCallId = str(event.toolCallId);
    let key: string | undefined = toolCallId;
    if (!toolCallId) {
      // No id: match the oldest open id-less span of the same tool.
      const prefix = anonymousPrefix(str(event.toolName) ?? "unknown");
      key = [...this.tools.keys()].find((candidate) => candidate.startsWith(prefix));
    }
    if (!key || !this.tools.has(key)) {
      // tool_execution_end without a start (subscribed mid-tool): open one now.
      key = this.onToolStart(event);
    }
    const open = this.tools.get(key)!;
    this.tools.delete(key);
    const { span } = open;
    const isError = event.isError === true;
    setDefined(span, {
      [Attr.PI_TOOL_DURATION_MS]: num(event.durationMs),
      [Attr.PI_TOOL_IS_ERROR]: isError,
    });
    const resultText = this.policy.hideOutputs ? undefined : toolResultText(event.result);
    if (resultText !== undefined) {
      span.setAttribute(Attr.OUTPUT_VALUE, truncate(resultText, this.policy.maxContentLength));
      span.setAttribute(Attr.OUTPUT_MIME_TYPE, MimeType.TEXT);
    }
    if (isError) {
      // The error text is the tool's result (paths, command output): content.
      this.setError(span, {
        type: "tool_error",
        message: resultText !== undefined ? truncate(resultText, ERROR_TEXT_LIMIT) : TOOL_ERROR_HIDDEN,
      });
    } else {
      span.setStatus({ code: SpanStatusCode.OK });
    }
    span.end();
  }

  private endTools(incomplete: boolean): void {
    // Newest first, so a nested tool ends before the tool that called it.
    for (const { span } of [...this.tools.values()].reverse()) {
      if (incomplete) span.setAttribute(Attr.PI_INCOMPLETE, true);
      span.end();
    }
    this.tools.clear();
  }

  private onCompactionStart(event: PiSessionEvent): void {
    this.endCompaction(true);
    // Inside a run (threshold or overflow compaction after a model call) it is
    // a child of the run; before a prompt or from session.compact() it is its
    // own trace.
    const parent = this.run?.ctx ?? otelContext.active();
    this.compaction = this.start(SpanNames.COMPACTION, SpanKind.CHAIN, parent, {
      [Attr.PI_COMPACTION_REASON]: str(event.reason),
    });
  }

  private onCompactionEnd(event: PiSessionEvent): void {
    if (!this.compaction) this.onCompactionStart(event);
    const { span } = this.compaction!;
    this.compaction = undefined;
    const result = (event.result && typeof event.result === "object" ? event.result : undefined) as
      | PiCompactionResult
      | undefined;
    setDefined(span, {
      [Attr.PI_COMPACTION_REASON]: str(event.reason),
      [Attr.PI_COMPACTION_TOKENS_BEFORE]: num(result?.tokensBefore),
      [Attr.PI_COMPACTION_TOKENS_AFTER]: num(result?.estimatedTokensAfter),
      [Attr.PI_COMPACTION_WILL_RETRY]: event.willRetry === true,
      [Attr.PI_ABORTED]: event.aborted === true || undefined,
    });
    const usage = result?.usage && typeof result.usage === "object" ? (result.usage as PiUsage) : undefined;
    if (usage) {
      setUsage(span, usage);
      const run = this.run;
      if (run) {
        run.inputTokens += promptTokens(usage) ?? 0;
        run.outputTokens += num(usage.output) ?? 0;
        run.totalTokens += num(usage.totalTokens) ?? 0;
        run.cost += num(usage.cost?.total) ?? 0;
      }
    }
    if (!this.policy.hideOutputs && str(result?.summary)) {
      span.setAttribute(Attr.OUTPUT_VALUE, truncate(result!.summary as string, this.policy.maxContentLength));
      span.setAttribute(Attr.OUTPUT_MIME_TYPE, MimeType.TEXT);
    }
    const errorMessage = str(event.errorMessage);
    if (event.aborted === true) {
      this.setError(span, { type: "aborted", message: "aborted" });
    } else if (errorMessage) {
      this.setError(span, {
        type: "compaction_error",
        message: this.policy.hideOutputs ? "compaction failed" : truncate(errorMessage, ERROR_TEXT_LIMIT),
      });
    } else {
      span.setStatus({ code: SpanStatusCode.OK });
    }
    span.end();
  }

  private endCompaction(incomplete: boolean): void {
    const compaction = this.compaction;
    if (!compaction) return;
    this.compaction = undefined;
    if (incomplete) compaction.span.setAttribute(Attr.PI_INCOMPLETE, true);
    compaction.span.end();
  }

  private runEvent(name: string, attributes: Record<string, unknown>): void {
    const run = this.run;
    if (!run) return;
    const defined: Record<string, string | number | boolean> = {};
    for (const [key, value] of Object.entries(attributes)) {
      if (value !== undefined) defined[key] = value as string | number | boolean;
    }
    run.span.addEvent(name, defined);
  }

  private endAll(incomplete: boolean): void {
    if (this.run) {
      this.endRun(false, incomplete);
    } else {
      this.endChildren(incomplete);
    }
  }
}

function anonymousPrefix(toolName: string): string {
  return `\u0000anonymous:${toolName}:`;
}

function joinInput(first: string | undefined, second: string | undefined): string | undefined {
  if (!first) return second;
  if (!second) return first;
  return `${first}\n\n${second}`;
}

/** Promoted token and cost keys from a Pi `Usage`. */
function setUsage(span: Span, usage: PiUsage): void {
  setDefined(span, {
    [Attr.INPUT_TOKENS]: promptTokens(usage),
    [Attr.OUTPUT_TOKENS]: num(usage.output),
    [Attr.TOTAL_TOKENS]: num(usage.totalTokens),
    [Attr.CACHE_READ_TOKENS]: num(usage.cacheRead) || undefined,
    [Attr.CACHE_WRITE_TOKENS]: num(usage.cacheWrite) || undefined,
    [Attr.REASONING_TOKENS]: num(usage.reasoning),
    [Attr.PI_UNCACHED_INPUT_TOKENS]: num(usage.input),
    [Attr.COST_TOTAL]: (num(usage.cost?.total) ?? 0) > 0 ? usage.cost?.total : undefined,
  });
}

function setDefined(span: Span, attributes: Record<string, unknown>): void {
  for (const [key, value] of Object.entries(attributes)) {
    if (value === undefined || value === null) continue;
    if (Array.isArray(value)) {
      span.setAttribute(key, value.map((item) => String(item)));
    } else if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") {
      span.setAttribute(key, value);
    }
  }
}
