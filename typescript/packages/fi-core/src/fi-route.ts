/**
 * For @traceai/auto only; not a package export and not a supported API. The auto package resolves its Future AGI
 * base URL from options, a config file, the environment or the default, so the endpoint it passes is the FI route
 * by that resolution, whatever host it names. Everything else is classified by its resolved endpoint.
 *
 * The factory is handed to the auto package through a process-wide symbol rather than a package subpath. That
 * symbol is readable by any code in the process, so the factory itself is the guard: it accepts any endpoint
 * but only together with a complete, non-empty explicit key pair, and it never fills keys from the environment.
 * Code that can call it can therefore only send keys it already holds. The symbol name carries a protocol
 * version, so copies of this package that disagree about the factory do not silently cross-wire.
 */
import type { SpanExporter } from "@opentelemetry/sdk-trace-base";
import { createSpanExporter } from "./otel";
import { declareFiRoute } from "./fi-route-registry";
import { hasCompleteKeyPair } from "./fi-request-policy";

export const FI_ROUTE_FACTORY = Symbol.for("traceai.fi-core.fi-route-factory.v1");

/** The string-valued entries of `headers`, each read once, so what is checked is what is used. */
function snapshotHeaders(headers: unknown): Record<string, string> | undefined {
  if (typeof headers !== "object" || headers === null) return undefined;
  const snapshot: Record<string, string> = {};
  try { for (const name of Object.keys(headers)) { const value = (headers as Record<string, unknown>)[name]; if (typeof value === "string") snapshot[name] = value; } }
  catch { return undefined; }
  return snapshot;
}

export function createFiRouteSpanExporter(options: Parameters<typeof createSpanExporter>[0] = {}): SpanExporter {
  const copy = { ...options };
  const headers = snapshotHeaders(copy.headers);
  if (!headers || !hasCompleteKeyPair(headers)) {
    throw new Error("The Future AGI route needs an explicit complete key pair; it does not read them from the environment.");
  }
  return createSpanExporter(declareFiRoute({ ...copy, headers }));
}

// The first copy of this package loaded in the process provides the factory; it checks its own registry.
if (!Object.prototype.hasOwnProperty.call(globalThis, FI_ROUTE_FACTORY)) {
  Object.defineProperty(globalThis, FI_ROUTE_FACTORY, { value: createFiRouteSpanExporter, enumerable: false, writable: false, configurable: false });
}
