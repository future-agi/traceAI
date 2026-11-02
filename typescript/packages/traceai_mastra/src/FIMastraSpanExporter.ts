import { BaseExporter } from "@mastra/observability";
import * as observability from "@mastra/observability";
import { coreFeatures } from "@mastra/core/features";
import { SpanConverter } from "@mastra/otel-exporter";
import { SpanType } from "@mastra/core/observability";
import type { AnyExportedSpan, TracingEvent } from "@mastra/core/observability";
import { FISpanKind } from "@traceai/fi-semantic-conventions";
import { OTLPTraceExporter } from "@opentelemetry/exporter-trace-otlp-proto";
import { BatchSpanProcessor } from "@opentelemetry/sdk-trace-base";
import type { ReadableSpan } from "@opentelemetry/sdk-trace-base";
import { SpanKind, SpanStatusCode } from "@opentelemetry/api";
import { generateTraceConfig } from "@traceai/fi-core";
import { sanitizeSpan } from "./privacy.js";
import { VERSION } from "./version.js";

function runtimeEmitsInference(): boolean {
  // Use the emitting runtime, NOT the converter's potentially nested dependency.
  // A namespace import keeps strict ESM compatible with the old peer floor.
  const features = (observability as { observabilityFeatures?: ReadonlySet<string> }).observabilityFeatures;
  return features?.has("model-inference-span") === true && coreFeatures.has("model-inference-span");
}

function isModelCall(type: string, inferenceEnabled: boolean): boolean {
  return type === "model_inference" || (type === "model_generation" && !inferenceEnabled);
}

export interface FISpanExporterConfig {
  /** Full OTLP traces endpoint URL. */
  endpoint: string;
  /** Auth + extra headers for the export request. */
  headers: Record<string, string>;
  /** Service name (also the resource `service.name`). */
  serviceName: string;
  /** OTel resource attributes (must include `project_name` / `project_type`). */
  resourceAttributes: Record<string, string>;
  /** Export request timeout in ms. */
  timeout?: number;
  /** Spans per export batch. */
  batchSize?: number;
}

/**
 * Mastra span type → Future AGI span kind (`gen_ai.span.kind`).
 *
 * The FI backend maps these (case-insensitively) to its observation types
 * (llm / agent / tool / chain / ...). Mastra emits `gen_ai.operation.name` but
 * NOT a span kind, so without this mapping every span renders as "unknown".
 */
// String keys also work with the peer floor, whose enum lacks newer members.
const SPAN_KIND_BY_TYPE: Readonly<Partial<Record<string, FISpanKind>>> = {
  agent_run: FISpanKind.AGENT,
  model_inference: FISpanKind.LLM,
  model_step: FISpanKind.CHAIN,
  tool_call: FISpanKind.TOOL,
  mcp_tool_call: FISpanKind.TOOL,
  client_tool_call: FISpanKind.TOOL,
  provider_tool_call: FISpanKind.TOOL,
  workflow_run: FISpanKind.CHAIN,
  workflow_step: FISpanKind.CHAIN,
  workflow_conditional: FISpanKind.CHAIN,
  workflow_parallel: FISpanKind.CHAIN,
  workflow_loop: FISpanKind.CHAIN,
  generic: FISpanKind.CHAIN,
  rag_embedding: FISpanKind.EMBEDDING,
  rag_vector_operation: FISpanKind.RETRIEVER,
};

/**
 * Write `<key>.value` + `<key>.mime_type` the way the FI backend's `set_io_value`
 * expects, so the trace UI renders the input/output.
 */
function setIoValue(
  attrs: Record<string, unknown>,
  key: "input" | "output",
  value: unknown,
): void {
  if (value === undefined || value === null) return;
  if (typeof value === "object") {
    attrs[`${key}.value`] = JSON.stringify(value);
    attrs[`${key}.mime_type`] = "application/json";
  } else {
    attrs[`${key}.value`] = String(value);
    attrs[`${key}.mime_type`] = "text/plain";
  }
}

/**
 * Enrich a converted OTLP span in place with the attributes Future AGI keys on:
 * `gen_ai.span.kind` (from the Mastra span type) and `input.value`/`output.value`
 * (from the span's input/output). Existing values are not overwritten. Exported
 * for unit testing.
 */
export function enrichSpan(
  otelSpan: ReadableSpan,
  span: AnyExportedSpan,
  inferenceEnabled = runtimeEmitsInference(),
): void {
  const attrs = otelSpan.attributes as Record<string, unknown>;

  const kind = isModelCall(span.type, inferenceEnabled) ? FISpanKind.LLM
    : span.type === SpanType.MODEL_GENERATION ? FISpanKind.CHAIN : SPAN_KIND_BY_TYPE[span.type];
  if (span.type === SpanType.MODEL_GENERATION && inferenceEnabled) {
    // Old converters classify the new generation loop as chat and attach its
    // aggregate usage. Keep that usage only on the real provider-call spans.
    attrs["gen_ai.operation.name"] = "model_generation";
    for (const key of Object.keys(attrs)) {
      if (/^gen_ai\.(?:usage|request|response)\./.test(key)
        || key === "gen_ai.input.messages" || key === "gen_ai.output.messages") delete attrs[key];
    }
  }
  if (kind && attrs["gen_ai.span.kind"] == null) {
    attrs["gen_ai.span.kind"] = kind;
  }

  // input.value / output.value so prompt / response / tool I/O renders.
  // (SpanConverter puts these under gen_ai.*/mastra.* keys the backend doesn't
  // surface in the I/O preview.)
  const s = span as { input?: unknown; output?: unknown };
  if (attrs["input.value"] == null) setIoValue(attrs, "input", s.input);
  if (attrs["output.value"] == null) setIoValue(attrs, "output", s.output);

  // Older converters only look at errorInfo, but current runtimes communicate
  // aborts/timeouts/interruptions through the authoritative termination fields.
  const termination = span as { outcome?: string | null; stopReason?: string | null };
  if (termination.outcome) attrs["mastra.span.outcome"] = termination.outcome;
  if (termination.stopReason) attrs["mastra.span.stop_reason"] = termination.stopReason;
  if (termination.outcome && termination.outcome !== "success" && otelSpan.status.code !== SpanStatusCode.ERROR) {
    otelSpan.status.code = ["error", "abort"].includes(termination.outcome)
      ? SpanStatusCode.ERROR
      : SpanStatusCode.UNSET;
    delete otelSpan.status.message;
  }
}

/**
 * Mastra v1 observability exporter for Future AGI.
 *
 * Reuses `@mastra/otel-exporter`'s {@link SpanConverter} to turn Mastra spans
 * into OTLP spans (standard `gen_ai.*` conventions), then enriches each span with
 * the attributes Future AGI keys on for display (`gen_ai.span.kind`,
 * `input.value` / `output.value`), and ships OTLP http/protobuf to the collector.
 */
export class FIMastraSpanExporter extends BaseExporter {
  name = "future-agi";
  private readonly converter: SpanConverter;
  private readonly processor: BatchSpanProcessor;
  private readonly traceConfig = generateTraceConfig();
  private readonly pending = new Set<Promise<void>>();
  private shutdownPromise?: Promise<void>;

  constructor(config: FISpanExporterConfig) {
    super();
    this.converter = new SpanConverter({
      format: "GenAI_v1_38_0",
      packageName: "@traceai/mastra",
      serviceName: config.serviceName,
      // Only `resourceAttributes` is read off this config by the converter.
      config: { resourceAttributes: {
        ...config.resourceAttributes,
        "telemetry.sdk.version": VERSION,
      } } as any,
    });
    const exporter = new OTLPTraceExporter({
      url: config.endpoint,
      headers: config.headers,
      ...(config.timeout !== undefined ? { timeoutMillis: config.timeout } : {}),
    });
    this.processor = new BatchSpanProcessor(
      exporter,
      config.batchSize !== undefined
        ? { maxExportBatchSize: config.batchSize }
        : undefined,
    );
  }

  exportTracingEvent(event: TracingEvent): Promise<void> {
    if (this.shutdownPromise) return Promise.resolve();
    // Track from public entry: BaseExporter awaits its formatter before calling
    // _exportTracingEvent, so tracking only conversion misses immediate shutdown.
    const task = super.exportTracingEvent(event);
    this.pending.add(task);
    const cleanup = () => { this.pending.delete(task); };
    void task.then(cleanup, cleanup);
    return task;
  }

  protected async _exportTracingEvent(event: TracingEvent): Promise<void> {
    if (event.type !== "span_ended") return;
    const span = event.exportedSpan;
    try {
      const inferenceEnabled = runtimeEmitsInference();
      const modelCall = isModelCall(span.type, inferenceEnabled);
      let otelSpan = await this.converter.convertSpan(span);
      if (modelCall && otelSpan.attributes["gen_ai.operation.name"] !== "chat") {
        // Independently versioned converters can recognize the other request
        // type. Reuse their native message/usage/parameter conversion with a
        // shallow view; never mutate the runtime span or emit a second span.
        const converterType = span.type === "model_inference" ? "model_generation" : "model_inference";
        otelSpan = await this.converter.convertSpan({ ...span, type: converterType } as AnyExportedSpan);
        otelSpan.attributes["mastra.span.type"] = span.type;
      }
      enrichSpan(otelSpan, span, inferenceEnabled);
      const generationWrapper = span.type === SpanType.MODEL_GENERATION && inferenceEnabled;
      this.processor.onEnd(sanitizeSpan({
        ...otelSpan,
        ...(generationWrapper ? { name: span.name, kind: SpanKind.INTERNAL } : {}),
        ...(modelCall ? { kind: SpanKind.CLIENT } : {}),
        instrumentationScope: { ...otelSpan.instrumentationScope, version: VERSION },
      }, this.traceConfig));
    } catch {
      // Conversion failures can quote sensitive input; do not send it to logs.
      this.logger.error("[@traceai/mastra] Failed to convert or enqueue span");
    }
  }

  private async drain(): Promise<void> {
    while (this.pending.size > 0) await Promise.allSettled([...this.pending]);
  }

  async flush(): Promise<void> {
    if (this.shutdownPromise) return this.shutdownPromise;
    await this.drain();
    await this.processor.forceFlush();
  }

  shutdown(): Promise<void> {
    this.shutdownPromise ??= this.drain().then(() => this.processor.shutdown());
    return this.shutdownPromise;
  }
}
