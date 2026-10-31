import { BaseExporter } from "@mastra/observability";
import { SpanConverter } from "@mastra/otel-exporter";
import { SpanType } from "@mastra/core/observability";
import type { AnyExportedSpan, TracingEvent } from "@mastra/core/observability";
import { FISpanKind } from "@traceai/fi-semantic-conventions";
import { OTLPTraceExporter } from "@opentelemetry/exporter-trace-otlp-proto";
import { BatchSpanProcessor, TraceIdRatioBasedSampler } from "@opentelemetry/sdk-trace-base";
import { diag, TraceFlags } from "@opentelemetry/api";
import { exportedSpanMetadata, samplingContext } from "./span-context.js";
import type { ReadableSpan, SpanProcessor } from "@opentelemetry/sdk-trace-base";
import { ownData, replaceOwnData, snapshotData, snapshotMastraSpan } from "./snapshot.js";

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
  /** Forward through an existing agent's privacy/export boundary; never owned here. */
  spanProcessor?: SpanProcessor;
  /** Head sampling ratio when forwarding Mastra's already-created spans. */
  sampleRate?: number;
}

/**
 * Mastra span type → Future AGI span kind (`gen_ai.span.kind`).
 *
 * The FI backend maps these (case-insensitively) to its observation types
 * (llm / agent / tool / chain / ...). Mastra emits `gen_ai.operation.name` but
 * NOT a span kind, so without this mapping every span renders as "unknown".
 */
const SPAN_KIND_BY_TYPE: Partial<Record<SpanType, FISpanKind>> = {
  [SpanType.AGENT_RUN]: FISpanKind.AGENT,
  [SpanType.MODEL_GENERATION]: FISpanKind.LLM,
  [SpanType.MODEL_INFERENCE]: FISpanKind.LLM,
  [SpanType.MODEL_STEP]: FISpanKind.CHAIN,
  [SpanType.TOOL_CALL]: FISpanKind.TOOL,
  [SpanType.MCP_TOOL_CALL]: FISpanKind.TOOL,
  [SpanType.CLIENT_TOOL_CALL]: FISpanKind.TOOL,
  [SpanType.WORKFLOW_RUN]: FISpanKind.CHAIN,
  [SpanType.WORKFLOW_STEP]: FISpanKind.CHAIN,
  [SpanType.WORKFLOW_CONDITIONAL]: FISpanKind.CHAIN,
  [SpanType.WORKFLOW_PARALLEL]: FISpanKind.CHAIN,
  [SpanType.WORKFLOW_LOOP]: FISpanKind.CHAIN,
  [SpanType.GENERIC]: FISpanKind.CHAIN,
  [SpanType.RAG_EMBEDDING]: FISpanKind.EMBEDDING,
  [SpanType.RAG_VECTOR_OPERATION]: FISpanKind.RETRIEVER,
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
  // This helper is also callable directly: do not assume its arguments have
  // already crossed the converter boundary, or mutate shared attribute values.
  const attrs = snapshotData(ownData(otelSpan, "attributes"));
  if (!attrs || typeof attrs !== "object" || Array.isArray(attrs)) return;
  if (!replaceOwnData(otelSpan, "attributes", attrs)) return;
  enrichSnapshot(otelSpan, snapshotMastraSpan(span));
}

/** Both arguments are detached data created by us/the upstream converter. */
function enrichSnapshot(otelSpan: ReadableSpan, span: AnyExportedSpan): void {
  const attrs = otelSpan.attributes as Record<string, unknown>;
  const type = typeof span.type === "string" ? span.type : "generic";

  // Span kind so the span isn't typed "unknown" in Future AGI.
  const kind = Object.getOwnPropertyDescriptor(SPAN_KIND_BY_TYPE, type)?.value;
  if (kind && attrs["gen_ai.span.kind"] == null) {
    attrs["gen_ai.span.kind"] = kind;
  }

  // input.value / output.value so prompt / response / tool I/O renders.
  // (SpanConverter puts these under gen_ai.*/mastra.* keys the backend doesn't
  // surface in the I/O preview.)
  const s = span as { input?: unknown; output?: unknown };
  if (attrs["input.value"] == null) setIoValue(attrs, "input", s.input);
  if (type === SpanType.RAG_EMBEDDING) {
    // Anonymous numeric arrays otherwise look like ordinary JSON content at the
    // shared privacy boundary. Label the entire embedding result, including
    // string/structured variants, so vector hiding AND output hiding apply at
    // that one boundary. Do not duplicate vectors in an unlabelled I/O alias.
    const output = attrs["output.value"] ?? s.output;
    if (output !== undefined && output !== null) {
      const labelled = Object.create(null);
      labelled.embeddings = output;
      setIoValue(attrs, "output", labelled);
    }
  } else if (attrs["output.value"] == null) setIoValue(attrs, "output", s.output);

  // SpanConverter duplicates framework I/O under vendor-specific keys. Keep the
  // canonical input/output families instead, so the agent's ONE export-time
  // privacy boundary also covers agent, workflow, step and inference content.
  delete attrs[`mastra.${type.toLowerCase()}.input`];
  delete attrs[`mastra.${type.toLowerCase()}.output`];

  // The converter emits two invocation aliases outside the shared sanitizer's
  // scalar vocabulary. Canonicalize them unconditionally, not with a second
  // hide-flag filter here. The one export boundary owns parameter masking.
  const parameters: Record<string, unknown> = {};
  for (const name of ["top_k", "stop_sequences"]) {
    const key = `gen_ai.request.${name}`;
    if (attrs[key] !== undefined) { parameters[name] = attrs[key]; delete attrs[key]; }
  }
  if (Object.keys(parameters).length) {
    const existing = attrs["gen_ai.request.parameters"];
    attrs["gen_ai.request.parameters"] = JSON.stringify({ ...(existing === undefined ? {} : { parameters: existing }), ...parameters });
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
  private readonly processor: SpanProcessor;
  private readonly ownsProcessor: boolean;
  private readonly sampler: TraceIdRatioBasedSampler;

  constructor(config: FISpanExporterConfig) {
    super();
    this.converter = new SpanConverter({
      format: "GenAI_v1_38_0",
      packageName: "@traceai/mastra",
      serviceName: config.serviceName,
      // Only `resourceAttributes` is read off this config by the converter.
      config: { resourceAttributes: snapshotData(config.resourceAttributes) } as any,
    });
    this.ownsProcessor = !config.spanProcessor;
    this.sampler = new TraceIdRatioBasedSampler(config.sampleRate ?? 1);
    if (config.spanProcessor) {
      this.processor = config.spanProcessor;
    } else {
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
  }

  protected async _exportTracingEvent(event: TracingEvent): Promise<void> {
    if (ownData(event, "type") !== "span_ended") return;
    const original = ownData(event, "exportedSpan");
    if (!original || typeof original !== "object") return;
    try {
      // Decisions are captured on live span creation, not recomputed with a root
      // sampler when an event happens to be delivered outside its transaction.
      // A bridge's native SDK span already reaches this very agent processor;
      // only that exact observed identity is omitted from conversion.
      const metadata = exportedSpanMetadata(original);
      if (!this.ownsProcessor && metadata?.native) return;
      // Mastra preserves some application objects (notably Dates) by reference.
      // Snapshot BEFORE the converter's first await: every upstream stringify,
      // coercion and enrichment must see only detached data, never user hooks.
      const span = snapshotMastraSpan(original);
      const identity = metadata?.context ?? samplingContext(span, this.sampler);
      if (!(identity.traceFlags & TraceFlags.SAMPLED)) return;
      const otelSpan = await this.converter.convertSpan(span);
      enrichSnapshot(otelSpan, span);
      // The agent's sanitizer reads trusted data slots rather than calling
      // arbitrary ReadableSpan methods/getters. Adapt the converter's plain
      // snapshot to the SDK shape without weakening that privacy boundary.
      Object.assign(otelSpan, {
        _spanContext: identity,
        spanContext: () => identity,
        _duration: otelSpan.duration,
        _droppedAttributesCount: otelSpan.droppedAttributesCount,
        _droppedEventsCount: otelSpan.droppedEventsCount,
        _droppedLinksCount: otelSpan.droppedLinksCount,
      });
      // Credential observation and process-wide scrubbing belong to the core
      // agent. Converted spans need no per-request secrets or synthetic onStart.
      this.processor.onEnd(otelSpan);
    } catch {
      // Conversion errors can contain application content or credentials. Do not
      // print them, and do not let even a throwing diagnostic logger escape.
      try { diag.debug("[@traceai/mastra] Span conversion/export failed"); } catch { /* Fail open. */ }
    }
  }

  async flush(): Promise<void> {
    if (this.ownsProcessor) {
      await this.processor.forceFlush();
      return;
    }
    // Collector failure must not change Mastra's own shutdown result or hang a
    // server's graceful shutdown. The agent still owns final transport cleanup.
    let timer: ReturnType<typeof setTimeout> | undefined;
    try {
      await Promise.race([
        Promise.resolve().then(() => this.processor.forceFlush()),
        new Promise<void>(resolve => { timer = setTimeout(resolve, 5000); timer.unref(); }),
      ]);
    } catch {
      try { diag.debug("[@traceai/mastra] Agent flush failed"); } catch { /* Fail open. */ }
    } finally { if (timer) clearTimeout(timer); }
  }

  async shutdown(): Promise<void> {
    // Mastra must never shut down the process-wide agent's shared processor.
    if (this.ownsProcessor) await this.processor.shutdown();
    else await this.flush();
  }
}
