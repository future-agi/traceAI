import { context, diag, ROOT_CONTEXT, trace, type Attributes, type Context, type SpanContext } from "@opentelemetry/api";
import { getAttributesFromContext, REDACTED_VALUE, type TraceConfigOptions } from "@traceai/fi-core";
import { GENKIT_CONTENT_KEYS } from "./attributes";
import { genkitSpanKind, mapGenkitAttributes } from "./mapping";
import { defaultScrubContext, resolveContentPolicy, SHOW_ALL } from "./privacy";
import { toExportableSpan, type GenkitReadableSpan } from "./processor";

/** Only the traceAI-owned export branch, never the application's MultiSpanProcessor. */
export interface GenkitAutoSpanProcessor {
  onStart(span: object, parentContext: Context): void;
  onEnd(span: GenkitReadableSpan & { readonly resource?: unknown }): void;
  forceFlush?(): Promise<void>;
  shutdown?(): Promise<void>;
}

export interface EnableGenkitOptions {
  /**
   * Optional explicit traceAI export branch for embedders. With no arguments, use
   * the processor installed by @traceai/auto. Do not pass the application's
   * provider/aggregate processor: its other exporters must keep the native spans.
   */
  spanProcessor?: GenkitAutoSpanProcessor;
  traceConfig?: TraceConfigOptions & { hideLLMInvocationParameters?: boolean };
}

export interface EnableGenkitResult {
  status: "instrumented" | "disabled" | "manual" | "failed";
  reason?: string;
}

interface AutoAgent {
  processor?: GenkitAutoSpanProcessor;
  config?: { sdkDisabled?: boolean; traceConfig?: EnableGenkitOptions["traceConfig"]; apiKey?: string; secretKey?: string };
}

const AGENT_KEY = Symbol.for("traceai.auto.agent.v1");
const ENABLED_KEY = Symbol.for("traceai.genkit.auto.v1");
const SUCCESS: Readonly<EnableGenkitResult> = Object.freeze({ status: "instrumented" });
const MODEL_KINDS = new Set<string>(["LLM", "EMBEDDING", "RERANKER", "GUARDRAIL"]);

function coverageContext(span: object, parent: Context): Context {
  const native = span as { attributes?: Attributes; spanContext(): SpanContext };
  const kind = genkitSpanKind(native.attributes ?? {});
  if (kind === undefined || !MODEL_KINDS.has(kind)) return parent;
  // A non-recording view ONLY for the agent's ancestry tracker, not app context.
  const marker = Object.assign(trace.wrapSpanContext(native.spanContext()), { attributes: { "gen_ai.span.kind": kind } });
  return trace.setSpan(parent, marker);
}

function warn(): void {
  // Never stringify a thrown value: exporters and user diagnostics may contain credentials.
  try { diag.warn("@traceai/genkit: automatic mapping failed; the application will continue."); } catch { /* Fail open. */ }
}

function isGenkitSpan(span: GenkitReadableSpan): boolean {
  const scope = span.instrumentationScope ?? span.instrumentationLibrary;
  return scope?.name === "genkit-tracer" || typeof span.attributes?.["genkit:type"] === "string";
}

/**
 * Attach mapping to the existing FI branch, before its authoritative sanitizer.
 * The core agent observes transport credentials even for covered model calls and
 * scrubs the process-wide registry at export. No credential discovery, ancestry,
 * inheritance or buffering belongs here. Application spans export immediately.
 * Native exporters, timing, context, sampling and application values are untouched.
 */
export function enable(options: EnableGenkitOptions = {}): EnableGenkitResult {
  try {
    const agent = (globalThis as unknown as Record<symbol, AutoAgent | undefined>)[AGENT_KEY];
    const target = options.spanProcessor ?? agent?.processor;
    const marked = target as (GenkitAutoSpanProcessor & { [ENABLED_KEY]?: EnableGenkitResult }) | undefined;
    if (marked?.[ENABLED_KEY]) return marked[ENABLED_KEY];
    if (process.env.OTEL_SDK_DISABLED?.toLowerCase() === "true" || agent?.config?.sdkDisabled) {
      return { status: "disabled", reason: "OTEL_SDK_DISABLED" };
    }
    if (!options.spanProcessor && agent?.config && (!agent.config.apiKey || !agent.config.secretKey)) {
      return { status: "disabled", reason: "Missing traceAI credentials" };
    }
    if (!target) return { status: "manual", reason: "Requires an initialized @traceai/auto export processor" };
    if (typeof target.onStart !== "function" || typeof target.onEnd !== "function") {
      return { status: "failed", reason: "Unsupported traceAI export processor" };
    }
    const policy = resolveContentPolicy(options.traceConfig ?? agent?.config?.traceConfig);
    const scrub = defaultScrubContext();
    // Mapping metadata only: no parent/child lookup, credentials or ended spans.
    const parents = new WeakMap<object, Context>();
    const onStart = target.onStart;
    const onEnd = target.onEnd;
    const wrappedStart: GenkitAutoSpanProcessor["onStart"] = function (span, parent) {
      try { if (isGenkitSpan(span as GenkitReadableSpan)) parents.set(span, parent); } catch { /* onEnd fails open. */ }
      let covered = parent;
      try { covered = coverageContext(span, parent); } catch { /* Retain original context. */ }
      try { onStart.call(target, span, covered); } catch { warn(); }
    };
    const wrappedEnd: GenkitAutoSpanProcessor["onEnd"] = function (span) {
      try {
        if (!isGenkitSpan(span)) { onEnd.call(target, span); return; }
        const active = context.active();
        const parent = parents.get(span) ?? ((trace.getSpan(active) as unknown) === span ? active : ROOT_CONTEXT);
        const attributes = mapGenkitAttributes(span.attributes, {
          policy, scrub, contextAttributes: getAttributesFromContext(parent),
          separateInvocationParameters: true,
        });
        for (const key of GENKIT_CONTENT_KEYS) delete attributes[key];
        const output = attributes["output.value"];
        if (span.attributes["genkit:type"] === "util" && span.attributes["genkit:name"] === "generate" &&
            typeof output === "string" && output !== REDACTED_VALUE) {
          try {
            const parsed: unknown = JSON.parse(output);
            if (parsed && typeof parsed === "object" && !Array.isArray(parsed) && Object.prototype.hasOwnProperty.call(parsed, "request")) {
              // Flow/tool return values may legitimately have a `request` field.
              delete (parsed as Record<string, unknown>).request;
              attributes["output.value"] = JSON.stringify(parsed);
            }
          } catch {
            attributes["output.value"] = REDACTED_VALUE;
            delete attributes["output.mime_type"];
          }
        }
        const copy = toExportableSpan(span, attributes, span.resource, { policy: SHOW_ALL, scrub });
        // Supply SDK data fields for the agent's no-getter sanitizer.
        Object.defineProperties(copy, {
          _spanContext: { value: copy.spanContext() },
          _duration: { value: copy.duration },
          _droppedAttributesCount: { value: copy.droppedAttributesCount },
          _droppedEventsCount: { value: copy.droppedEventsCount },
          _droppedLinksCount: { value: copy.droppedLinksCount },
        });
        onStart.call(target, copy, parent);
        onEnd.call(target, copy);
      } catch { warn(); /* Never forward raw hidden content after a mapping failure. */ }
      finally { parents.delete(span); }
    };

    const replacements = { onStart: wrappedStart, onEnd: wrappedEnd };
    const descriptors = new Map<string, PropertyDescriptor | undefined>();
    try {
      for (const [key, value] of Object.entries(replacements)) {
        descriptors.set(key, Object.getOwnPropertyDescriptor(target, key));
        Object.defineProperty(target, key, { configurable: true, writable: true, value });
      }
      Object.defineProperty(target, ENABLED_KEY, { value: SUCCESS });
    } catch {
      for (const [key, descriptor] of descriptors) {
        if (descriptor) Object.defineProperty(target, key, descriptor);
        else delete (target as unknown as Record<string, unknown>)[key];
      }
      throw new Error("Unsupported traceAI export processor");
    }
    return SUCCESS;
  } catch {
    warn();
    return { status: "failed", reason: "Could not attach Genkit mapping to the traceAI export processor" };
  }
}
