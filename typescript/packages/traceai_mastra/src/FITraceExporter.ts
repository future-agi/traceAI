import { OTLPTraceExporter } from "@opentelemetry/exporter-trace-otlp-proto";
import { addFIAttributesToSpan } from "@traceai/vercel";
import { addFIAttributesToMastraSpan } from "./attributes.js";
import { addFIProjectResourceAttributeSpan } from "./utils.js";
import { ownData, snapshotReadableSpan } from "./snapshot.js";

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
    let filteredSpans: ReadableSpanFromExporter[] = [];
    const length = ownData(spans, "length");
    if (typeof length === "number" && length <= 4096) for (let i = 0; i < length; i++) {
      try {
        // Legacy enrichment also used to spread arbitrary SDK/application spans
        // and mutate their shared resources. Work only on detached data, before
        // either the Vercel adapter or the OTLP serializer sees the values.
        const span = snapshotReadableSpan(ownData(spans, String(i)));
        addFIProjectResourceAttributeSpan(span);
        addFIAttributesToSpan({
          ...span,
          instrumentationLibrary: { name: "@traceai/mastra" },
        } as any);
        addFIAttributesToMastraSpan(span);
        filteredSpans.push(span as any);
      } catch { /* Unsupported legacy carriers never execute their methods/getters. */ }
    }
    if (this.spanFilter) {
      filteredSpans = filteredSpans.filter(this.spanFilter);
    }
    super.export(filteredSpans, resultCallback);
  }
}