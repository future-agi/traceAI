import {
  Attributes,
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
} from "@opentelemetry/sdk-trace-base";
import { OTLPTraceExporter as _OTLPGRPCTraceExporter } from "@opentelemetry/exporter-trace-otlp-grpc";
import {
  BasicTracerProvider,
  BatchSpanProcessor as OTelBatchSpanProcessor,
  SimpleSpanProcessor as OTelSimpleSpanProcessor,
  IdGenerator,
} from "@opentelemetry/sdk-trace-node";
import { Resource, resourceFromAttributes, detectResources, defaultResource } from "@opentelemetry/resources";
import { SemanticResourceAttributes } from "@opentelemetry/semantic-conventions";
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
const FI_COLLECTOR_PATH = "/tracer/observation-span/create_otel_span/";
const FI_CUSTOM_EVAL_CONFIG_CHECK_PATH = "/tracer/custom-eval-config/check_exists/";
const FI_CUSTOM_EVAL_TEMPLATE_CHECK_PATH = "/tracer/custom-eval-config/get_custom_eval_by_name/";

// Default gRPC endpoint  
const DEFAULT_FI_GRPC_COLLECTOR_BASE_URL = "https://grpc.futureagi.com:50051";

// Transport enum
export enum Transport {
  HTTP = "http",
  GRPC = "grpc",
}



interface FIHeaders {
  [key: string]: string;
}

interface HTTPSpanExporterOptions {
  endpoint: string; // This should be the full URL
  headers?: FIHeaders;
  verbose?: boolean;
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

// --- Custom HTTPSpanExporter ---
class HTTPSpanExporter implements SpanExporter {
  private readonly endpoint: string;
  private readonly headers: FIHeaders;
  private isShutdown = false;
  private verbose: boolean;

  constructor(options: HTTPSpanExporterOptions) {
    this.endpoint = options.endpoint; // Expects full endpoint from _normalizedEndpoint
    this.headers = {
      "Content-Type": "application/json",
      ...options.headers,
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

    const spansData = spans.map((span) => {
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

    fetch(this.endpoint, {
      method: "POST",
      headers: this.headers,
      body: JSON.stringify(spansData),
    })
      .then((response) => {
        if (response.ok) {
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

// --- Custom GRPCSpanExporter extending OTLP gRPC exporter ---
class GRPCSpanExporter extends _OTLPGRPCTraceExporter {
  private verbose: boolean;

  constructor(options: { endpoint: string; headers?: FIHeaders; verbose?: boolean; [key: string]: any }) {
    const { endpoint, headers = {}, verbose = false, ...restOptions } = options;
    
    const auth_header = headers;
    const lower_case_auth_header = auth_header 
      ? Object.fromEntries(Object.entries(auth_header).map(([k, v]) => [k.toLowerCase(), v]))
      : {};

    const metadata = new grpc.Metadata();
    Object.entries(lower_case_auth_header).forEach(([key, value]) => {
      metadata.set(key, value);
    });

    const grpcConfig = {
      url: endpoint,
      metadata: metadata,
      ...(endpoint.includes('localhost') || endpoint.includes('127.0.0.1') ? { 
        credentials: grpc.credentials.createInsecure() 
      } : {}),
      ...restOptions,
    };

    try {
      super(grpcConfig);
    } catch (error) {
      diag.error(`GRPCSpanExporter: Error initializing OTLP exporter:`, error);
      throw error;
    }
    
    this.verbose = verbose;
    
    if (this.verbose) {
      diag.info(`GRPCSpanExporter: Configured for endpoint: ${endpoint}`);
      diag.info(`GRPCSpanExporter: Authentication: ${Object.keys(lower_case_auth_header).length > 0 ? 'Enabled' : 'None'}`);
    }
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

function getEnvFiAuthHeader(): FIHeaders | undefined {
  const apiKey = getEnv("FI_API_KEY");
  const secretKey = getEnv("FI_SECRET_KEY");
  if (apiKey && secretKey) {
    // Use lowercase headers for gRPC compatibility
    return { "x-api-key": apiKey, "x-secret-key": secretKey };
  }
  return undefined;
}

function getEnvGrpcCollectorEndpoint(): string {
  let endpoint = getEnv("FI_GRPC_COLLECTOR_ENDPOINT") ?? 
                 getEnv("FI_GRPC_URL") ?? 
                 DEFAULT_FI_GRPC_COLLECTOR_BASE_URL;
                 
  // Remove http:// or https:// prefix if present (gRPC doesn't use HTTP protocol prefix)
  endpoint = endpoint.replace(/^https?:\/\//, '');
  
  return endpoint;
}

// This function now constructs the FULL endpoint URL
// It prioritizes the endpoint passed directly to `register` (if any),
// then checks FI_BASE_URL (or a more specific FI_COLLECTOR_ENDPOINT env var),
// and finally falls back to a default base URL.
function constructFullEndpoint(customEndpoint?: string): string {
  let baseUrlToUse: string;

  if (customEndpoint) {
    try {
      const parsedCustom = new URL(customEndpoint);
      if (!parsedCustom.protocol || !parsedCustom.host) {
        diag.warn(`Custom endpoint '${customEndpoint}' is missing protocol or host. Falling back to environment or default.`);
        baseUrlToUse = getEnv("FI_BASE_URL") ?? getEnv("FI_COLLECTOR_ENDPOINT") ?? DEFAULT_FI_COLLECTOR_BASE_URL;
      } else if (parsedCustom.pathname !== "/" && parsedCustom.pathname !== FI_COLLECTOR_PATH) {
        diag.warn(`Using custom endpoint as full URL: ${customEndpoint}`);
        return customEndpoint;
      } else {
        baseUrlToUse = `${parsedCustom.protocol}//${parsedCustom.host}`;
      }
    } catch (e) {
      diag.warn(`Custom endpoint '${customEndpoint}' is not a valid URL. Falling back to environment or default.`);
      baseUrlToUse = getEnv("FI_BASE_URL") ?? getEnv("FI_COLLECTOR_ENDPOINT") ?? DEFAULT_FI_COLLECTOR_BASE_URL;
    }
  } else {
    baseUrlToUse = getEnv("FI_BASE_URL") ?? getEnv("FI_COLLECTOR_ENDPOINT") ?? DEFAULT_FI_COLLECTOR_BASE_URL;
  }
  
  // Ensure no trailing slash from baseUrl before appending path
  if (baseUrlToUse.endsWith('/')) {
    baseUrlToUse = baseUrlToUse.slice(0, -1);
  }

  return `${baseUrlToUse}${FI_COLLECTOR_PATH}`;
}


// --- TracerProvider ---
interface FITracerProviderOptions {
  resource?: Resource;
  verbose?: boolean;
  idGenerator?: IdGenerator;
  endpoint?: string; // Custom endpoint (can be base or full, _constructFullEndpoint will handle)
  headers?: FIHeaders;
  transport?: Transport; // Transport type
}

class FITracerProvider extends BasicTracerProvider {
  private defaultProcessorAttached: boolean = false;
  private verbose: boolean;
  private endpoint: string; // This will store the fully constructed endpoint
  private headers?: FIHeaders;
  private transport: Transport;

  constructor(config: FITracerProviderOptions = {}) {
    const idGenerator = config.idGenerator ?? new UuidIdGenerator();
    const transport = config.transport ?? Transport.HTTP;
    
    const verbose = config.verbose ?? getEnv("FI_VERBOSE_PROVIDER")?.toLowerCase() === "true" ?? false;
    
    // Construct the appropriate endpoint based on transport
    let endpoint: string;
    if (transport === Transport.GRPC) {
      endpoint = config.endpoint ?? getEnvGrpcCollectorEndpoint();
    } else {
      endpoint = constructFullEndpoint(config.endpoint);
    }
    
    const headers = config.headers ?? getEnvFiAuthHeader();

    if (verbose) {
      diag.info(`FITracerProvider: Using ${transport.toUpperCase()} exporter endpoint: ${endpoint}`);
    }

    // Create the appropriate exporter
    let exporter: SpanExporter;
    if (transport === Transport.GRPC) {
      exporter = new GRPCSpanExporter({ endpoint, headers, verbose: config.verbose });
    } else {
      exporter = new HTTPSpanExporter({ endpoint, headers, verbose: config.verbose });
    }
    
    const defaultProcessor = new OTelSimpleSpanProcessor(exporter);
    super({ resource: config.resource, idGenerator, spanProcessors: [defaultProcessor] });
    this.defaultProcessorAttached = true;
    this.verbose = verbose;
    this.endpoint = endpoint;
    this.headers = headers;
    this.transport = transport;

    // Log to confirm processor and exporter details
    if (verbose) {
      diag.info(`FITracerProvider: Default SimpleSpanProcessor added with ${transport.toUpperCase()}SpanExporter targeting: ${endpoint}`);
    }

    if (verbose) {
      this.printTracingDetails();
    }
  }

  addSpanProcessor(spanProcessor: SpanProcessor): void {
    if (this.defaultProcessorAttached) {
      diag.warn(
        "Adding a new SpanProcessor. The default SimpleSpanProcessor will be replaced.",
      );
      (this as any)._registeredSpanProcessors = []; 
      this.defaultProcessorAttached = false; 
    }
    (this as any)._registeredSpanProcessors.push(spanProcessor);
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

    const processorName = this.defaultProcessorAttached ? "SimpleSpanProcessor (default)" : "Custom/Multiple";
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
  async shutdown(): Promise<void> {
    if (this.verbose) {
      diag.info("Shutting down FI TracerProvider...");
    }
    return super.shutdown();
  }
}

class SimpleSpanProcessor extends OTelSimpleSpanProcessor {}
class BatchSpanProcessor extends OTelBatchSpanProcessor {}

export interface RegisterOptions {
  projectName?: string;
  projectType?: ProjectType;
  projectVersionName?: string;
  evalTags?: EvalTag[];
  sessionName?: string;
  metadata?: Record<string, any>;
  batch?: boolean;
  setGlobalTracerProvider?: boolean;
  headers?: FIHeaders;
  verbose?: boolean;
  endpoint?: string; // Can be a base URL or a full URL. _constructFullEndpoint will resolve.
  idGenerator?: IdGenerator;
  transport?: Transport; // Transport type: HTTP or GRPC
}

function register(options: RegisterOptions = {}): FITracerProvider {
  const {
    projectName: optProjectName,
    projectType = ProjectType.EXPERIMENT,
    projectVersionName: optProjectVersionName,
    evalTags: optEvalTags = [],
    sessionName,
    metadata = {},
    batch = false,
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
      verbose
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
    [SemanticResourceAttributes.SERVICE_NAME]: projectName,
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

  const detected = detectResources();
  const resource = detected.merge(resourceFromAttributes(resourceAttributes));

  
  // Headers for the exporter
  const exporterHeaders = optHeaders ?? getEnvFiAuthHeader();
  // Endpoint for the exporter is now determined by FITracerProvider's constructor
  // using _constructFullEndpoint, which considers optEndpoint and env vars.

  const tracerProvider = new FITracerProvider({
    resource,
    verbose,
    idGenerator,
    endpoint: optEndpoint,
    headers: exporterHeaders,
    transport,
  });

  if (batch) {
    let batchExporter: SpanExporter;
    if (transport === Transport.GRPC) {
      batchExporter = new GRPCSpanExporter({
        endpoint: (tracerProvider as any).endpoint,
        headers: exporterHeaders,
        verbose: verbose
      });
    } else {
      batchExporter = new HTTPSpanExporter({
        endpoint: (tracerProvider as any).endpoint,
        headers: exporterHeaders,
        verbose: verbose
      });
    }
    const batchProcessor = new OTelBatchSpanProcessor(batchExporter);

    (tracerProvider as any)._registeredSpanProcessors = [];
    (tracerProvider as any)._defaultProcessorAttached = false;
    tracerProvider.addSpanProcessor(batchProcessor);
  }

  if (setGlobalTracerProvider) {
    trace.setGlobalTracerProvider(tracerProvider);
    if (verbose) {
      diag.info(
        "|  \n" +
        "|  `register` has set this TracerProvider as the global OpenTelemetry default.\n" +
        "|  To disable this behavior, call `register` with " +
        "`set_global_tracer_provider=false`.\n"
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

async function checkCustomEvalConfigExists(
  projectName: string,
  evalTags: any[], // Expects result of prepareEvalTags
  customEndpoint?: string, // Can be base or full URL for the API call itself
  verbose?: boolean
): Promise<boolean> {
  if (!evalTags || evalTags.length === 0) {
    return false;
  }

  let apiBaseUrl: string;
  if (customEndpoint) {
    try {
      const parsedCustom = new URL(customEndpoint);
      if (parsedCustom.pathname.endsWith(FI_CUSTOM_EVAL_CONFIG_CHECK_PATH)) {
        if (verbose) diag.info(`checkCustomEvalConfigExists: Using custom full endpoint: ${customEndpoint}`);
        apiBaseUrl = customEndpoint.substring(0, customEndpoint.lastIndexOf(FI_CUSTOM_EVAL_CONFIG_CHECK_PATH));
      } else if (parsedCustom.pathname === "/" || parsedCustom.pathname === "") { 
         apiBaseUrl = `${parsedCustom.protocol}//${parsedCustom.host}`;
      } else { 
         apiBaseUrl = customEndpoint; 
      }
    } catch (e) {
      if (verbose) diag.warn(`checkCustomEvalConfigExists: Custom endpoint '${customEndpoint}' is not a valid URL. Falling back to environment or default.`);
      apiBaseUrl = getEnv("FI_BASE_URL") ?? getEnv("FI_COLLECTOR_ENDPOINT") ?? DEFAULT_FI_COLLECTOR_BASE_URL;
    }
  } else {
    apiBaseUrl = getEnv("FI_BASE_URL") ?? getEnv("FI_COLLECTOR_ENDPOINT") ?? DEFAULT_FI_COLLECTOR_BASE_URL;
  }

  if (apiBaseUrl.endsWith('/')) {
    apiBaseUrl = apiBaseUrl.slice(0, -1);
  }
  const url = `${apiBaseUrl}${FI_CUSTOM_EVAL_CONFIG_CHECK_PATH}`;

  const headers: FIHeaders = {
    "Content-Type": "application/json",
    ...(getEnvFiAuthHeader() || {}),
  };

  const payload = {
    project_name: projectName,
    eval_tags: evalTags,
  };

  if (verbose) {
    diag.info(`checkCustomEvalConfigExists: Checking custom eval config at ${url} with payload:`, JSON.stringify(payload, null, 2));
  }

  try {
    const response = await fetch(url, {
      method: "POST",
      headers: headers,
      body: JSON.stringify(payload),
    });

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

  let apiBaseUrl: string;
  if (customEndpoint) {
    try {
      const parsedCustom = new URL(customEndpoint);
      if (parsedCustom.pathname.endsWith(FI_CUSTOM_EVAL_TEMPLATE_CHECK_PATH)) {
        if (verbose) diag.info(`checkCustomEvalTemplateExists: Using custom full endpoint: ${customEndpoint}`);
        apiBaseUrl = customEndpoint.substring(0, customEndpoint.lastIndexOf(FI_CUSTOM_EVAL_TEMPLATE_CHECK_PATH));
      } else if (parsedCustom.pathname === "/" || parsedCustom.pathname === "") { 
         apiBaseUrl = `${parsedCustom.protocol}//${parsedCustom.host}`;
      } else { 
         apiBaseUrl = customEndpoint; 
      }
    } catch (e) {
      if (verbose) diag.warn(`checkCustomEvalTemplateExists: Custom endpoint '${customEndpoint}' is not a valid URL. Falling back to environment or default.`);
      apiBaseUrl = getEnv("FI_BASE_URL") ?? getEnv("FI_COLLECTOR_ENDPOINT") ?? DEFAULT_FI_COLLECTOR_BASE_URL;
    }
  } else {
    apiBaseUrl = getEnv("FI_BASE_URL") ?? getEnv("FI_COLLECTOR_ENDPOINT") ?? DEFAULT_FI_COLLECTOR_BASE_URL;
  }

  if (apiBaseUrl.endsWith('/')) {
    apiBaseUrl = apiBaseUrl.slice(0, -1);
  }
  const url = `${apiBaseUrl}${FI_CUSTOM_EVAL_TEMPLATE_CHECK_PATH}`;

  const headers: FIHeaders = {
    "Content-Type": "application/json",
    ...(getEnvFiAuthHeader() || {}),
  };

  const payload = {
    eval_template_name: eval_template_name
  };

  if (verbose) {
    diag.info(`checkCustomEvalTemplateExists: Checking custom eval template at ${url} with payload:`, JSON.stringify(payload, null, 2));
  }

  try {
    const response = await fetch(url, {
      method: "POST",
      headers: headers,
      body: JSON.stringify(payload),
    });

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