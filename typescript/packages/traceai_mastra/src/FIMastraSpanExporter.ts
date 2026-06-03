import { BaseExporter } from "@mastra/observability";
import { SpanConverter } from "@mastra/otel-exporter";
import { SpanType } from "@mastra/core/observability";
import type { AnyExportedSpan, TracingEvent } from "@mastra/core/observability";
import { OTLPTraceExporter } from "@opentelemetry/exporter-trace-otlp-proto";
import { BatchSpanProcessor } from "@opentelemetry/sdk-trace-base";
import type { ReadableSpan } from "@opentelemetry/sdk-trace-base";

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
const SPAN_KIND_BY_TYPE: Partial<Record<SpanType, string>> = {
  [SpanType.AGENT_RUN]: "AGENT",
  [SpanType.MODEL_GENERATION]: "LLM",
  [SpanType.MODEL_INFERENCE]: "LLM",
  [SpanType.MODEL_STEP]: "CHAIN",
  [SpanType.TOOL_CALL]: "TOOL",
  [SpanType.MCP_TOOL_CALL]: "TOOL",
  [SpanType.CLIENT_TOOL_CALL]: "TOOL",
  [SpanType.WORKFLOW_RUN]: "CHAIN",
  [SpanType.WORKFLOW_STEP]: "CHAIN",
  [SpanType.WORKFLOW_CONDITIONAL]: "CHAIN",
  [SpanType.WORKFLOW_PARALLEL]: "CHAIN",
  [SpanType.WORKFLOW_LOOP]: "CHAIN",
  [SpanType.GENERIC]: "CHAIN",
  [SpanType.RAG_EMBEDDING]: "EMBEDDING",
  [SpanType.RAG_VECTOR_OPERATION]: "RETRIEVER",
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
export function enrichSpan(otelSpan: ReadableSpan, span: AnyExportedSpan): void {
  const attrs = otelSpan.attributes as Record<string, unknown>;

  // Span kind so the span isn't typed "unknown" in Future AGI.
  const kind = SPAN_KIND_BY_TYPE[span.type];
  if (kind && attrs["gen_ai.span.kind"] == null) {
    attrs["gen_ai.span.kind"] = kind;
  }

  // input.value / output.value so prompt / response / tool I/O renders.
  // (SpanConverter puts these under gen_ai.*/mastra.* keys the backend doesn't
  // surface in the I/O preview.)
  const s = span as { input?: unknown; output?: unknown };
  if (attrs["input.value"] == null) setIoValue(attrs, "input", s.input);
  if (attrs["output.value"] == null) setIoValue(attrs, "output", s.output);
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

  constructor(config: FISpanExporterConfig) {
    super();
    this.converter = new SpanConverter({
      format: "GenAI_v1_38_0",
      packageName: "@traceai/mastra",
      serviceName: config.serviceName,
      // Only `resourceAttributes` is read off this config by the converter.
      config: { resourceAttributes: config.resourceAttributes } as any,
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

  protected async _exportTracingEvent(event: TracingEvent): Promise<void> {
    if (event.type !== "span_ended") return;
    const span = event.exportedSpan;
    try {
      const otelSpan = await this.converter.convertSpan(span);
      enrichSpan(otelSpan, span);
      this.processor.onEnd(otelSpan);
    } catch (error) {
      this.logger.error(
        `[@traceai/mastra] Failed to export span ${span.id}`,
        error as Error,
      );
    }
  }

  async flush(): Promise<void> {
    await this.processor.forceFlush();
  }

  async shutdown(): Promise<void> {
    await this.processor.shutdown();
  }
}
