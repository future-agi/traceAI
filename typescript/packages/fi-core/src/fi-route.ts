/**
 * For @traceai/auto only; not a package export and not a supported API. The auto package resolves its Future AGI
 * base URL from options, a config file, the environment or the default, so the endpoint it passes is the FI route
 * by that resolution, whatever host it names. Everything else is classified by its resolved endpoint.
 *
 * The factory is handed to the auto package through a process-wide symbol rather than a package subpath, so an
 * application cannot import it by name; nothing an application puts in its options can reach it either.
 */
import type { SpanExporter } from "@opentelemetry/sdk-trace-base";
import { createSpanExporter } from "./otel";
import { declareFiRoute } from "./fi-route-registry";

export const FI_ROUTE_FACTORY = Symbol.for("traceai.fi-core.fi-route-factory");

export function createFiRouteSpanExporter(options: Parameters<typeof createSpanExporter>[0] = {}): SpanExporter {
  return createSpanExporter(declareFiRoute({ ...options }));
}

// The first copy of this package loaded in the process provides the factory; it checks its own registry.
if (!Object.prototype.hasOwnProperty.call(globalThis, FI_ROUTE_FACTORY)) {
  Object.defineProperty(globalThis, FI_ROUTE_FACTORY, { value: createFiRouteSpanExporter, enumerable: false, writable: false, configurable: false });
}
