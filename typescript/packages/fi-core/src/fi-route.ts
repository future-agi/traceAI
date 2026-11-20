/**
 * For @traceai/auto only; not a supported API. The auto package resolves its Future AGI base URL from
 * options, a config file, the environment or the default, so the endpoint it passes is the FI route by
 * that resolution, whatever host it names. Everything else is classified by its resolved endpoint.
 */
import type { SpanExporter } from "@opentelemetry/sdk-trace-base";
import { createSpanExporter } from "./otel";
import { declareFiRoute } from "./fi-route-registry";

export function createFiRouteSpanExporter(options: Parameters<typeof createSpanExporter>[0] = {}): SpanExporter {
  return createSpanExporter(declareFiRoute({ ...options }));
}
