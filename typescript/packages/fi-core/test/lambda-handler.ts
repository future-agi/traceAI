import type { APIGatewayProxyHandler } from "aws-lambda";
import { trace } from "@opentelemetry/api";
import { BatchSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { OTLPTraceExporter } from "@opentelemetry/exporter-trace-otlp-proto";
import { register, traceLambdaHandler, ProjectType, type FITracerProvider } from "@traceai/fi-core";

const apiKey = process.env.FI_API_KEY?.trim();
const secretKey = process.env.FI_SECRET_KEY?.trim();
const projectName = process.env.FI_PROJECT_NAME?.trim();
let provider: FITracerProvider | undefined;

if (apiKey && secretKey && projectName) {
  try {
    const exporter = new OTLPTraceExporter({
      url: process.env.OTEL_EXPORTER_OTLP_TRACES_ENDPOINT ?? "https://api.futureagi.com/tracer/v1/traces",
      headers: { "x-api-key": apiKey, "x-secret-key": secretKey },
      timeoutMillis: 1000,
    });
    provider = register({
      projectName,
      projectType: ProjectType.OBSERVE,
      spanProcessors: [new BatchSpanProcessor(exporter)],
    });
  } catch {
    console.warn("Lambda tracing is disabled: check your telemetry configuration.");
  }
}

const tracer = trace.getTracer("order-handler");
const applicationHandler: APIGatewayProxyHandler = async () => {
  return tracer.startActiveSpan("prepare-response", async span => {
    try {
      await Promise.resolve();
      return { statusCode: 200, body: JSON.stringify({ message: "Ready" }) };
    } finally {
      span.end();
    }
  });
};
export const handler: APIGatewayProxyHandler = traceLambdaHandler(applicationHandler, { provider });
