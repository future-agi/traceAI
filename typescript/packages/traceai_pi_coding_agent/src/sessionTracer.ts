/**
 * Turns the event stream of one Pi `AgentSession` into spans.
 *
 * Tree per agent run (one `session.prompt()`, including Pi's automatic
 * retries and compaction):
 *
 *   invoke_agent pi                     AGENT  agent_start .. agent_settled
 *   ├─ pi.turn                          CHAIN  turn_start .. turn_end
 *   │  ├─ chat <model>                  LLM    assistant message_start .. message_end
 *   │  └─ execute_tool <tool>           TOOL   tool_execution_start .. tool_execution_end
 *   │     └─ execute_tool <nested>      TOOL   (event carries parentToolCallId)
 *   └─ pi.turn ...
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
import { PiMessage, PiSessionEvent, PiSessionLike, PiUsage } from "./types";

const ERROR_TEXT_LIMIT = 1_000;

interface OpenSpan {
  span: Span;
  ctx: Context;
}

interface RunState extends OpenSpan {
  attempts: number;
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

export class PiSessionTracer {
  private run?: RunState;
  private turn?: TurnState;
  private llm?: OpenSpan & { message: PiMessage };
  private readonly tools = new Map<string, OpenSpan>();
  private pendingInput?: string;
  private unsubscribe?: () => void;
  private closed = false;

  constructor(
    private readonly session: PiSessionLike,
    private readonly tracer: FITracer,
    private readonly policy: ContentPolicy,
  ) {}

  /** Subscribe to the session. Returns the function that stops tracing it. */
  attach(): () => void {
    this.unsubscribe = this.session.subscribe((event) => this.onEvent(event));
    return () => this.detach();
  }

  /** Stop listening and end every open span, marked incomplete. */
  detach(): void {
    if (this.closed) return;
    this.closed = true;
    try {
      this.unsubscribe?.();
    } catch (error) {
      diag.debug(`@traceai/pi-coding-agent: unsubscribe failed: ${error}`);
    }
    this.endAll(true);
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
        // Pi may retry or continue after agent_end; the run ends at agent_settled.
        break;
      case "agent_settled":
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
        break;
      case "compaction_start":
        this.runEvent(EventNames.COMPACTION_START, { "pi.reason": str(event.reason) });
        break;
      case "compaction_end":
        this.runEvent(EventNames.COMPACTION_END, {
          "pi.reason": str(event.reason),
          "pi.aborted": event.aborted === true,
          "pi.will_retry": event.willRetry === true,
        });
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

  private ensureRun(): RunState {
    if (this.run) return this.run;
    const sessionId = str(this.session.sessionId);
    const open = this.start(SpanNames.AGENT_RUN, SpanKind.AGENT, otelContext.active(), {
      [Attr.GEN_AI_OPERATION_NAME]: "invoke_agent",
      [Attr.GEN_AI_AGENT_NAME]: PI_AGENT_NAME,
      [Attr.GEN_AI_CONVERSATION_ID]: sessionId,
      [Attr.PI_SESSION_NAME]: str(this.session.sessionName),
    });
    this.run = {
      ...open,
      attempts: 0,
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

  private endRun(aborted: boolean, incomplete = false): void {
    const run = this.run;
    if (!run) return;
    this.endTurn(undefined, incomplete);
    this.endTools(incomplete);
    this.endLlm(undefined, incomplete);
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
      [Attr.PI_RUN_TURN_COUNT]: run.turnCount,
      [Attr.PI_RUN_LLM_CALL_COUNT]: run.llmCount,
      [Attr.PI_RUN_TOOL_CALL_COUNT]: run.toolCount,
      [Attr.PI_RUN_INPUT_TOKENS]: run.inputTokens,
      [Attr.PI_RUN_OUTPUT_TOKENS]: run.outputTokens,
      [Attr.PI_RUN_TOTAL_TOKENS]: run.totalTokens,
      [Attr.PI_RUN_COST_TOTAL]: run.cost > 0 ? run.cost : undefined,
      [Attr.PI_STOP_REASON]: str(last?.stopReason),
      [Attr.PI_ABORTED]: aborted || undefined,
      [Attr.PI_INCOMPLETE]: incomplete || undefined,
    });
    const failed = last?.stopReason === "error" || last?.stopReason === "aborted";
    if (aborted) {
      span.setStatus({ code: SpanStatusCode.ERROR, message: "aborted" });
    } else if (failed) {
      span.setStatus({ code: SpanStatusCode.ERROR, message: errorText(last) });
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
      [Attr.PI_STOP_REASON]: str(message?.stopReason),
      [Attr.PI_TURN_TOOL_RESULT_COUNT]: toolResults?.length,
      [Attr.PI_INCOMPLETE]: incomplete || undefined,
    });
    if (message?.stopReason === "error" || message?.stopReason === "aborted") {
      turn.span.setStatus({ code: SpanStatusCode.ERROR, message: errorText(message) });
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
    setDefined(span, {
      [Attr.REQUEST_MODEL]: str(final.model),
      [Attr.RESPONSE_MODEL]: str(final.responseModel) ?? str(final.model),
      [Attr.RESPONSE_ID]: str(final.responseId),
      [Attr.PROVIDER_NAME]: str(final.provider),
      [Attr.PI_THINKING_LEVEL]: str(final.thinkingLevel),
      [Attr.PI_STOP_REASON]: str(final.stopReason),
      [Attr.FINISH_REASONS]: str(final.stopReason) ? [final.stopReason as string] : undefined,
      [Attr.PI_TOOL_CALL_COUNT]: calls.length,
      [Attr.PI_TOOL_CALL_NAMES]: calls.length ? calls.map((call) => String(call.name ?? "")) : undefined,
      [Attr.PI_INCOMPLETE]: incomplete || undefined,
    });
    if (!incomplete) {
      const output = num(usage.output);
      setDefined(span, {
        [Attr.INPUT_TOKENS]: input,
        [Attr.OUTPUT_TOKENS]: output,
        [Attr.TOTAL_TOKENS]: num(usage.totalTokens),
        [Attr.CACHE_READ_TOKENS]: num(usage.cacheRead) || undefined,
        [Attr.CACHE_WRITE_TOKENS]: num(usage.cacheWrite) || undefined,
        [Attr.REASONING_TOKENS]: num(usage.reasoning),
        [Attr.PI_UNCACHED_INPUT_TOKENS]: num(usage.input),
        [Attr.COST_TOTAL]: (num(usage.cost?.total) ?? 0) > 0 ? usage.cost?.total : undefined,
      });
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
      span.setStatus({ code: SpanStatusCode.ERROR, message: errorText(final) });
      if (final.stopReason === "aborted") span.setAttribute(Attr.PI_ABORTED, true);
    } else if (!incomplete) {
      span.setStatus({ code: SpanStatusCode.OK });
    }
    const start = num(final.timestamp);
    const duration = num(final.durationMs);
    span.end(start !== undefined && duration !== undefined && !incomplete ? start + duration : undefined);
  }

  private onToolStart(event: PiSessionEvent): void {
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
    this.tools.set(toolCallId ?? `anonymous-${this.tools.size}-${Date.now()}`, open);
  }

  private onToolEnd(event: PiSessionEvent): void {
    const toolCallId = str(event.toolCallId);
    let open = toolCallId ? this.tools.get(toolCallId) : undefined;
    if (!open) {
      // tool_execution_end without a start (subscribed mid-tool): open one now.
      this.onToolStart(event);
      open = toolCallId ? this.tools.get(toolCallId) : undefined;
      if (!open) return;
    }
    if (toolCallId) this.tools.delete(toolCallId);
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
      span.setStatus({
        code: SpanStatusCode.ERROR,
        message: resultText !== undefined ? truncate(resultText, ERROR_TEXT_LIMIT) : "tool reported an error (result hidden)",
      });
    } else {
      span.setStatus({ code: SpanStatusCode.OK });
    }
    span.end();
  }

  private endTools(incomplete: boolean): void {
    for (const { span } of this.tools.values()) {
      if (incomplete) span.setAttribute(Attr.PI_INCOMPLETE, true);
      span.end();
    }
    this.tools.clear();
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
      this.endTurn(undefined, incomplete);
      this.endTools(incomplete);
      this.endLlm(undefined, incomplete);
    }
  }
}

function joinInput(first: string | undefined, second: string | undefined): string | undefined {
  if (!first) return second;
  if (!second) return first;
  return `${first}\n\n${second}`;
}

/**
 * Status message for a failed or aborted assistant message. `errorMessage` is
 * the provider's error text, not the prompt, so it is kept (truncated).
 */
function errorText(message: PiMessage | undefined): string {
  const text = str(message?.errorMessage) ?? (message?.stopReason === "aborted" ? "aborted" : "error");
  return truncate(text, ERROR_TEXT_LIMIT);
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
