import type { Tracer } from "@opentelemetry/api";
import { BasicTracerProvider, InMemorySpanExporter, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { FITracer } from "../trace/trace-config/FITracer";
import { DROPPED_CONTENT_COUNT } from "../attribute-projection";

function check(value: boolean, id: string): void { if (!value) throw new Error(id); }
test("R-C1-NATIVE-LIMIT", async () => {
  const saved = { ...process.env };
  for (const key of Object.keys(process.env)) if (/^(?:FI_|OTEL_)/.test(key)) delete process.env[key];
  const exporter = new InMemorySpanExporter();
  const provider = new BasicTracerProvider({ spanLimits: { attributeCountLimit: 16 }, spanProcessors: [new SimpleSpanProcessor(exporter)] });
  try {
    const sdk = provider.getTracer("c1");
    // An API-only facade deliberately has no SDK getSpanLimits or private data.
    // It returns an actual SDK span, not a shim that assumes the desired layout.
    const facade: Tracer = { startSpan: sdk.startSpan.bind(sdk), startActiveSpan: sdk.startActiveSpan.bind(sdk) };
    const tracer = new FITracer({ tracer: facade });
    const span = tracer.startSpan("c1", { attributes: {
      "gen_ai.span.kind": "LLM",
      ...Object.fromEntries(Array.from({ length: 70 }, (_, i) => [`gen_ai.input.messages.${i}.message.content`, "ordinary words"])),
    } });
    span.setAttributes({ "gen_ai.usage.input_tokens": 101, "gen_ai.usage.output_tokens": 7,
      "gen_ai.usage.total_tokens": 108, "output.value": "answer" });
    span.end();
    await provider.forceFlush();
    const delivered = exporter.getFinishedSpans();
    check(delivered.length === 1, "R-C1-NATIVE-LIMIT");
    const attributes = delivered[0].attributes;
    check(attributes["gen_ai.usage.input_tokens"] === 101 && attributes["gen_ai.usage.output_tokens"] === 7 &&
      attributes["gen_ai.usage.total_tokens"] === 108 && attributes["output.value"] === "answer", "R-C1-NATIVE-LIMIT");
    check(Object.keys(attributes).length <= 16 && Number(attributes[DROPPED_CONTENT_COUNT]) > 0, "R-C1-NATIVE-LIMIT");
    check(delivered[0].droppedAttributesCount >= Number(attributes[DROPPED_CONTENT_COUNT]), "R-C1-NATIVE-LIMIT");
  } finally {
    await provider.shutdown();
    process.env = saved;
  }
});
