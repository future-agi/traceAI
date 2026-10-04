// Future AGI tracing for TanStack AI. This is a recipe, not a package:
// TanStack AI's own otelMiddleware creates the spans, and @traceai/fi-core
// only supplies the tracer provider and the exporter.
import { register, ProjectType } from "@traceai/fi-core";
// otelMiddleware is exported from this subpath only, not from "@tanstack/ai"
// (@tanstack/ai 0.64.0 src/middlewares/index.ts lines 15 to 18).
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
 */
export function registerFutureAgiTracing() {
  return register({
    projectType: ProjectType.OBSERVE,
    projectName: process.env.FI_PROJECT_NAME ?? "tanstack-ai-example",
  });
}

function describe(error) {
  const errors = Array.isArray(error) ? error : [error];
  return errors.map((e) => e?.message ?? String(e)).join("; ");
}

/**
 * Wait for in-flight span exports. Call it in the route's finally. An
 * unreachable collector is logged here and never fails the request.
 */
export async function flushTraces(tracerProvider) {
  try {
    await tracerProvider.forceFlush();
  } catch (error) {
    console.error(`[futureagi] span export failed: ${describe(error)}`);
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
 * - chat root that ran more than one iteration -> AGENT
 *
 * The root's kind is set in onSpanEnd because attributeEnricher runs for the
 * root when chat() starts, before the iteration count is known. A
 * single-iteration root and media "generation" spans get no kind.
 *
 * Usage: TanStack sums every model call's usage onto the root
 * (otel.ts applyRootUsage, set just before onSpanEnd). Future AGI promotes
 * gen_ai.usage.* into its token columns on any span and sums them over the
 * whole trace, so leaving the root's copy would count each call twice. When
 * the model-call spans carry usage, the root's sum moves to
 * tanstack.ai.root_usage.*. If no model-call span reported usage, the root
 * keeps it, because then it is the only copy.
 */
const PROMOTED_USAGE_KEYS = [
  "gen_ai.usage.input_tokens",
  "gen_ai.usage.output_tokens",
  "gen_ai.usage.total_tokens",
];

function moveRootUsage(span) {
  // The SDK span's attributes object is what the exporter reads at end().
  const attributes = span?.attributes;
  if (!attributes) return;
  for (const key of PROMOTED_USAGE_KEYS) {
    if (key in attributes) {
      span.setAttribute(`tanstack.ai.root_usage.${key.split(".").pop()}`, attributes[key]);
      delete attributes[key];
    }
  }
}

export function futureAgiSpanKinds() {
  const iterationsByRun = new WeakMap();
  const usageOnIterations = new WeakSet();
  return {
    attributeEnricher(info) {
      if (info.kind === "iteration") {
        iterationsByRun.set(info.ctx, info.iteration + 1);
        return { [GEN_AI_SPAN_KIND]: "LLM" };
      }
      if (info.kind === "tool") {
        return { [GEN_AI_SPAN_KIND]: "TOOL" };
      }
      return {};
    },
    onSpanEnd(info, span) {
      if (info.kind === "iteration" && span?.attributes?.["gen_ai.usage.input_tokens"] !== undefined) {
        usageOnIterations.add(info.ctx);
      }
      if (info.kind === "chat") {
        if ((iterationsByRun.get(info.ctx) ?? 0) > 1) {
          span.setAttribute(GEN_AI_SPAN_KIND, "AGENT");
        }
        if (usageOnIterations.has(info.ctx)) {
          moveRootUsage(span);
        }
      }
    },
  };
}

/**
 * The one middleware to pass to chat(). captureContent is left unset, so it
 * stays at its default of false and no prompt, completion, or tool argument
 * text lands on a span. Do not pass captureContent: true.
 */
export function futureAgiOtelMiddleware(tracer) {
  return otelMiddleware({
    tracer,
    ...futureAgiSpanKinds(),
  });
}
