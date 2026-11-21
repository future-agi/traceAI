// Child process for ambientOtlpHeaders.test.ts: the application's own provider and exporter, with a caller
// header named x-api-key for its own collector, while FI keys are present in the environment.
import { trace } from "@opentelemetry/api";
import { BasicTracerProvider, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { OTLPTraceExporter } from "@opentelemetry/exporter-trace-otlp-http";
import * as auto from "../../src";
(async () => {
  const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(new OTLPTraceExporter({
    url: `${process.env.USER_TEST_BASE_URL}/v1/traces`, headers: { "x-api-key": "own-backend-key" } }))] });
  trace.setGlobalTracerProvider(provider);
  auto.init({ provider, configFile: "/does-not-exist/user-endpoint-config", instrumentations: [], forwardApplicationSpans: true } as any);
  trace.getTracer("user-endpoint").startSpan("one").end();
  await provider.forceFlush(); await auto.forceFlush(); await auto.shutdown(); await provider.shutdown();
})().catch(e => { console.error(String(e)); process.exitCode = 1; });
