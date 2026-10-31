import {
  AttributeValue, Context, Span, SpanOptions, SpanStatusCode, Tracer, diag,
} from "@opentelemetry/api";
import { ReadableSpan } from "@opentelemetry/sdk-trace-base";
import { ProjectType } from "./fi_types";
import { register, RegisterOptions } from "./otel";
import { spanWithAttributes } from "./spanTransform";

export const GEN_AI_SPAN_KIND = "gen_ai.span.kind";
export const SESSION_ID = "session.id";
export const GEN_AI_PROVIDER_NAME = "gen_ai.provider.name";
export const ERROR_TYPE = "error.type";
export const FLUSH_TIMEOUT_MS = 2000;
const AGGREGATE_USAGE = "traceai.tanstack.aggregate_usage";
const REDACTION_FAILED = "[redaction_failed]";
const providers: Readonly<Record<string, string>> = {
  openai: "openai", "openai-chat": "openai", anthropic: "anthropic",
  gemini: "google", "gemini-text-interactions": "google", grok: "xai",
  groq: "groq", ollama: "ollama",
};

/** Public subset of TanStack's span hook metadata; no runtime framework dependency. */
export interface TanStackSpanInfo {
  kind: "chat" | "iteration" | "tool" | "generation";
  ctx: { provider: string; threadId?: string };
  iteration?: number;
}

export interface TanStackTracingOptions {
  captureContent?: boolean;
  redact?: (text: string) => string;
  /** Use only when chat() receives a caller-owned, opaque threadId. */
  threadIdAsSession?: boolean;
  /** Enable only when the active application's spans also go to Future AGI. */
  joinActiveTrace?: boolean;
  maxContentLength?: number;
}

/** Options accepted by TanStack AI's public otelMiddleware(options). */
export interface TanStackOtelOptions {
  tracer: Tracer;
  captureContent: boolean;
  redact?: (text: string) => string;
  maxContentLength?: number;
  onBeforeSpanStart(info: TanStackSpanInfo, options: SpanOptions): SpanOptions;
  attributeEnricher(info: TanStackSpanInfo): Record<string, AttributeValue>;
  onSpanEnd(info: TanStackSpanInfo, span: Span): void;
}

function promoted(key: string): boolean {
  return key.startsWith("gen_ai.usage.") || key.startsWith("gen_ai.cost.");
}

/** Export-only normalization. Original spans remain intact for other exporters. */
export function normalizeTanStackSpan(span: ReadableSpan): ReadableSpan {
  if (!Object.prototype.hasOwnProperty.call(span.attributes, AGGREGATE_USAGE)) return span;
  const attributes = { ...span.attributes };
  const aggregate = attributes[AGGREGATE_USAGE] === true;
  delete attributes[AGGREGATE_USAGE];
  if (aggregate) {
    for (const key of Object.keys(attributes)) {
      if (!promoted(key)) continue;
      const suffix = key.startsWith("gen_ai.usage.")
        ? key.slice("gen_ai.usage.".length) : key.slice("gen_ai.".length);
      attributes[`tanstack.ai.root_usage.${suffix}`] = attributes[key];
      delete attributes[key];
    }
  }
  return spanWithAttributes(span, attributes);
}

function field(value: unknown, name: string): unknown {
  return value !== null && typeof value === "object" ? (value as Record<string, unknown>)[name] : undefined;
}

/** A provider error code, else the error class name. Never the message. */
export function errorType(error: unknown): string {
  const code = field(error, "code");
  if ((typeof code === "string" && code) || (typeof code === "number" && Number.isFinite(code))) return String(code);
  const name = field(error, "name");
  return typeof name === "string" && name ? name : "Error";
}

function safeRedactor(redact: (text: string) => string): (text: string) => string {
  return text => {
    try {
      const result = redact(text);
      return typeof result === "string" ? result : REDACTION_FAILED;
    } catch { return REDACTION_FAILED; }
  };
}

/**
 * Build the options to pass to TanStack AI's own otelMiddleware.
 *
 * TanStack remains the only span source. The returned tracer is a public-API
 * facade that keeps error text/stack traces out by default and observes usage;
 * the returned hooks add Future AGI kinds, providers and opt-in sessions. Root
 * usage is marked here and moved only in the Future AGI export view by
 * normalizeTanStackSpan, so no SDK internals are mutated.
 */
export function tanStackOtelOptions(
  tracer: Tracer,
  {
    captureContent = false, redact, threadIdAsSession = false,
    joinActiveTrace = false, maxContentLength,
  }: TanStackTracingOptions = {},
): TanStackOtelOptions {
  const safeRedact = safeRedactor(redact ?? (text => text));
  const usageSpans = new WeakSet<Span>();
  const iterations = new WeakMap<TanStackSpanInfo["ctx"], number>();
  const usageOnIterations = new WeakSet<TanStackSpanInfo["ctx"]>();

  const wrap = (span: Span): Span => {
    let lastType: string | undefined;
    const noteUsage = (key: string, value: unknown): void => {
      if (promoted(key) && typeof value === "number" && Number.isFinite(value)) usageSpans.add(proxy);
    };
    const proxy: Span = new Proxy(span, {
      get(target, property) {
        if (property === "setAttribute") return (key: string, value: AttributeValue) => {
          target.setAttribute(key, value);
          noteUsage(key, value);
          return proxy;
        };
        if (property === "setAttributes") return (attributes: Record<string, AttributeValue>) => {
          target.setAttributes(attributes);
          for (const [key, value] of Object.entries(attributes ?? {})) noteUsage(key, value);
          return proxy;
        };
        if (property === "recordException") return (exception: unknown, time?: Parameters<Span["recordException"]>[1]) => {
          lastType = errorType(exception);
          target.setAttribute(ERROR_TYPE, lastType);
          const message = typeof exception === "string" ? exception : field(exception, "message");
          target.recordException({
            name: lastType,
            ...(captureContent && typeof message === "string" ? { message: safeRedact(message) } : {}),
          }, time);
        };
        if (property === "setStatus") return (status: Parameters<Span["setStatus"]>[0]) => {
          // TanStack's fixed "cancelled" status carries no user/provider text.
          target.setStatus(status.code === SpanStatusCode.ERROR && typeof status.message === "string" && status.message !== "cancelled"
            ? { code: status.code, message: captureContent ? safeRedact(status.message) : (lastType ?? "Error") }
            : status);
          return proxy;
        };
        const value = Reflect.get(target, property, target);
        return typeof value === "function" ? (...args: unknown[]) => {
          const result = value.apply(target, args);
          return result === target ? proxy : result;
        } : value;
      },
    });
    return proxy;
  };

  function startActiveSpan<F extends (span: Span) => unknown>(
    name: string, arg2: SpanOptions | F, arg3?: Context | F, arg4?: F,
  ): ReturnType<F> {
    if (typeof arg2 === "function") {
      return tracer.startActiveSpan<(span: Span) => unknown>(name, span => arg2(wrap(span))) as ReturnType<F>;
    }
    if (typeof arg3 === "function") {
      return tracer.startActiveSpan<(span: Span) => unknown>(name, arg2, span => arg3(wrap(span))) as ReturnType<F>;
    }
    return tracer.startActiveSpan<(span: Span) => unknown>(name, arg2, arg3 as Context, span => arg4!(wrap(span))) as ReturnType<F>;
  }

  return {
    tracer: { startSpan: (name, options, context) => wrap(tracer.startSpan(name, options, context)), startActiveSpan },
    captureContent,
    ...(redact ? { redact: safeRedact } : {}),
    ...(maxContentLength !== undefined ? { maxContentLength } : {}),
    onBeforeSpanStart(info, options) {
      if (info.kind !== "chat") return options;
      return {
        ...options,
        ...(!joinActiveTrace ? { root: true } : {}),
        // Reserve the marker and kind at creation so end-time updates never
        // need a new attribute slot under the SDK's attribute-count limit.
        attributes: { [AGGREGATE_USAGE]: false, [GEN_AI_SPAN_KIND]: "CHAIN", ...options.attributes },
      };
    },
    attributeEnricher(info) {
      const attributes: Record<string, AttributeValue> = {};
      if (info.kind === "iteration") {
        iterations.set(info.ctx, (info.iteration ?? 0) + 1);
        attributes[GEN_AI_SPAN_KIND] = "LLM";
      } else if (info.kind === "tool") {
        attributes[GEN_AI_SPAN_KIND] = "TOOL";
      }
      if (info.kind === "chat" || info.kind === "iteration") {
        const provider = Object.prototype.hasOwnProperty.call(providers, info.ctx.provider)
          ? providers[info.ctx.provider] : undefined;
        if (provider) attributes[GEN_AI_PROVIDER_NAME] = provider;
      }
      if (threadIdAsSession && info.ctx.threadId) attributes[SESSION_ID] = info.ctx.threadId;
      return attributes;
    },
    onSpanEnd(info, span) {
      if (info.kind === "iteration" && usageSpans.has(span)) usageOnIterations.add(info.ctx);
      if (info.kind !== "chat") return;
      span.setAttribute(GEN_AI_SPAN_KIND, (iterations.get(info.ctx) ?? 0) > 1 ? "AGENT" : "CHAIN");
      // The root copy is TanStack's sum of the model-call spans. Keep it only
      // when it is the sole copy (no model-call span reported usage).
      span.setAttribute(AGGREGATE_USAGE, usageOnIterations.has(info.ctx));
      iterations.delete(info.ctx);
      usageOnIterations.delete(info.ctx);
    },
  };
}

export type TanStackRegisterOptions = Omit<
  RegisterOptions, "spanTransform" | "setGlobalTracerProvider" | "projectType" | "batch"
>;

/**
 * Register an isolated Future AGI provider for TanStack AI. It never replaces
 * the application's global provider, and normalizes only its own export view.
 */
export function registerTanStackTracing(options: TanStackRegisterOptions = {}) {
  return register({
    ...options,
    projectType: ProjectType.OBSERVE,
    setGlobalTracerProvider: false,
    spanTransform: normalizeTanStackSpan,
  });
}

/** Bounded best-effort flush for serverless handlers; diagnostics carry no payloads. */
export async function flushTraces(
  provider: { forceFlush(): Promise<void> }, { timeoutMs = FLUSH_TIMEOUT_MS } = {},
): Promise<void> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    const timeout = new Promise<boolean>(resolve => {
      timer = setTimeout(() => resolve(true), timeoutMs);
      (timer as { unref?: () => void }).unref?.();
    });
    const flushed = provider.forceFlush().then(() => false);
    if (await Promise.race([flushed, timeout])) {
      flushed.catch(() => diag.warn("Future AGI trace flush failed"));
      diag.warn("Future AGI trace flush timed out");
    }
  } catch { diag.warn("Future AGI trace flush failed"); }
  finally { clearTimeout(timer); }
}

/** Flush and stop the provider at process exit. Never throws. */
export async function shutdownTraces(provider: { shutdown(): Promise<void> }): Promise<void> {
  try { await provider.shutdown(); }
  catch { diag.warn("Future AGI trace shutdown failed"); }
}
