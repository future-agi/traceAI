import type { PipecatClient } from "@pipecat-ai/client-js";
import { WebTracerProvider } from "@opentelemetry/sdk-trace-web";
import { BatchSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { Resource } from "@opentelemetry/resources";
import { OTLPTraceExporter } from "@opentelemetry/exporter-trace-otlp-http";
import { observePipecatClient } from "@traceai/fi-instrumentation-pipecat/client";

// Call before connecting your existing Pipecat client.
export function traceClient(client: PipecatClient, collectorUrl: string): () => Promise<void> {
  const provider = new WebTracerProvider({
    resource: new Resource({ "service.name": "pipecat-web" }),
    spanProcessors: [new BatchSpanProcessor(new OTLPTraceExporter({ url: collectorUrl }))],
  });
  const detach = observePipecatClient(client, { tracer: provider.getTracer("pipecat-web") });
  return async () => {
    detach();
    await provider.shutdown();
  };
}
