/**
 * Exporters bound for the Future AGI endpoint send the headers the SDK sets plus any the
 * caller passes in explicitly. The OTLP exporters would also merge OTEL_EXPORTER_OTLP_HEADERS and
 * OTEL_EXPORTER_OTLP_TRACES_HEADERS into every request; this module rebuilds the exporter's
 * delegate with a header provider that carries only those headers, for that instance only.
 * process.env and every other exporter are untouched.
 *
 * The rebuild uses the exporter packages' own configuration functions (everything but headers
 * is resolved exactly as before) and checks the layout it relies on. When the installed exporter
 * does not have that layout it refuses to construct, so nothing is sent through a header
 * provider whose contents cannot be pinned.
 */
import * as grpc from "@grpc/grpc-js";
import { parseKeyPairsIntoRecord } from "@opentelemetry/core";
import { convertLegacyHttpOptions, createOtlpHttpExportDelegate } from "@opentelemetry/otlp-exporter-base/node-http";
import { convertLegacyOtlpGrpcOptions, createOtlpGrpcExportDelegate } from "@opentelemetry/otlp-grpc-exporter-base";
import { JsonTraceSerializer, ProtobufTraceSerializer } from "@opentelemetry/otlp-transformer";

/** Version of the installed HTTP trace exporter, for the User-Agent it sets itself; absent when it cannot be read. */
function installedExporterVersion(): string | undefined {
  try {
    if (typeof require !== "function") return undefined;
    const version = require("@opentelemetry/exporter-trace-otlp-http/package.json")?.version;
    return typeof version === "string" && /^[\w.+-]+$/.test(version) ? version : undefined;
  } catch { return undefined; }
}
/** Headers the OTLP HTTP exporter always sets itself (the JSON trace exporter's own set). */
const exporterVersion = installedExporterVersion();
const HTTP_REQUIRED: Record<string, string> = {
  "User-Agent": exporterVersion ? `OTel-OTLP-Exporter-JavaScript/${exporterVersion}` : "OTel-OTLP-Exporter-JavaScript",
  "Content-Type": "application/json",
};
/** Thrown without header names or values. */
export const UNSUPPORTED_EXPORTER_LAYOUT = "Unsupported OTLP exporter layout: the exporter's header configuration cannot be pinned, so no exporter was created. Use a supported @opentelemetry/exporter-trace-otlp package version.";

function refuse(): never {
  throw new Error(UNSUPPORTED_EXPORTER_LAYOUT);
}

function envHeaderNames(): Set<string> {
  const names = new Set<string>();
  for (const key of ["OTEL_EXPORTER_OTLP_HEADERS", "OTEL_EXPORTER_OTLP_TRACES_HEADERS"]) {
    for (const name of Object.keys(parseKeyPairsIntoRecord(process.env[key]?.trim()))) names.add(name);
  }
  return names;
}

/**
 * Only an object that is provably a jest stand-in for the exporter may skip isolation: a plain mock
 * object (no `_delegate`) built while running under jest. Whatever else reaches here is treated as an
 * OTLP exporter and must pass the layout check, whichever copy of the exporter base class it was
 * built from; outside jest nothing is skipped.
 */
function isJestStandIn(exporter: any): boolean {
  if (process.env.JEST_WORKER_ID === undefined) return false;
  return exporter === null || typeof exporter !== "object" || !("_delegate" in exporter);
}

function checkDelegate(exporter: any): void {
  const delegate = exporter._delegate;
  if (!delegate || typeof delegate.export !== "function" || typeof delegate.forceFlush !== "function" || typeof delegate.shutdown !== "function") refuse();
}

/**
 * Replace the delegate of a freshly constructed OTLP HTTP exporter. `config` is what was passed
 * to its constructor; `explicit` are the headers to send besides the exporter's own.
 */
export function isolateHttpExporterHeaders(exporter: unknown, config: { url?: string; [key: string]: unknown }, explicit: Record<string, string> | undefined): void {
  try {
    if (isJestStandIn(exporter)) return;
    checkDelegate(exporter);
    if (typeof convertLegacyHttpOptions !== "function" || typeof createOtlpHttpExportDelegate !== "function" || !JsonTraceSerializer) refuse();
    const resolved = convertLegacyHttpOptions(config, "TRACES", "v1/traces", { ...HTTP_REQUIRED }) as { headers?: unknown };
    if (typeof resolved?.headers !== "function") refuse();
    const probe = (resolved.headers as () => Record<string, unknown>)();
    if (!probe || typeof probe !== "object") refuse();
    const own = Object.fromEntries(Object.entries(explicit ?? {}).filter(([, value]) => value !== undefined).map(([name, value]) => [name, String(value)]));
    // Every header of the merged result must be one we can account for.
    const accounted = new Set([...Object.keys(own), ...Object.keys(HTTP_REQUIRED), ...envHeaderNames()]);
    for (const name of Object.keys(probe)) if (!accounted.has(name)) refuse();
    // The exporter's own headers win over the caller's, as in the exporter's merge.
    (exporter as any)._delegate = createOtlpHttpExportDelegate({ ...resolved, headers: () => ({ ...own, ...HTTP_REQUIRED }) }, JsonTraceSerializer);
  } catch (error) {
    if (error instanceof Error && error.message === UNSUPPORTED_EXPORTER_LAYOUT) throw error;
    refuse();
  }
}

/**
 * The same for an OTLP gRPC exporter. `config` is its constructor argument, whose `metadata`
 * holds the headers to send besides the exporter's own.
 */
export function isolateGrpcExporterHeaders(exporter: unknown, config: { metadata?: grpc.Metadata; [key: string]: unknown }): void {
  try {
    if (isJestStandIn(exporter)) return;
    checkDelegate(exporter);
    if (typeof convertLegacyOtlpGrpcOptions !== "function" || typeof createOtlpGrpcExportDelegate !== "function" || !ProtobufTraceSerializer) refuse();
    const resolved = convertLegacyOtlpGrpcOptions(config as any, "TRACES") as { metadata?: unknown };
    if (typeof resolved?.metadata !== "function") refuse();
    const probe = (resolved.metadata as () => any)();
    if (!probe || typeof probe.getMap !== "function" || typeof probe.get !== "function") refuse();
    const own = new Set(Object.keys(config.metadata?.getMap() ?? {}).map(name => name.toLowerCase()));
    const env = new Set([...envHeaderNames()].map(name => name.toLowerCase()));
    const kept = new Set([...own, "user-agent"]);
    const clean = new grpc.Metadata();
    for (const name of Object.keys(probe.getMap())) {
      if (kept.has(name)) { for (const value of probe.get(name)) clean.add(name, value); }
      else if (!env.has(name)) refuse(); // not ours, not from the environment: unknown layout
    }
    (exporter as any)._delegate = createOtlpGrpcExportDelegate({ ...(resolved as any), metadata: () => clean.clone() } as any, ProtobufTraceSerializer, "TraceExportService", "/opentelemetry.proto.collector.trace.v1.TraceService/Export");
  } catch (error) {
    if (error instanceof Error && error.message === UNSUPPORTED_EXPORTER_LAYOUT) throw error;
    refuse();
  }
}
