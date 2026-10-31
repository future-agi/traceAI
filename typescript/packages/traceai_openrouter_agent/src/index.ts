import { context, trace, SpanStatusCode, type Context, type Span, type TracerProvider } from "@opentelemetry/api";
import { BasicTracerProvider, BatchSpanProcessor, type SpanProcessor } from "@opentelemetry/sdk-trace-base";
import { resourceFromAttributes } from "@opentelemetry/resources";
import { OTLPTraceExporter } from "@opentelemetry/exporter-trace-otlp-http";
import type { InlineHookConfig, LifecycleHookContext } from "@openrouter/agent" with { "resolution-mode": "import" };

const enabled = (value: string | undefined) => /^(1|true|yes|on)$/i.test(value ?? "");
export const envCaptureContent = (): boolean => enabled(process.env.FI_CAPTURE_CONTENT);
const LIMIT = 32_000;
const clip = (text: string) => text.length > LIMIT ? `${text.slice(0, LIMIT)}...[truncated]` : text;
function content(value: unknown): string {
  try {
    return clip(typeof value === "string" ? value : JSON.stringify(value) ?? "null");
  } catch {
    return "[unserializable]";
  }
}

export interface ProviderOptions {
  projectName?: string;
  baseUrl?: string;
  apiKey?: string;
  secretKey?: string;
  spanProcessors?: SpanProcessor[];
}

/** Standalone OTLP/HTTP provider. Does not replace the application's global provider. */
export function buildTracerProvider({
  projectName = process.env.FI_PROJECT_NAME ?? "openrouter-agent-app",
  baseUrl = process.env.FI_BASE_URL ?? "https://api.futureagi.com",
  apiKey = process.env.FI_API_KEY,
  secretKey = process.env.FI_SECRET_KEY,
  spanProcessors,
}: ProviderOptions = {}): BasicTracerProvider {
  if (!spanProcessors && (!apiKey || !secretKey)) throw new Error("Set FI_API_KEY and FI_SECRET_KEY");
  return new BasicTracerProvider({
    resource: resourceFromAttributes({ project_name: projectName, project_type: "observe" }),
    spanProcessors: spanProcessors ?? [new BatchSpanProcessor(new OTLPTraceExporter({
      url: `${baseUrl.replace(/\/+$/, "")}/tracer/v1/traces`,
      headers: { "X-Api-Key": apiKey!, "X-Secret-Key": secretKey! },
    }))],
  });
}

export interface TraceOptions {
  /** Prompt, tool input/output, final text and error messages. Off by default. */
  captureContent?: boolean;
  agentName?: string;
  /** Defaults to the active context at adapter creation, before lazy consumption. */
  parentContext?: Context;
}

export interface TraceSession {
  hooks: InlineHookConfig;
  /** Consume the original result exactly once and capture its final text when opted in. */
  getText(result: { getText(): Promise<string> }): Promise<string>;
  /** Pass deltas through without prefetching or draining an abandoned stream. */
  getTextStream(result: { getTextStream(): AsyncIterable<string> }): AsyncGenerator<string>;
  /** Close a run abandoned before SessionEnd. Does not cancel upstream work. */
  close(): void;
}

/**
 * One adapter per callModel invocation, including each resume. Completion payloads
 * are authoritative: hooks expose no tool-call ID, so never correlate by name or
 * by input equality (both can repeat, and PreToolUse can mutate input).
 */
export function createTraceSession(provider: TracerProvider, options: TraceOptions = {}): TraceSession {
  const capture = options.captureContent ?? envCaptureContent();
  const inputAllowed = capture && !enabled(process.env.FI_HIDE_INPUTS);
  const outputAllowed = capture && !enabled(process.env.FI_HIDE_OUTPUTS);
  const errorAllowed = capture && !enabled(process.env.FI_HIDE_ERROR_DETAILS);
  const parent = options.parentContext ?? context.active();
  const tracer = provider.getTracer("@traceai/openrouter-agent");
  const agentName = options.agentName ?? "openrouter-agent";
  let root: Span | undefined;
  let sessionId = "";
  let closed = false;
  let ended = false;
  let consumers = 0;
  let failed = false;

  function ensureRoot(ctx?: LifecycleHookContext): Span | undefined {
    if (closed) return;
    if (ctx?.sessionId) sessionId = ctx.sessionId;
    root ??= tracer.startSpan(`invoke_agent ${agentName}`, {
      attributes: {
        "fi.span.kind": "AGENT",
        "gen_ai.operation.name": "invoke_agent",
        "gen_ai.provider.name": "openrouter",
        "gen_ai.agent.name": agentName,
      },
    }, parent);
    if (sessionId) root.setAttribute("session.id", sessionId);
    return root;
  }

  function finish(): void {
    if (closed || consumers > 0 || !ended) return;
    root?.end();
    closed = true;
  }

  function markError(span: Span, error?: unknown): void {
    // Error.name and custom thrown objects can themselves contain user data.
    span.setAttribute("error.type", "Error");
    let message: string | undefined;
    if (errorAllowed && error !== undefined) {
      try {
        message = content(error instanceof Error ? error.message : typeof error === "string" ? error : "[non-Error thrown]");
      } catch { message = "[unserializable]"; }
      span.setAttribute("error.message", message);
    }
    span.setStatus({ code: SpanStatusCode.ERROR, ...(message === undefined ? {} : { message }) });
  }

  function toolSpan(name: string, ctx: LifecycleHookContext, durationMs?: number): Span | undefined {
    const activeRoot = ensureRoot(ctx);
    if (!activeRoot) return;
    const end = Date.now();
    const duration = durationMs !== undefined && Number.isFinite(durationMs) ? Math.max(0, durationMs) : 0;
    return tracer.startSpan(`execute_tool ${name}`, {
      startTime: end - duration,
      attributes: {
        "fi.span.kind": "TOOL",
        "gen_ai.operation.name": "execute_tool",
        "gen_ai.tool.name": name,
        ...(sessionId ? { "session.id": sessionId } : {}),
        // Failure hooks have no duration or invocation ID. Do not fabricate timing.
        ...(durationMs === undefined ? { "openrouter.agent.duration_unavailable": true } : {}),
      },
    }, trace.setSpan(parent, activeRoot));
  }

  const hooks: InlineHookConfig = {
    SessionStart: [{ handler: (_payload, ctx) => { ensureRoot(ctx); } }],
    UserPromptSubmit: [{ handler: ({ prompt }, ctx) => {
      const span = ensureRoot(ctx);
      if (inputAllowed) span?.setAttribute("input.value", content(prompt));
    } }],
    PostModelCall: [{ handler: (event, ctx) => {
      const activeRoot = ensureRoot(ctx);
      if (!activeRoot) return;
      const end = Date.now();
      const span = tracer.startSpan(`chat ${event.model}`, {
        startTime: end - Math.max(0, Number.isFinite(event.durationMs) ? event.durationMs : 0),
        attributes: {
          "fi.span.kind": "LLM",
          "gen_ai.operation.name": "chat",
          "gen_ai.provider.name": "openrouter",
          "gen_ai.request.model": event.model,
          "gen_ai.response.id": event.responseId,
          "session.id": ctx.sessionId,
          "openrouter.agent.turn_type": event.turnType,
          "openrouter.agent.turn_number": event.turnNumber,
        },
      }, trace.setSpan(parent, activeRoot));
      if (event.usage) {
        span.setAttributes({
          "gen_ai.usage.input_tokens": event.usage.inputTokens,
          "gen_ai.usage.output_tokens": event.usage.outputTokens,
          "gen_ai.usage.total_tokens": event.usage.totalTokens,
          "gen_ai.usage.cache_read_tokens": event.usage.cachedTokens,
          "gen_ai.usage.output_tokens.reasoning": event.usage.reasoningTokens,
          ...(event.usage.cost === undefined ? {} : { "gen_ai.cost.total": event.usage.cost }),
        });
      }
      span.end(end);
    } }],
    PostToolUse: [{ handler: ({ toolName, toolInput, toolOutput, durationMs }, ctx) => {
      const span = toolSpan(toolName, ctx, durationMs);
      if (!span) return;
      if (inputAllowed) span.setAttribute("input.value", content(toolInput));
      if (outputAllowed) span.setAttribute("output.value", content(toolOutput));
      span.end();
    } }],
    PostToolUseFailure: [{ handler: ({ toolName, toolInput, error }, ctx) => {
      const span = toolSpan(toolName, ctx);
      if (!span) return;
      if (inputAllowed) span.setAttribute("input.value", content(toolInput));
      markError(span, error);
      span.end();
      // A handled tool failure is not necessarily a failed agent run.
    } }],
    SessionEnd: [{ handler: ({ reason, totalUsage }, ctx) => {
      const span = ensureRoot(ctx);
      if (!span) return;
      span.setAttribute("openrouter.agent.end_reason", reason);
      if (totalUsage) span.setAttribute("openrouter.agent.model_calls", totalUsage.modelCalls);
      // Usage stays on model spans only to avoid counting it twice.
      if (reason !== "complete") { failed = true; markError(span); }
      ended = true;
      finish();
    } }],
  };

  function consumeStart(): void {
    if (closed) throw new Error("Create a new trace session for each callModel invocation");
    consumers++;
    ensureRoot();
  }
  function consumeEnd(): void {
    consumers--;
    // Approval resumes can skip SessionStart/SessionEnd upstream. Consumption
    // still defines a bounded invocation; no private model-result fields needed.
    ended = true;
    finish();
  }

  return {
    hooks,
    async getText(result) {
      consumeStart();
      try {
        const text = await result.getText();
        if (outputAllowed && !failed) root?.setAttribute("output.value", content(text));
        return text;
      } catch (error) {
        failed = true;
        if (root) markError(root, error);
        throw error;
      } finally { consumeEnd(); }
    },
    async *getTextStream(result) {
      consumeStart();
      let text = "";
      let complete = false;
      try {
        for await (const delta of result.getTextStream()) {
          if (outputAllowed && text.length <= LIMIT) text += delta.slice(0, LIMIT + 1 - text.length);
          yield delta;
        }
        complete = true;
        if (outputAllowed && !failed) root?.setAttribute("output.value", clip(text));
      } catch (error) {
        failed = true;
        if (root) markError(root, error);
        throw error;
      } finally {
        if (!complete && root) {
          failed = true;
          root.setAttribute("openrouter.agent.consumption_incomplete", true);
          markError(root);
        }
        consumeEnd();
      }
    },
    close() {
      if (closed) return;
      failed = true;
      if (root) {
        root.setAttribute("openrouter.agent.consumption_incomplete", true);
        markError(root);
      }
      ended = true;
      finish();
    },
  };
}

/** Compatible replacement for the former docs helper. Final text needs createTraceSession. */
export function createTraceHooks(provider: TracerProvider, options: TraceOptions = {}): InlineHookConfig {
  return createTraceSession(provider, options).hooks;
}
