import {
  Attributes,
  context,
  createContextKey,
  diag,
  TraceFlags,
  trace,
  SpanContext,
  SpanStatus,
  SpanStatusCode,
} from "@opentelemetry/api";
import { ProjectType, EvalTag, prepareEvalTags } from "./fi_types";
import { ExportResult, ExportResultCode } from "@opentelemetry/core";
import {
  ReadableSpan,
  SpanExporter,
  SpanProcessor,
  Sampler,
} from "@opentelemetry/sdk-trace-base";
import { OTLPTraceExporter as _OTLPGRPCTraceExporter } from "@opentelemetry/exporter-trace-otlp-grpc";
import { OTLPTraceExporter as _OTLPHTTPTraceExporter } from "@opentelemetry/exporter-trace-otlp-http";
import {
  BasicTracerProvider,
  BatchSpanProcessor as OTelBatchSpanProcessor,
  SimpleSpanProcessor as OTelSimpleSpanProcessor,
  IdGenerator,
} from "@opentelemetry/sdk-trace-node";
import { Resource, ResourceDetector, resourceFromAttributes, detectResources, defaultResource, envDetector, hostDetector, osDetector, processDetector } from "@opentelemetry/resources";
import { privacyFilter, PrivacyOptions } from "./export-privacy";
import { connectGrpcDirect, isolateGrpcExporterHeaders, isolateHttpExporterHeaders } from "./otlp-header-isolation";
import { declareFiRoute, isFiRouteDeclared } from "./fi-route-registry";
import { isFiEndpoint, sameEndpointTarget, type EndpointTransport } from "./fi-endpoint";
import { assertDirectFetch, assertNoKeysInAmbientHeaders, fiKeyHeaderValues, decideFiRequest, hasCompleteKeyPair, isRedirect, metadataKeyValues, metadataStrings, FiKeyRefusal, hostClass, type FiDecision, type FiRequest } from "./fi-request-policy";
import { credentialSecrets } from "./privacy-runtime";
import { seedSecrets } from "./secrets";
import { DefaultBatchSpanProcessor, DefaultSimpleSpanProcessor, SpanProcessorRegistry } from "./spanProcessorRegistry";
import { AsyncLocalStorageContextManager } from "@opentelemetry/context-async-hooks";

const privacyDestinations = new WeakSet<SpanExporter>();
/** @internal True only for constructors in this core copy, never based on user properties. */
export function hasExportPrivacyBoundary(exporter: SpanExporter): boolean { return privacyDestinations.has(exporter); }
interface ExportPrivacyOptions {
  traceConfig?: PrivacyOptions;
  /** Routing attributes are added only to this destination's detached snapshot. */
  resourceAttributes?: Attributes;
}
function exportPrivacy(options: ExportPrivacyOptions, headers: FIHeaders = {}, configured: readonly string[] = []) {
  const credentials = credentialSecrets(headers, configured);
  seedSecrets(process.env, credentials);
  const filter = privacyFilter(options.traceConfig, credentials);
  return (spans: ReadableSpan[]) => spans.map(span => filter.span(span, [], options.resourceAttributes));
}
/**
 * Apply the existing FI serialization boundary to an integration-owned transport.
 * Opt-in only: never wrap a borrowed application destination indiscriminately.
 * Keeping this outside the batch processor means secrets observed after enqueue
 * are included, and the transport sees only detached, fail-closed snapshots.
 */
export function withExportPrivacy(exporter: SpanExporter, options: ExportPrivacyOptions & { headers?: FIHeaders } = {}): SpanExporter {
  const sanitize = exportPrivacy(options, options.headers);
  const guarded: SpanExporter = {
    export(spans, callback) {
      let copies: ReadableSpan[];
      try { copies = sanitize(spans); }
      catch { callback({ code: ExportResultCode.FAILED }); return; } // Never send raw on failure.
      exporter.export(copies, callback);
    },
    forceFlush: () => exporter.forceFlush?.() ?? Promise.resolve(),
    shutdown: () => exporter.shutdown(),
  };
  privacyDestinations.add(guarded);
  return guarded;
}
// The full process detector exports the command line (which can hold secrets passed as
// flags), the OS user name and the executable path. By default only the runtime is kept.
const runtimeDetector: ResourceDetector = {
  detect(config) {
    const attributes = processDetector.detect(config).attributes ?? {};
    return { attributes: Object.fromEntries(Object.entries(attributes).filter(([key]) => key.startsWith("process.runtime."))) };
  },
};
const OPT_IN_DETECTORS: Record<string, ResourceDetector> = { host: hostDetector, os: osDetector, process: processDetector };
// OTEL_NODE_RESOURCE_DETECTORS (the OpenTelemetry Node variable) opts in to host, os and
// process detection. OTEL_SERVICE_NAME and OTEL_RESOURCE_ATTRIBUTES are always applied.
function resourceDetectors(): ResourceDetector[] {
  const names = (process.env.OTEL_NODE_RESOURCE_DETECTORS ?? "").split(",").map((name) => name.trim().toLowerCase()).filter(Boolean);
  if (!names.length) return [envDetector, runtimeDetector];
  const chosen = names.includes("none") ? [] : names.includes("all") ? Object.keys(OPT_IN_DETECTORS) : names;
  return [envDetector, ...chosen.flatMap((name) => OPT_IN_DETECTORS[name] ?? [])];
}
function applicationResource(resource?: Resource): Resource {
  const application = resource && typeof (resource as any).getRawAttributes === "function" ? resource : resourceFromAttributes(resource?.attributes ?? {});
  return defaultResource().merge(detectResources({ detectors: resourceDetectors() })).merge(application);
}
import { v4 as uuidv4 } from "uuid"; // For UUID generation

// Import grpc for metadata handling
import * as grpc from "@grpc/grpc-js";

// --- Constants for Resource Attributes ---
export const PROJECT_NAME = "project_name";
export const PROJECT_TYPE = "project_type";
export const PROJECT_VERSION_NAME = "project_version_name";
export const PROJECT_VERSION_ID = "project_version_id";
export const EVAL_TAGS = "eval_tags";
export const METADATA = "metadata";
export const SESSION_NAME = "session_name";



// Default base URL if not overridden by environment or direct config
const DEFAULT_FI_COLLECTOR_BASE_URL = "https://api.futureagi.com";
const FI_COLLECTOR_PATH = "/tracer/v1/traces";
const FI_CUSTOM_EVAL_CONFIG_CHECK_PATH = "/tracer/custom-eval-config/check_exists/";
const FI_CUSTOM_EVAL_TEMPLATE_CHECK_PATH = "/tracer/custom-eval-config/get_custom_eval_by_name/";

// Default gRPC endpoint  
const DEFAULT_FI_GRPC_COLLECTOR_BASE_URL = "https://grpc.futureagi.com";

// Transport enum
export enum Transport {
  HTTP = "http",
  GRPC = "grpc",
}



interface FIHeaders {
  [key: string]: string;
}

interface HTTPSpanExporterOptions extends ExportPrivacyOptions {
  endpoint: string; // This should be the full URL
  headers?: FIHeaders;
  verbose?: boolean;
  /** Allows plaintext to a self-hosted collector that is not a Future AGI host; see FI_ALLOW_INSECURE_HTTP. */
  allowInsecureHttp?: boolean;
}

// --- Custom ID Generator (using UUIDs) ---
class UuidIdGenerator implements IdGenerator {
  generateTraceId(): string {
    return uuidv4().replace(/-/g, "");
  }
  generateSpanId(): string {
    return uuidv4().replace(/-/g, "").substring(0, 16);
  }
}

/**
 * @deprecated Use the standard OTLPTraceExporter from `@opentelemetry/exporter-trace-otlp-http` instead.
 * This custom exporter sent a proprietary JSON format. The SDK now uses standard OTLP HTTP protobuf.
 */
class HTTPSpanExporter implements SpanExporter {
  private readonly endpoint: string;
  private readonly headers: FIHeaders;
  private isShutdown = false;
  private verbose: boolean;
  private readonly sanitize: ReturnType<typeof exportPrivacy>;

  constructor(options: HTTPSpanExporterOptions) {
    const { headers } = decide({ target: options.endpoint, transport: "http", headers: options.headers, allowInsecureHttp: options.allowInsecureHttp });
    this.sanitize = exportPrivacy(options, headers);
    privacyDestinations.add(this);
    this.endpoint = options.endpoint; // Expects full endpoint from _normalizedEndpoint
    this.headers = {
      "Content-Type": "application/json",
      ...headers,
    };
    this.verbose = options.verbose ?? false;
  }

  private formatTraceId(traceId: string): string {
    return traceId; 
  }

  private formatSpanId(spanId: string): string {
    return spanId; 
  }

  private convertAttributes(attributes: Attributes | undefined): Record<string, any> {
    if (!attributes) {
      return {};
    }
    try {
      return JSON.parse(JSON.stringify(attributes));
    } catch (e) {
      diag.error(`HTTPSpanExporter: Error converting attributes: ${e}`);
      return {};
    }
  }

  private getSpanStatusName(status: SpanStatus): string {
    switch (status.code) {
      case SpanStatusCode.UNSET:
        return "UNSET";
      case SpanStatusCode.OK:
        return "OK";
      case SpanStatusCode.ERROR:
        return "ERROR";
      default:
        return "UNKNOWN";
    }
  }

  export(
    spans: ReadableSpan[],
    resultCallback: (result: ExportResult) => void,
  ): void {
    if (!spans || !resultCallback) {
      resultCallback?.({ code: ExportResultCode.FAILED });
      return;
    }
    if (this.isShutdown) {
      resultCallback({ code: ExportResultCode.FAILED });
      return;
    }

    const spansData = this.sanitize(spans).map((span) => {
      if (!span) return null;
      const spanContext = span.spanContext();
      if (!spanContext) {
        return null;
      }
      const parentSpanId = span.parentSpanContext?.spanId
        ? this.formatSpanId(span.parentSpanContext.spanId)
        : undefined;

      return {
        trace_id: this.formatTraceId(spanContext.traceId),
        span_id: this.formatSpanId(spanContext.spanId),
        name: span.name || "unknown-span",
        start_time: span.startTime?.[0] * 1e9 + span.startTime?.[1] || 0,
        end_time: span.endTime?.[0] * 1e9 + span.endTime?.[1] || 0,
        attributes: this.convertAttributes(span.attributes),
        events: span.events.map((event) => ({
          name: event.name,
          attributes: this.convertAttributes(event.attributes),
          timestamp: event.time[0] * 1e9 + event.time[1], 
        })),
        status: this.getSpanStatusName(span.status),
        parent_id: parentSpanId,
        project_name: span.resource?.attributes[PROJECT_NAME],
        project_type: span.resource?.attributes[PROJECT_TYPE],
        project_version_name: span.resource?.attributes[PROJECT_VERSION_NAME],
        project_version_id: span.resource?.attributes[PROJECT_VERSION_ID],
        latency: Math.floor(
          (span.endTime[0] * 1e9 +
            span.endTime[1] -
            (span.startTime[0] * 1e9 + span.startTime[1])) /
            1e6, 
        ),
        eval_tags: span.resource?.attributes[EVAL_TAGS], 
        metadata: span.resource?.attributes[METADATA], 
        session_name: span.resource?.attributes[SESSION_NAME],
      };
    }).filter(Boolean); 

    if (this.verbose) {
        diag.info("HTTPSpanExporter: Sending payload:", JSON.stringify(spansData, null, 2));
    }

    // A request that carries Future AGI keys is not routed through a proxy dispatcher (read now: the global one can change)
    // and does not follow a redirect.
    try { assertDirectFetch(this.endpoint, hasCompleteKeyPair(this.headers)); }
    catch (error) {
      diag.error(`HTTPSpanExporter: not sent: ${(error as Error).message}`);
      resultCallback({ code: ExportResultCode.FAILED });
      return;
    }
    fetch(this.endpoint, {
      method: "POST",
      headers: this.headers,
      body: JSON.stringify(spansData),
      redirect: "manual",
    })
      .then((response) => {
        if (isRedirect(response) && hasCompleteKeyPair(this.headers)) {
          diag.error(`HTTPSpanExporter: ${new FiKeyRefusal("FI_REDIRECT").message}`);
          resultCallback({ code: ExportResultCode.FAILED });
        } else if (response.ok) {
          resultCallback({ code: ExportResultCode.SUCCESS });
        } else {
          diag.error(
            `HTTPSpanExporter: Failed to export spans: ${response.status} ${response.statusText}`,
          );
          response.text().then(text => diag.error(`HTTPSpanExporter: Server response: ${text}`));
          resultCallback({ code: ExportResultCode.FAILED });
        }
      })
      .catch((errorCaught) => { 
        diag.error(`HTTPSpanExporter: Error exporting spans: ${errorCaught}`);
        resultCallback({ code: ExportResultCode.FAILED });
      });
  }

  async shutdown(): Promise<void> {
    this.isShutdown = true;
    return Promise.resolve();
  }

  async forceFlush?(): Promise<void> {
    return Promise.resolve();
  }
}

/** Every credential value in the caller's gRPC Metadata, repeated values included. */
function metadataSecrets(metadata: unknown): string[] {
  const found = new Set<string>();
  try {
    const source = metadata as { getMap?: () => Record<string, unknown>; get?: (name: string) => unknown[] } | undefined;
    for (const name of Object.keys(source?.getMap?.() ?? {}))
      for (const value of source?.get?.(name) ?? []) if (typeof value === "string") for (const secret of credentialSecrets({ [name]: value })) found.add(secret);
  } catch { /* An unreadable metadata object adds nothing. */ }
  return [...found];
}

// --- Custom GRPCSpanExporter extending OTLP gRPC exporter ---
class GRPCSpanExporter extends _OTLPGRPCTraceExporter {
  private verbose: boolean;
  private readonly sanitize: ReturnType<typeof exportPrivacy>;

  constructor(options: ExportPrivacyOptions & { endpoint?: string; url?: string; headers?: FIHeaders; verbose?: boolean; allowInsecureHttp?: boolean; [key: string]: any }) {
    const { endpoint: givenEndpoint, url: givenUrl, metadata: callerMetadata, credentials: callerCredentials, headers: callerHeaders,
      verbose = false, traceConfig, resourceAttributes, allowInsecureHttp, ...restOptions } = options;
    delete restOptions.fiEndpoint; // never an option: only the SDK's own FI route (fi-route.ts) declares an FI endpoint
    // The target is the one the options name (endpoint, or the standard url option), so the classification
    // below is made on the host that is dialled.
    const endpoint = resolveTarget({ endpoint: givenEndpoint, url: givenUrl }, true) ?? getEnvGrpcCollectorEndpoint();
    const callerStrings = metadataStrings(callerMetadata);
    const metadataKnown = metadataSecrets(callerMetadata);
    seedSecrets(process.env, metadataKnown);
    // One decision, on the target and the final outgoing set (the caller's metadata merged with the headers).
    const decision = decide({ target: endpoint, transport: "grpc", headers: callerHeaders, metadata: callerMetadata, credentials: callerCredentials, allowInsecureHttp, declaredFi: isFiRouteDeclared(options) });
    const { fiBound, metadata, credentials: channelCredentials } = decision;
    // An exporter for another collector merges the ambient OTLP headers: they are judged here, where the OTLP exporter reads them.
    if (!fiBound) assertNoKeysInAmbientHeaders([...fiKeyHeaderValues(callerHeaders, callerStrings), ...metadataKeyValues(callerMetadata)]);
    const headers = decision.headers ?? {};
    const lower_case_auth_header = Object.fromEntries(Object.entries(headers).map(([k, v]) => [k.toLowerCase(), v]));

    const grpcConfig = {
      ...restOptions,
      url: endpoint,
      metadata: metadata,
      ...(channelCredentials ? { credentials: channelCredentials } : {}),
    };

    try {
      super(grpcConfig);
    } catch (error) {
      diag.error(`GRPCSpanExporter: Error initializing OTLP exporter:`, error);
      throw error;
    }
    if (fiBound) isolateGrpcExporterHeaders(this, grpcConfig);
    // A plaintext channel that carries keys, loopback included, is built with proxies off: no environment can route it.
    if (decision.plaintextDirect) connectGrpcDirect(this);
    
    this.verbose = verbose;
    this.sanitize = exportPrivacy({ traceConfig, resourceAttributes }, { ...callerStrings, ...headers }, metadataKnown);
    privacyDestinations.add(this);

    if (this.verbose) {
      diag.info(`GRPCSpanExporter: Configured for endpoint: ${loggable(endpoint)}`);
      diag.info(`GRPCSpanExporter: Authentication: ${Object.keys(lower_case_auth_header).length > 0 ? 'Enabled' : 'None'}`);
    }
  }

  export(spans: ReadableSpan[], callback: (result: ExportResult) => void): void {
    super.export(this.sanitize(spans), callback);
  }

  async shutdown(): Promise<void> {
    if (this.verbose) {
      diag.info("GRPCSpanExporter: Shutting down...");
    }
    return super.shutdown();
  }
}

// --- Helper Functions ---
function getEnv(key: string, defaultValue?: string): string | undefined {
  return process.env[key] ?? defaultValue;
}

function getEnvVerbose(): boolean | undefined {
  const envVar = getEnv("FI_VERBOSE_EXPORTER");
  if (envVar) {
    return envVar.toLowerCase() === "true" || envVar === "1";
  }
  return undefined;
}

/**
 * The one target an exporter's options name. `endpoint` is this SDK's option and `url` the standard OTLP
 * exporter option; both may be given only when they name the same host and port, and the error names neither.
 */
function resolveTarget(options: { endpoint?: unknown; url?: unknown }, grpcTransport: boolean): string | undefined {
  const endpoint = typeof options.endpoint === "string" ? options.endpoint : undefined;
  const url = typeof options.url === "string" ? options.url : undefined;
  if (endpoint !== undefined && url !== undefined && endpoint !== url && !sameEndpointTarget(endpoint, url, grpcTransport ? "grpc" : "http")) {
    throw new Error("Conflicting exporter options: endpoint and url name different targets. Pass only one of them.");
  }
  return endpoint ?? url;
}
/**
 * The one decision for a request: see fi-request-policy.ts. The endpoint the FI environment selects is supplied here, so a
 * target that names it is Future AGI bound whatever its host.
 */
function decide(request: Omit<FiRequest, "envRoute">): FiDecision {
  return decideFiRequest({ ...request, envRoute: request.transport === "grpc" ? getEnvGrpcCollectorEndpoint() : constructFullEndpoint() });
}

function getEnvGrpcCollectorEndpoint(): string {
  // Kept as written: an http:// prefix is a plaintext request and goes through the same decision as any other target.
  return getEnv("FI_GRPC_COLLECTOR_ENDPOINT") ??
         getEnv("FI_GRPC_URL") ??
         DEFAULT_FI_GRPC_COLLECTOR_BASE_URL;
}

// Explicit endpoints retain their base-or-full URL behavior. Environment collector
// endpoints are full URLs; only FI_BASE_URL and the default are base origins.
/** An endpoint as it may be logged: scheme, host and path only, never userinfo or a query; an unparseable value is not echoed. */
function loggable(endpoint: string): string {
  try { const url = new URL(endpoint); return `${url.protocol}//${url.host}${url.pathname}`; } catch { return "(not a URL)"; }
}
/** The collector URL an HTTP exporter given `endpoint` dials: the endpoint itself when it is a usable URL, else the environment's or the default route. It carries no userinfo. */
export function resolveTraceEndpoint(endpoint?: string): string {
  const resolved = constructFullEndpoint(endpoint);
  try {
    const url = new URL(resolved);
    if (!url.username && !url.password) return resolved;
    url.username = ""; url.password = "";
    return url.toString();
  } catch { return resolved; }
}
/**
 * The scheme and authority of a URL as written (userinfo removed), so the host the exporter dials, and every
 * decision about it, is the host the configuration names rather than the one a URL parser rewrites it to.
 */
function writtenOrigin(text: string, parsed: URL): string {
  const match = /^([A-Za-z][A-Za-z0-9+.-]*):\/\/([\s\S]*)$/.exec(text.trim());
  // A backslash ends the authority only for the schemes in which a URL parser reads it as a separator.
  const authority = match === null ? undefined : (/^(?:https?|wss?|ftp|file)$/i.test(match[1]) ? /^[^/?#\\]*/ : /^[^/?#]*/).exec(match[2])?.[0];
  // An empty authority is refused, never re-read from the path (`https:///host/` and `https://\host/` would otherwise name the host "tracer").
  if (authority === "") throw new FiKeyRefusal("FI_EMPTY_AUTHORITY");
  return `${parsed.protocol}//${authority === undefined ? parsed.host : authority.slice(authority.lastIndexOf("@") + 1)}`;
}
function constructFullEndpoint(customEndpoint?: string): string {
  if (customEndpoint) {
    try {
      const parsedCustom = new URL(customEndpoint);
      if (!parsedCustom.protocol || !parsedCustom.host) {
        diag.warn(`Custom endpoint '${loggable(customEndpoint)}' is missing protocol or host. Falling back to environment or default.`);
      } else if (parsedCustom.pathname !== "/" && parsedCustom.pathname !== FI_COLLECTOR_PATH) {
        diag.warn(`Using custom endpoint as full URL: ${loggable(customEndpoint)}`);
        return customEndpoint;
      } else {
        return `${writtenOrigin(customEndpoint, parsedCustom)}${FI_COLLECTOR_PATH}`;
      }
    } catch (e) {
      if (e instanceof FiKeyRefusal) throw e;
      diag.warn(`Custom endpoint '${loggable(customEndpoint)}' is not a valid URL. Falling back to environment or default.`);
    }
  }

  // FI_TRACES_URL names the trace collector separately from the platform API in
  // FI_BASE_URL (self-hosted Docker installs serve them on different ports). It
  // is a base origin like FI_BASE_URL. Platform API calls keep using FI_BASE_URL.
  const tracesUrl = getEnv("FI_TRACES_URL")?.trim();
  if (tracesUrl) {
    return `${tracesUrl.replace(/\/$/, "")}${FI_COLLECTOR_PATH}`;
  }
  const baseUrl = getEnv("FI_BASE_URL");
  const collectorEndpoint = getEnv("FI_COLLECTOR_ENDPOINT");
  if (baseUrl === undefined && collectorEndpoint !== undefined) {
    return collectorEndpoint;
  }
  const baseUrlToUse = baseUrl ?? DEFAULT_FI_COLLECTOR_BASE_URL;
  return `${baseUrlToUse.replace(/\/$/, "")}${FI_COLLECTOR_PATH}`;
}


/** Standard OTLP HTTP serializer with the same S1 boundary as legacy/gRPC routes. */
class OTLPHTTPSpanExporter extends _OTLPHTTPTraceExporter {
  private readonly sanitize: ReturnType<typeof exportPrivacy>;
  constructor(options: ExportPrivacyOptions & { endpoint?: string; url?: string; headers?: FIHeaders; allowInsecureHttp?: boolean } = {}) {
    const named = resolveTarget(options, false);
    const url = constructFullEndpoint(named);
    const { headers, fiBound } = decide({ target: url, written: named, transport: "http", headers: options.headers, declaredFi: isFiRouteDeclared(options), allowInsecureHttp: options.allowInsecureHttp });
    // An exporter for another collector merges the ambient OTLP headers: they are judged here, where the OTLP exporter reads them.
    // (It sends with its own http.Agent, which does not consult a proxy environment.)
    if (!fiBound) assertNoKeysInAmbientHeaders(fiKeyHeaderValues(options.headers));
    super({ url, headers });
    if (fiBound) isolateHttpExporterHeaders(this, { url, headers }, headers);
    this.sanitize = exportPrivacy(options, headers);
    privacyDestinations.add(this);
  }
  export(spans: ReadableSpan[], callback: (result: ExportResult) => void): void {
    super.export(this.sanitize(spans), callback);
  }
}
/** Construct a privacy-protected FI exporter without registering a provider or doing I/O. */
export function createSpanExporter(options: ExportPrivacyOptions & {
  endpoint?: string;
  /** The standard OTLP exporter option; it names the target the same way `endpoint` does. */
  url?: string;
  headers?: FIHeaders;
  transport?: Transport;
  verbose?: boolean;
  /**
   * Allows FI keys to go in plaintext to a self-hosted collector that is not a Future AGI host and not loopback
   * (the environment's FI_ALLOW_INSECURE_HTTP=1 does the same). Never applies to a Future AGI host or through a proxy.
   */
  allowInsecureHttp?: boolean;
} = {}): SpanExporter {
  // Whether the endpoint is the FI route is the SDK's own wiring (see fi-route.ts), carried by the
  // identity of the options object; no property of a caller's options can say so.
  const declared = isFiRouteDeclared(options);
  const { fiEndpoint: _ignored, ...rest } = options as typeof options & { fiEndpoint?: unknown };
  const mark = <T extends object>(built: T): T => (declared ? declareFiRoute(built) : built);
  return options.transport === Transport.GRPC
    ? new GRPCSpanExporter(mark({ ...rest }))
    : new OTLPHTTPSpanExporter(mark({ ...rest }));
}

// --- TracerProvider ---
export interface FITracerProviderOptions extends ExportPrivacyOptions {
  /** Default false selects SimpleSpanProcessor; true selects BatchSpanProcessor. */
  batch?: boolean;
  resource?: Resource;
  verbose?: boolean;
  idGenerator?: IdGenerator;
  endpoint?: string; // Custom endpoint (can be base or full, _constructFullEndpoint will handle)
  url?: string; // The standard OTLP exporter option; names the target like `endpoint`
  headers?: FIHeaders;
  transport?: Transport; // Transport type
  sampler?: Sampler;
  spanProcessors?: SpanProcessor[];
  /** See `createSpanExporter`. */
  allowInsecureHttp?: boolean;
}

class FITracerProvider extends BasicTracerProvider {
  private readonly processorRegistry: SpanProcessorRegistry;
  private shutdownPromise?: Promise<void>;
  private defaultProcessorAttached: boolean = false;
  private readonly batch: boolean;
  private verbose: boolean;
  private endpoint: string; // This will store the fully constructed endpoint
  private headers?: FIHeaders;
  private transport: Transport;

  constructor(config: FITracerProviderOptions = {}) {
    const idGenerator = config.idGenerator ?? new UuidIdGenerator();
    const transport = config.transport ?? Transport.HTTP;
    
    const verbose = config.verbose ?? (getEnv("FI_VERBOSE_PROVIDER")?.toLowerCase() === "true");
    
    // Construct the appropriate endpoint based on transport
    let endpoint: string;
    const named = resolveTarget(config, transport === Transport.GRPC);
    if (transport === Transport.GRPC) {
      endpoint = named ?? getEnvGrpcCollectorEndpoint();
    } else {
      endpoint = constructFullEndpoint(named);
    }
    
    // The key decision belongs to the SDK exporter that carries the keys: with explicit span processors none is built.
    const headers = config.spanProcessors === undefined
      ? decide({ target: endpoint, written: named, transport: transport === Transport.GRPC ? "grpc" : "http", headers: config.headers, allowInsecureHttp: config.allowInsecureHttp }).headers
      : config.headers;

    if (verbose) {
      diag.info(`FITracerProvider: Using ${transport.toUpperCase()} exporter endpoint: ${loggable(endpoint)}`);
    }

    // Explicit lists, including [], never construct a hidden default exporter.
    let defaultProcessor: DefaultBatchSpanProcessor | DefaultSimpleSpanProcessor | undefined;
    if (config.spanProcessors === undefined) {
      // Resolve the original options, not an already-resolved full environment URL.
      const exporter = createSpanExporter({ ...config, transport, headers, allowInsecureHttp: config.allowInsecureHttp });
      defaultProcessor = config.batch
        ? new DefaultBatchSpanProcessor(exporter)
        : new DefaultSimpleSpanProcessor(exporter);
    }
    const processorRegistry = new SpanProcessorRegistry(
      config.spanProcessors ?? [],
      () => defaultProcessor?.disable(),
    );
    // Keep the actual SDK default at the top level for native adapters that borrow
    // its exporter, and for SDK-owned flush/shutdown of its accepted batch queue.
    super({ resource: applicationResource(config.resource), idGenerator, sampler: config.sampler,
      spanProcessors: defaultProcessor ? [defaultProcessor, processorRegistry] : [processorRegistry] });
    this.processorRegistry = processorRegistry;
    this.defaultProcessorAttached = config.spanProcessors === undefined;
    this.batch = config.batch ?? false;
    this.verbose = verbose;
    this.endpoint = endpoint;
    this.headers = headers;
    this.transport = transport;

    // Log to confirm processor and exporter details
    if (verbose) {
      diag.info(`FITracerProvider: ${defaultProcessor ? `Default ${this.batch ? "Batch" : "Simple"}SpanProcessor` : "Explicit span processors"} with ${transport.toUpperCase()}SpanExporter targeting: ${loggable(endpoint)}`);
    }

    if (verbose) {
      this.printTracingDetails();
    }
  }

  addSpanProcessor(spanProcessor: SpanProcessor): void {
    // First legacy add stops new default deliveries but retains its queued work.
    // Constructor processors are copied by the registry and are never replaced.
    this.processorRegistry.add(spanProcessor);
    this.defaultProcessorAttached = false;
  }

  private printTracingDetails(): void {
    const resource = (this as BasicTracerProvider as any).resource;
    if (!resource) {
      diag.warn("No resource available for tracing details");
      return;
    }

    const projectName = resource.attributes[PROJECT_NAME] || "N/A";
    const projectType = resource.attributes[PROJECT_TYPE];
    const projectVersionName = resource.attributes[PROJECT_VERSION_NAME] || "default";
    const evalTags = resource.attributes[EVAL_TAGS] || [];
    const sessionName = resource.attributes[SESSION_NAME] || "N/A";

    const processorName = this.defaultProcessorAttached ? `${this.batch ? "Batch" : "Simple"}SpanProcessor (default)` : "Custom/Multiple";
    const transportName = this.transport.toUpperCase(); 

    const detailsHeader =
      process.platform === "win32"
        ? "OpenTelemetry Tracing Details"
        : "🔭 OpenTelemetry Tracing Details 🔭";

    let detailsMsg = `${detailsHeader}\n`;
    detailsMsg += `|  FI Project: ${projectName}\n`;
    detailsMsg += `|  FI Project Type: ${projectType}\n`;
    detailsMsg += `|  FI Project Version Name: ${projectVersionName}\n`;
    detailsMsg += `|  Span Processor: ${processorName}\n`;
    detailsMsg += `|  Collector Endpoint: ${this.endpoint}\n`; // Now shows the full endpoint
    detailsMsg += `|  Transport: ${transportName}\n`;
    detailsMsg += `|  Transport Headers: ${this.headers ? Object.keys(this.headers).map(h => `${h}: ****`).join(', ') : 'None'}\n`;
    detailsMsg += `|  Eval Tags: ${typeof evalTags === 'string' ? evalTags : JSON.stringify(evalTags)}\n`;
    detailsMsg += `|  Session Name: ${sessionName}\n`;
    detailsMsg += "|  \n";
    if (this.defaultProcessorAttached) {
      detailsMsg += "|  Using a default SpanProcessor. `addSpanProcessor` will overwrite this default.\n";
    }
  }
  shutdown(): Promise<void> {
    if (!this.shutdownPromise) {
      if (this.verbose) {
        diag.info("Shutting down FI TracerProvider...");
      }
      this.shutdownPromise = super.shutdown();
    }
    return this.shutdownPromise;
  }
}

const CONTEXT_MANAGER_PROBE = createContextKey("traceai.context-manager-probe");

function ensureContextManager(): void {
  // OTel has no public getter for the installed manager. Test its behavior using
  // a private context key, without replacing an application's working manager.
  const probeContext = context.active().setValue(CONTEXT_MANAGER_PROBE, true);
  if (context.with(probeContext, () => context.active().getValue(CONTEXT_MANAGER_PROBE))) return;
  const manager = new AsyncLocalStorageContextManager().enable();
  if (!context.setGlobalContextManager(manager)) manager.disable();
  // A global manager may also serve other providers. Provider shutdown must not
  // disable it; the application owns teardown of global OTel state.
}

class SimpleSpanProcessor extends OTelSimpleSpanProcessor {}
class BatchSpanProcessor extends OTelBatchSpanProcessor {
  constructor(private readonly destination: SpanExporter, config?: ConstructorParameters<typeof OTelBatchSpanProcessor>[1]) { super(destination, config); }
  async forceFlush(): Promise<void> {
    await super.forceFlush();
    // SDK batching drains its own queue but does not flush a destination's
    // additional buffers. Preserve the public FI flush/lifecycle contract.
    await this.destination.forceFlush?.();
  }
}

export interface RegisterOptions extends ExportPrivacyOptions {
  /** See `createSpanExporter`: plaintext to a self-hosted collector that is not a Future AGI host. */
  allowInsecureHttp?: boolean;
  /** Preserve application service identity; FI project routing is separate. */
  resource?: Resource;
  projectName?: string;
  projectType?: ProjectType;
  projectVersionName?: string;
  evalTags?: EvalTag[];
  sessionName?: string;
  metadata?: Record<string, any>;
  /** Default false preserves manual SimpleSpanProcessor semantics. Auto explicitly opts into batching. */
  batch?: boolean;
  setGlobalTracerProvider?: boolean;
  headers?: FIHeaders;
  verbose?: boolean;
  endpoint?: string; // Can be a base URL or a full URL. _constructFullEndpoint will resolve.
  idGenerator?: IdGenerator;
  transport?: Transport; // Transport type: HTTP or GRPC
  /** Optional advanced SDK configuration; omitted values preserve existing defaults. */
  sampler?: Sampler;
  /** When provided, use these processors instead of constructing the default exporter. */
  spanProcessors?: SpanProcessor[];
}

function register(options: RegisterOptions = {}): FITracerProvider {
  const {
    projectName: optProjectName,
    projectType = ProjectType.EXPERIMENT,
    projectVersionName: optProjectVersionName,
    evalTags: optEvalTags = [],
    sessionName,
    metadata = {},
    setGlobalTracerProvider = true,
    headers: optHeaders,
    verbose = false,
    endpoint: optEndpoint,
    idGenerator = new UuidIdGenerator(),
    transport = Transport.HTTP,
  } = options;

  const preparedEvalTags = prepareEvalTags(optEvalTags);

  if (projectType === ProjectType.OBSERVE) {
    if (preparedEvalTags.length > 0) {
      throw new Error("Eval tags are not allowed for project type OBSERVE");
    }
    if (optProjectVersionName) {
      throw new Error(
        "Project Version Name not allowed for project type OBSERVE",
      );
    }
  }

  if (projectType === ProjectType.EXPERIMENT) {
    if (sessionName) {
      throw new Error(
        "Session name is not allowed for project type EXPERIMENT",
      );
    }
  }

const projectName = optProjectName ?? getEnv("FI_PROJECT_NAME");
const projectVersionName = optProjectVersionName ?? getEnv("FI_PROJECT_VERSION_NAME") ?? "DEFAULT";
const projectVersionId = uuidv4(); 

if (!projectName) {
  throw new Error("FI_PROJECT_NAME is not set");
}

  const customEvalNames = preparedEvalTags.map(tag => tag.custom_eval_name).filter(name => name && name.length > 0);
  if (customEvalNames.length !== new Set(customEvalNames).size) {
    throw new Error("Duplicate custom eval names are not allowed");
  }

  // Call checkCustomEvalConfigExists without await (fire-and-forget)
  // It will log an error internally if a config exists.
  if (preparedEvalTags.length > 0) {
    checkCustomEvalConfigExists(
      projectName,
      preparedEvalTags,
      optEndpoint,
      verbose,
      { allowInsecureHttp: options.allowInsecureHttp },
    ).then(customEvalConfigExists => {
      if (customEvalConfigExists) {
        // Log an error instead of throwing, as register has already returned.
        diag.error(
          `register: Custom eval configuration already exists for project '${projectName}'. ` +
          "The SDK will continue to initialize, but this may lead to unexpected behavior or duplicate configurations. " +
          "Please use a different project name or disable/modify the existing custom eval configuration."
        );
      }
    }).catch(error => {
        // Log any error from the check itself
        diag.error(`register: Error during background checkCustomEvalConfigExists for project '${projectName}': ${error}`);
    });
  }

  const resourceAttributes: Attributes = {
    [PROJECT_NAME]: projectName,
    [PROJECT_TYPE]: projectType,
    [PROJECT_VERSION_NAME]: projectVersionName,
    [PROJECT_VERSION_ID]: projectVersionId,
    [EVAL_TAGS]: JSON.stringify(preparedEvalTags),
    [METADATA]: JSON.stringify(metadata),
  };

  if (projectType === ProjectType.OBSERVE && sessionName) {
    resourceAttributes[SESSION_NAME] = sessionName;
  }

  const resource = (options.resource ?? resourceFromAttributes({})).merge(resourceFromAttributes(resourceAttributes));

  
  // Headers for the exporter
  // The provider resolves them against the endpoint it exports to.
  const exporterHeaders = optHeaders;
  // Endpoint for the exporter is now determined by FITracerProvider's constructor
  // using _constructFullEndpoint, which considers optEndpoint and env vars.

  const tracerProvider = new FITracerProvider({
    resource,
    verbose,
    idGenerator,
    endpoint: optEndpoint,
    headers: exporterHeaders,
    transport,
    sampler: options.sampler,
    batch: options.batch ?? false,
    spanProcessors: options.spanProcessors,
    traceConfig: options.traceConfig,
    resourceAttributes: options.resourceAttributes,
    allowInsecureHttp: options.allowInsecureHttp,
  });

  if (setGlobalTracerProvider) {
    if (trace.setGlobalTracerProvider(tracerProvider)) {
      ensureContextManager();
      if (verbose) diag.info("register: Installed the global FI tracer provider.");
    } else {
      diag.warn(
        "register: An OpenTelemetry tracer provider is already installed; it was not replaced. " +
        "Use the returned provider explicitly, or configure exporters on your application's SDK. " +
        "Use setGlobalTracerProvider: false for an intentionally isolated FI provider.",
      );
    }
  }

  return tracerProvider;
}

interface CheckExistsResponse {
  result?: {
    exists?: boolean;
  };
  // Add other fields if the API returns more
}

export interface CheckCustomEvalTemplateExistsResponse {
  result?: {
    isUserEvalTemplate?: boolean;
    evalTemplate?: any
  };
  // Add other fields if the API returns more
}

/** The Future AGI API base the FI environment (or the default) selects. */
function fiApiBase(): string {
  return getEnv("FI_BASE_URL") ?? getEnv("FI_COLLECTOR_ENDPOINT") ?? DEFAULT_FI_COLLECTOR_BASE_URL;
}
/**
 * The base URL an eval check is sent to. The check carries the FI keys, so it goes only to a Future AGI base:
 * a custom endpoint is used when it is one (a futureagi.com host, or the host FI_BASE_URL selects); any other
 * endpoint is the application's own collector, and the check goes to the base the FI environment selects.
 */
function evalCheckBase(name: string, checkPath: string, customEndpoint: string | undefined, verbose?: boolean): string {
  if (customEndpoint) {
    let candidate: string | undefined;
    try {
      const parsedCustom = new URL(customEndpoint);
      if (parsedCustom.pathname.endsWith(checkPath)) {
        if (verbose) diag.info(`${name}: Using custom full endpoint: ${hostClass(customEndpoint)}`);
        candidate = customEndpoint.substring(0, customEndpoint.lastIndexOf(checkPath));
      } else if (parsedCustom.pathname === "/" || parsedCustom.pathname === "") {
        candidate = writtenOrigin(customEndpoint, parsedCustom);
      } else {
        candidate = customEndpoint;
      }
    } catch (e) {
      if (verbose) diag.warn(`${name}: The custom endpoint is not a valid URL. Falling back to environment or default.`);
    }
    if (candidate !== undefined) {
      if (isFiEndpoint(candidate, "http") || sameEndpointTarget(candidate, fiApiBase(), "http")) return candidate;
      if (verbose) diag.info(`${name}: The endpoint is not a Future AGI base; checking the Future AGI base instead.`);
    }
  }
  return fiApiBase();
}

async function checkCustomEvalConfigExists(
  projectName: string,
  evalTags: any[], // Expects result of prepareEvalTags
  customEndpoint?: string, // Can be base or full URL for the API call itself
  verbose?: boolean,
  options: { allowInsecureHttp?: boolean; /** A caller-supplied undici dispatcher, which makes a plaintext request count as proxied when it is a proxy agent. */ dispatcher?: unknown } = {},
): Promise<boolean> {
  if (!evalTags || evalTags.length === 0) {
    return false;
  }

  let apiBaseUrl = evalCheckBase("checkCustomEvalConfigExists", FI_CUSTOM_EVAL_CONFIG_CHECK_PATH, customEndpoint, verbose);

  if (apiBaseUrl.endsWith('/')) {
    apiBaseUrl = apiBaseUrl.slice(0, -1);
  }
  const url = `${apiBaseUrl}${FI_CUSTOM_EVAL_CONFIG_CHECK_PATH}`;

  // The check goes only to a Future AGI base (see evalCheckBase); the decision is the same one every request gets.
  let keys: FIHeaders | undefined;
  try { keys = decide({ target: url, transport: "http", declaredFi: true, allowInsecureHttp: options.allowInsecureHttp, dispatcher: options.dispatcher }).headers; }
  catch (error) {
    diag.warn(`checkCustomEvalConfigExists: skipped: ${(error as Error).message}`);
    return false;
  }
  const headers: FIHeaders = {
    "Content-Type": "application/json",
    ...(keys || {}),
  };

  const payload = {
    project_name: projectName,
    eval_tags: evalTags,
  };

  if (verbose) {
    diag.info(`checkCustomEvalConfigExists: Checking custom eval config at ${url} with payload:`, JSON.stringify(payload, null, 2));
  }

  try {
    try { assertDirectFetch(url, hasCompleteKeyPair(headers), options.dispatcher); }
    catch (error) { diag.warn(`checkCustomEvalConfigExists: skipped: ${(error as Error).message}`); return false; }
    const response = await fetch(url, {
      ...(options.dispatcher ? { dispatcher: options.dispatcher as any } : {}),
      method: "POST",
      headers: headers,
      body: JSON.stringify(payload),
      redirect: "manual",
    });

    if (isRedirect(response) && hasCompleteKeyPair(headers)) {
      diag.error(`checkCustomEvalConfigExists: ${new FiKeyRefusal("FI_REDIRECT").message}`);
      return false;
    }
    if (!response.ok) {
      const errorText = await response.text();
      diag.error(
        `checkCustomEvalConfigExists: Failed to check custom eval config: ${response.status} ${response.statusText} - ${errorText}`,
      );
      return false; 
    }

    const result = await response.json() as CheckExistsResponse;
    if (verbose) {
        diag.info("checkCustomEvalConfigExists: Response from server:", JSON.stringify(result, null, 2));
    }
    return result?.result?.exists === true;
  } catch (error) {
    diag.error(`checkCustomEvalConfigExists: Error checking custom eval config: ${error}`);
    return false;
  }
}

export async function checkCustomEvalTemplateExists(
  eval_template_name: string,
  verbose?: boolean,
  customEndpoint?: string,
  options: { allowInsecureHttp?: boolean; /** A caller-supplied undici dispatcher, which makes a plaintext request count as proxied when it is a proxy agent. */ dispatcher?: unknown } = {},
): Promise<CheckCustomEvalTemplateExistsResponse> {
  if (!eval_template_name || eval_template_name.length === 0) {
    const response: CheckCustomEvalTemplateExistsResponse = {
      result: {
        isUserEvalTemplate: false,
        evalTemplate: null
      }
    }
    return response;
  }

  let apiBaseUrl = evalCheckBase("checkCustomEvalTemplateExists", FI_CUSTOM_EVAL_TEMPLATE_CHECK_PATH, customEndpoint, verbose);

  if (apiBaseUrl.endsWith('/')) {
    apiBaseUrl = apiBaseUrl.slice(0, -1);
  }
  const url = `${apiBaseUrl}${FI_CUSTOM_EVAL_TEMPLATE_CHECK_PATH}`;

  // The check goes only to a Future AGI base (see evalCheckBase); the decision is the same one every request gets.
  let keys: FIHeaders | undefined;
  try { keys = decide({ target: url, transport: "http", declaredFi: true, allowInsecureHttp: options.allowInsecureHttp, dispatcher: options.dispatcher }).headers; }
  catch (error) {
    diag.warn(`checkCustomEvalTemplateExists: skipped: ${(error as Error).message}`);
    return { result: { isUserEvalTemplate: false, evalTemplate: null } };
  }
  const headers: FIHeaders = {
    "Content-Type": "application/json",
    ...(keys || {}),
  };

  const payload = {
    eval_template_name: eval_template_name
  };

  if (verbose) {
    diag.info(`checkCustomEvalTemplateExists: Checking custom eval template at ${url} with payload:`, JSON.stringify(payload, null, 2));
  }

  try {
    try { assertDirectFetch(url, hasCompleteKeyPair(headers), options.dispatcher); }
    catch (error) {
      diag.warn(`checkCustomEvalTemplateExists: skipped: ${(error as Error).message}`);
      return { result: { isUserEvalTemplate: false, evalTemplate: null } };
    }
    const response = await fetch(url, {
      ...(options.dispatcher ? { dispatcher: options.dispatcher as any } : {}),
      method: "POST",
      headers: headers,
      body: JSON.stringify(payload),
      redirect: "manual",
    });

    if (isRedirect(response) && hasCompleteKeyPair(headers)) {
      diag.error(`checkCustomEvalTemplateExists: ${new FiKeyRefusal("FI_REDIRECT").message}`);
      return { result: { isUserEvalTemplate: false, evalTemplate: null } };
    }
    if (!response.ok) {
      const errorText = await response.text();
      diag.error(
        `checkCustomEvalTemplateExists: Failed to check custom eval template: ${response.status} ${response.statusText} - ${errorText}`,
      );
      return {
        result: {
          isUserEvalTemplate: false,
          evalTemplate: null
        }
      };
    }

    const result = await response.json() as CheckCustomEvalTemplateExistsResponse;
    if (verbose) {
        diag.info("checkCustomEvalTemplateExists: Response from server:", JSON.stringify(result, null, 2));
    }
    return result;
  } catch (error) {
    diag.error(`checkCustomEvalTemplateExists: Error checking custom eval template: ${error}`);
    return {
      result: {
        isUserEvalTemplate: false,
        evalTemplate: null
      }
    };
  }
}

export {
  register,
  FITracerProvider,
  SimpleSpanProcessor,
  BatchSpanProcessor,
  HTTPSpanExporter,
  GRPCSpanExporter,
  UuidIdGenerator,
  checkCustomEvalConfigExists
}


// TODO:
// - Implement prepareEvalTags (similar to Python)
// - Implement checkCustomEvalConfigExists
// - Refine error handling and logging
// - Add tests 