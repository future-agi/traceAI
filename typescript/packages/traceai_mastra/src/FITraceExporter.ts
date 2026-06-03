import { OTLPTraceExporter } from "@opentelemetry/exporter-trace-otlp-proto";
import { addFIAttributesToSpan } from "@traceai/vercel";
import { addFIAttributesToMastraSpan } from "./attributes.js";
import { addFIProjectResourceAttributeSpan } from "./utils.js";

type ReadableSpanFromExporter =
  Parameters<OTLPTraceExporter["export"]>[0][number];

type ConstructorArgs = {
  spanFilter?: (span: ReadableSpanFromExporter) => boolean;
} & NonNullable<ConstructorParameters<typeof OTLPTraceExporter>[0]>;

/**
 * @deprecated Mastra v0.x only. Mastra v1 (>= 1.16) removed the `telemetry`
 * config key this exporter plugs into, so it no longer receives spans. For
 * Mastra v1 use {@link createFIObservability} / {@link createFIMastraExporter}.
 */
export class FITraceExporter extends OTLPTraceExporter {
  private readonly spanFilter?: (span: ReadableSpanFromExporter) => boolean;

  constructor({ spanFilter, ...args }: ConstructorArgs) {
    super({
      ...args,
    });
    this.spanFilter = spanFilter;
  }
  export(
    ...args: Parameters<OTLPTraceExporter["export"]>
  ) {
    const spans = args[0];
    const resultCallback = args[1];
    let filteredSpans = spans.map((span) => {
      addFIProjectResourceAttributeSpan(span as any);
      addFIAttributesToSpan({
        ...(span as any),
        // backwards compatibility with older versions of sdk-trace-base
        instrumentationLibrary: {
          name: "@traceai/mastra",
        },
      });
      addFIAttributesToMastraSpan(span as any);
      return span;
    });
    if (this.spanFilter) {
      filteredSpans = filteredSpans.filter(this.spanFilter);
    }
    super.export(filteredSpans, resultCallback);
  }
}