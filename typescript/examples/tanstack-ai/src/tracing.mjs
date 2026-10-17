// Future AGI tracing for TanStack AI. This is a recipe, not a package:
// TanStack AI's own otelMiddleware creates the spans, and @traceai/fi-core
// only supplies the tracer provider and the exporter.
import { SpanStatusCode } from "@opentelemetry/api";
import { register, ProjectType } from "@traceai/fi-core";
// otelMiddleware is exported from this subpath only, not from "@tanstack/ai"
// (@tanstack/ai 0.64.0 to 0.69.0, src/middlewares/index.ts).
import { otelMiddleware } from "@tanstack/ai/middlewares/otel";

export const GEN_AI_SPAN_KIND = "gen_ai.span.kind";

/**
 * Register the Future AGI tracer provider.
 *
 * register() exports OTLP/HTTP to {FI_BASE_URL}/tracer/v1/traces, sends
 * FI_API_KEY and FI_SECRET_KEY as the X-Api-Key and X-Secret-Key headers, and
 * sets the project_name and project_type resource attributes the collector
 * requires. A batch without project_name is dropped.
 *
 * `batch: true` is not passed: with @traceai/fi-core 1.0.0 on
 * @opentelemetry/sdk-trace(-base) 2.11.0 it does not attach the batch
 * processor, so spans go through register()'s default SimpleSpanProcessor.
 *
 * `setGlobalTracerProvider: false`: the recipe passes this provider's tracer
 * to otelMiddleware, so it never needs the global provider, and leaving the
 * global slot alone keeps an app's own OpenTelemetry setup (and its other
 * spans) where it was. Removing the recipe is then dropping the middleware
 * and calling shutdownTraces().
 */
export function registerFutureAgiTracing() {
  return register({
    projectType: ProjectType.OBSERVE,
    projectName: process.env.FI_PROJECT_NAME ?? "tanstack-ai-example",
    setGlobalTracerProvider: false,
  });
}

function describe(error) {
  const errors = Array.isArray(error) ? error : [error];
  return errors.map((e) => e?.message ?? String(e)).join("; ");
}

/** How long the route waits for span export before it answers anyway. */
export const FLUSH_TIMEOUT_MS = 2000;

/**
 * Wait for in-flight span exports, at most `timeoutMs`. Call it in the
 * route's finally. Never throws: an unreachable collector is logged, and a
 * collector that does not answer within the bound is logged and left to
 * finish in the background (OTLP/HTTP gives up after its own 10 s timeout).
 *
 * This is for serverless routes, where the process may be frozen once the
 * response is sent. In a long-lived server, do not await a flush on the
 * request path at all: the SimpleSpanProcessor already exports each span when
 * it ends, so call shutdownTraces() when the process stops instead.
 */
export async function flushTraces(tracerProvider, { timeoutMs = FLUSH_TIMEOUT_MS } = {}) {
  let timer;
  const timedOut = new Promise((resolve) => {
    timer = setTimeout(() => resolve(true), timeoutMs);
    timer.unref?.();
  });
  try {
    const flushed = tracerProvider.forceFlush().then(() => false);
    if (await Promise.race([flushed, timedOut])) {
      console.error(
        `[futureagi] span export still pending after ${timeoutMs} ms; not waiting`,
      );
      flushed.catch((error) => {
        console.error(`[futureagi] span export failed: ${describe(error)}`);
      });
    }
  } catch (error) {
    console.error(`[futureagi] span export failed: ${describe(error)}`);
  } finally {
    clearTimeout(timer);
  }
}

/** Stop the exporter at process exit (shutdown flushes first). Never throws. */
export async function shutdownTraces(tracerProvider) {
  try {
    await tracerProvider.shutdown();
  } catch (error) {
    console.error(`[futureagi] tracer shutdown failed: ${describe(error)}`);
  }
}

/**
 * Future AGI span kinds for the spans otelMiddleware emits.
 *
 * Keyed on the middleware's span scope (`info.kind`), not on
 * gen_ai.operation.name: at @tanstack/ai 0.64.0 only iteration spans carry
 * gen_ai.operation.name ("chat"); the root and tool spans do not.
 *
 * - iteration (one provider model call) -> LLM
 * - tool (one tool execution)          -> TOOL
 * - chat root that ran more than one iteration -> AGENT, else CHAIN
 *
 * The root's kind is set in onSpanEnd because attributeEnricher runs for the
 * root when chat() starts, before the iteration count is known. A root with
 * no kind would be stored by Future AGI as observation type "unknown". Media
 * "generation" spans get no kind.
 *
 * Usage: TanStack sums every model call's gen_ai.usage.* keys onto the root
 * (otel.ts applyRootUsage, set just before onSpanEnd): input, output and
 * total tokens, cost, cache read/creation and reasoning tokens. Future AGI
 * promotes gen_ai.usage.* (and gen_ai.cost.*) into its token and cost
 * columns on any span and sums them over the whole trace, so leaving the
 * root's copy would count each call twice. When the model-call spans carry
 * usage, every gen_ai.usage.<suffix> key on the root moves to
 * tanstack.ai.root_usage.<suffix>, and every gen_ai.cost.<suffix> key to
 * tanstack.ai.root_usage.cost.<suffix>. If no model-call span reported
 * usage, the root keeps it, because then it is the only copy.
 */
const PROMOTED_PREFIXES = ["gen_ai.usage.", "gen_ai.cost."];

function rootUsageKey(key) {
  return key.startsWith("gen_ai.usage.")
    ? `tanstack.ai.root_usage.${key.slice("gen_ai.usage.".length)}`
    : `tanstack.ai.root_usage.${key.slice("gen_ai.".length)}`;
}

function moveRootUsage(span) {
  // Internal dependency: OpenTelemetry has no public API to remove a span
  // attribute. This edits the SDK span's `attributes` object, which the
  // exporter reads at end(). Tested with @opentelemetry/sdk-trace 2.11.0
  // (SpanImpl, reached through sdk-trace-node and sdk-trace-base 2.11.0,
  // which package.json "overrides" pins). `delete` does not lower SpanImpl's
  // private attribute count, so under a tight attributeCountLimit the
  // tanstack.ai.root_usage.* copy can be dropped. The promoted key still
  // leaves the root, so a trace's usage is never counted twice.
  const attributes = span?.attributes;
  if (!attributes) return;
  for (const key of Object.keys(attributes)) {
    if (!PROMOTED_PREFIXES.some((prefix) => key.startsWith(prefix))) continue;
    const value = attributes[key];
    delete attributes[key];
    span.setAttribute(rootUsageKey(key), value);
  }
}

export const SESSION_ID = "session.id";
export const GEN_AI_PROVIDER_NAME = "gen_ai.provider.name";

/**
 * TanStack adapter name (`adapter.name`, which otelMiddleware copies to
 * gen_ai.system) -> gen_ai.provider.name, the key fi-collector reads first for
 * a span's provider. Names from the published adapters: openaiChatCompletions
 * is "openai-chat", openaiText "openai", @tanstack/ai-gemini "gemini" and
 * "gemini-text-interactions", @tanstack/ai-grok "grok". Any other adapter
 * (OpenRouter, a custom one) leaves the provider unset. Future AGI prices a
 * call from gen_ai.request.model and its tokens, not from the provider.
 */
const ADAPTER_PROVIDERS = {
  openai: "openai",
  "openai-chat": "openai",
  anthropic: "anthropic",
  gemini: "google",
  "gemini-text-interactions": "google",
  grok: "xai",
  groq: "groq",
  ollama: "ollama",
};

/**
 * Span kinds, usage and session for the spans otelMiddleware emits.
 *
 * With `threadIdAsSession: true`, every span also gets
 * session.id = ctx.threadId, the `threadId` the caller passed to chat().
 * It is exported verbatim, so pass an opaque id (a UUID), never an email
 * address or user name.
 * Set it only when the caller passed one: chat() otherwise generates a
 * fresh thread-<ms>-<random> id per call, which would make every request its
 * own session. @traceai/fi-core's setSession() only sets a context value;
 * neither otelMiddleware nor the plain SDK tracer register()'s provider
 * hands out reads it, so the recipe uses TanStack's own threadId instead.
 */
export function futureAgiSpanKinds({ threadIdAsSession = false, joinActiveTrace = false } = {}) {
  const iterationsByRun = new WeakMap();
  const usageOnIterations = new WeakSet();
  // TanStack starts the chat root under the active OpenTelemetry context, so in
  // an app with its own provider and context manager (an HTTP server span, a
  // framework's own tracing) the root's parent is a span that is exported
  // elsewhere. Future AGI then has no root for the trace, and the trace is
  // missing from the Tracing list. The root starts a new trace unless the
  // caller asks to join the active one (when that app's spans also go to
  // Future AGI). Model-call and tool spans are parented explicitly by
  // TanStack, so they follow the root either way.
  const onBeforeSpanStart = joinActiveTrace
    ? undefined
    : (info, options) => (info.kind === "chat" ? { ...options, root: true } : options);
  const kindAttributes = (info) => {
    if (info.kind === "iteration") {
      iterationsByRun.set(info.ctx, info.iteration + 1);
      return { [GEN_AI_SPAN_KIND]: "LLM" };
    }
    if (info.kind === "tool") {
      return { [GEN_AI_SPAN_KIND]: "TOOL" };
    }
    return {};
  };
  return {
    ...(onBeforeSpanStart ? { onBeforeSpanStart } : {}),
    attributeEnricher(info) {
      const attributes = kindAttributes(info);
      const adapter = info.ctx?.provider;
      const provider = Object.hasOwn(ADAPTER_PROVIDERS, adapter) ? ADAPTER_PROVIDERS[adapter] : undefined;
      if (provider && (info.kind === "chat" || info.kind === "iteration")) {
        attributes[GEN_AI_PROVIDER_NAME] = provider;
      }
      if (threadIdAsSession && info.ctx?.threadId) {
        attributes[SESSION_ID] = info.ctx.threadId;
      }
      return attributes;
    },
    onSpanEnd(info, span) {
      if (info.kind === "iteration" && span?.attributes?.["gen_ai.usage.input_tokens"] !== undefined) {
        usageOnIterations.add(info.ctx);
      }
      if (info.kind === "chat") {
        span.setAttribute(GEN_AI_SPAN_KIND, (iterationsByRun.get(info.ctx) ?? 0) > 1 ? "AGENT" : "CHAIN");
        if (usageOnIterations.has(info.ctx)) {
          moveRootUsage(span);
        }
      }
    },
  };
}

export const ERROR_TYPE = "error.type";
/** TanStack's own fixed status text for an aborted run; kept as is. */
const CANCELLED = "cancelled";
const REDACTION_FAILED = "[redaction_failed]";

/** A provider error code (RUN_ERROR code, HTTP status) or the error's class name. */
export function errorType(error) {
  const code = error?.code;
  if ((typeof code === "string" && code) || (typeof code === "number" && Number.isFinite(code))) {
    return String(code);
  }
  const name = error?.name;
  return typeof name === "string" && name ? name : "Error";
}

function errorText(error) {
  if (typeof error === "string") return error;
  const message = error?.message;
  return typeof message === "string" ? message : undefined;
}

/**
 * Wrap the tracer otelMiddleware uses, so error details are exported the way
 * content is.
 *
 * otelMiddleware calls span.recordException(error) and sets the error's
 * message as the span status whatever captureContent is. That text comes
 * from the provider or from your tool, and the exception event carries the
 * stack trace, with local file paths and the user name. Through this
 * tracer:
 *
 * - a stack trace is never exported;
 * - with content capture off (the default), the error's message is not
 *   exported either: the exception event keeps only exception.type, the
 *   span status message is that type, and the span gets error.type (the
 *   provider's error code, such as "400" or "context_overflow", or the
 *   error class name);
 * - with captureContent: true, the message is exported, passed through the
 *   same `redact` as content (a redactor that throws yields
 *   "[redaction_failed]").
 *
 * Only the public Tracer and Span API is used: each span is the SDK span
 * behind a Proxy that replaces recordException and setStatus.
 */
export function futureAgiErrorPrivacy(tracer, { captureContent = false, redact = (text) => text } = {}) {
  const safeRedact = (text) => {
    try {
      return redact(text);
    } catch {
      return REDACTION_FAILED;
    }
  };
  const wrap = (span) => {
    let lastType;
    const proxy = new Proxy(span, {
      get(target, property) {
        if (property === "recordException") {
          return (exception, time) => {
            lastType = errorType(exception);
            target.setAttribute(ERROR_TYPE, lastType);
            const text = captureContent ? errorText(exception) : undefined;
            target.recordException(
              text ? { name: lastType, message: safeRedact(text) } : { name: lastType },
              time,
            );
          };
        }
        if (property === "setStatus") {
          return (status) => {
            let next = status;
            if (
              status?.code === SpanStatusCode.ERROR &&
              typeof status.message === "string" &&
              status.message !== CANCELLED
            ) {
              next = {
                code: status.code,
                message: captureContent ? safeRedact(status.message) : (lastType ?? "Error"),
              };
            }
            target.setStatus(next);
            return proxy;
          };
        }
        const value = Reflect.get(target, property, target);
        return typeof value === "function" ? value.bind(target) : value;
      },
    });
    return proxy;
  };
  return {
    startSpan: (name, options, context) => wrap(tracer.startSpan(name, options, context)),
    startActiveSpan: (...args) => tracer.startActiveSpan(...args),
  };
}

/**
 * The one middleware to pass to chat().
 *
 * Content capture is off by default: no prompt, completion, system prompt,
 * tool argument or result, and no error message, reaches a span, and a
 * stack trace never does (see futureAgiErrorPrivacy).
 *
 * Options:
 * - `threadIdAsSession: true` when this chat() call gets the caller's
 *   `threadId` (see futureAgiSpanKinds).
 * - `captureContent: true` to opt in to message, tool and error text
 *   (TanStack's own option), with an optional `redact(text)`.
 * - `joinActiveTrace: true` to keep TanStack's chat root under the app's
 *   active span (only when the app's own spans also go to Future AGI).
 */
export function futureAgiOtelMiddleware(
  tracer,
  { threadIdAsSession = false, captureContent = false, redact, joinActiveTrace = false } = {},
) {
  return otelMiddleware({
    tracer: futureAgiErrorPrivacy(tracer, { captureContent, ...(redact ? { redact } : {}) }),
    captureContent,
    ...(redact ? { redact } : {}),
    ...futureAgiSpanKinds({ threadIdAsSession, joinActiveTrace }),
  });
}
