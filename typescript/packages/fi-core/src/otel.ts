import {
  Attributes,
  diag,
  TraceFlags,
  trace,
  SpanContext,
  SpanStatus,
  SpanStatusCode,
} from "@opentelemetry/api";
import { ExportResult, ExportResultCode } from "@opentelemetry/core";
import {
  ReadableSpan,
  SpanExporter,
  SpanProcessor,
} from "@opentelemetry/sdk-trace-base";
import {
  BasicTracerProvider,
  BatchSpanProcessor as OTelBatchSpanProcessor,
  SimpleSpanProcessor as OTelSimpleSpanProcessor,
  IdGenerator,
} from "@opentelemetry/sdk-trace-node";
import { Resource } from "@opentelemetry/resources";
import { SemanticResourceAttributes } from "@opentelemetry/semantic-conventions";
import { v4 as uuidv4 } from "uuid"; // For UUID generation

// --- Constants for Resource Attributes ---
export const PROJECT_NAME = "project_name";
export const PROJECT_TYPE = "project_type";
export const PROJECT_VERSION_NAME = "project_version_name";
export const PROJECT_VERSION_ID = "project_version_id";
export const EVAL_TAGS = "eval_tags";
export const METADATA = "metadata";
export const SESSION_NAME = "session_name";

// --- Enums ---
export enum ProjectType {
  EXPERIMENT = "experiment",
  OBSERVE = "observe",
}

// Default base URL if not overridden by environment or direct config
const DEFAULT_FI_COLLECTOR_BASE_URL = "https://api.futureagi.com";
const FI_COLLECTOR_PATH = "/tracer/observation-span/create_otel_span/";

// --- Interfaces ---
export interface EvalTag {
  custom_eval_name: string;
  score?: number;
  value?: string | number | boolean | null;
  rationale?: string | null;
  metadata?: Record<string, any> | null;
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
  private _isShutdown = false;
  private _verbose: boolean;

  constructor(options: HTTPSpanExporterOptions) {
    this.endpoint = options.endpoint; // Expects full endpoint from _normalizedEndpoint
    this.headers = {
      "Content-Type": "application/json",
      ...options.headers,
    };
    this._verbose = options.verbose ?? false;
  }

  private _formatTraceId(traceId: string): string {
    return traceId; 
  }

  private _formatSpanId(spanId: string): string {
    return spanId; 
  }

  private _convertAttributes(attributes: Attributes | undefined): Record<string, any> {
    if (!attributes) {
      return {};
    }
    return JSON.parse(JSON.stringify(attributes));
  }

  private _getSpanStatusName(status: SpanStatus): string {
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
    if (this._isShutdown) {
      resultCallback({ code: ExportResultCode.FAILED });
      return;
    }

    const spansData = spans.map((span) => {
      const parentSpanId = span.parentSpanId
        ? this._formatSpanId(span.parentSpanId)
        : undefined;

      return {
        trace_id: this._formatTraceId(span.spanContext().traceId),
        span_id: this._formatSpanId(span.spanContext().spanId),
        name: span.name,
        start_time: span.startTime[0] * 1e9 + span.startTime[1], 
        end_time: span.endTime[0] * 1e9 + span.endTime[1], 
        attributes: this._convertAttributes(span.attributes),
        events: span.events.map((event) => ({
          name: event.name,
          attributes: this._convertAttributes(event.attributes),
          timestamp: event.time[0] * 1e9 + event.time[1], 
        })),
        status: this._getSpanStatusName(span.status),
        parent_id: parentSpanId,
        project_name: span.resource.attributes[PROJECT_NAME],
        project_type: span.resource.attributes[PROJECT_TYPE],
        project_version_name: span.resource.attributes[PROJECT_VERSION_NAME],
        project_version_id: span.resource.attributes[PROJECT_VERSION_ID],
        latency: Math.floor(
          (span.endTime[0] * 1e9 +
            span.endTime[1] -
            (span.startTime[0] * 1e9 + span.startTime[1])) /
            1e6, 
        ),
        eval_tags: span.resource.attributes[EVAL_TAGS], 
        metadata: span.resource.attributes[METADATA], 
        session_name: span.resource.attributes[SESSION_NAME],
      };
    });

    if (this._verbose) {
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
    this._isShutdown = true;
    return Promise.resolve();
  }

  async forceFlush?(): Promise<void> {
    return Promise.resolve();
  }
}

// --- Helper Functions ---
function _getEnv(key: string, defaultValue?: string): string | undefined {
  return process.env[key] ?? defaultValue;
}

function _getEnvVerbose(): boolean | undefined {
  const envVar = _getEnv("FI_VERBOSE_EXPORTER");
  if (envVar) {
    return envVar.toLowerCase() === "true" || envVar === "1";
  }
  return undefined;
}

function _getEnvFiAuthHeader(): FIHeaders | undefined {
  const apiKey = _getEnv("FI_API_KEY");
  const secretKey = _getEnv("FI_SECRET_KEY");
  if (apiKey && secretKey) {
    // Match Python SDK: use X-Api-Key and X-Secret-Key headers
    return { "X-Api-Key": apiKey, "X-Secret-Key": secretKey };
  }
  // Remove the FI_TOKEN logic for now, as it's not in the Python snippet provided
  // const token = _getEnv("FI_TOKEN");
  // if (token) {
  //   return { Authorization: `Bearer ${token}` };
  // }
  return undefined;
}

// This function now constructs the FULL endpoint URL
// It prioritizes the endpoint passed directly to `register` (if any),
// then checks FI_BASE_URL (or a more specific FI_COLLECTOR_ENDPOINT env var),
// and finally falls back to a default base URL.
function _constructFullEndpoint(customEndpoint?: string): string {
  let baseUrlToUse: string;

  if (customEndpoint) {
    // If an endpoint is directly passed (e.g. to register function),
    // it might be a full URL or just a base. We need to be careful.
    // For now, let's assume if it's passed, it's intended as the base or full path.
    // A more robust logic might check if it already contains the FI_COLLECTOR_PATH.
    try {
      const parsedCustom = new URL(customEndpoint);
      if (parsedCustom.pathname !== "/" && parsedCustom.pathname !== FI_COLLECTOR_PATH) {
        // If it has a path that isn't just '/' and isn't already the target path, use as is (assume full URL)
         diag.warn(`Using custom endpoint as full URL: ${customEndpoint}`);
        return customEndpoint;
      } else {
        // It's a base URL or a URL ending in '/' or the exact collector path
        baseUrlToUse = `${parsedCustom.protocol}//${parsedCustom.host}`;
      }
    } catch (e) {
      diag.warn(`Custom endpoint \'${customEndpoint}\' is not a valid URL. Falling back to environment or default.`);
      baseUrlToUse = _getEnv("FI_BASE_URL") ?? _getEnv("FI_COLLECTOR_ENDPOINT") ?? DEFAULT_FI_COLLECTOR_BASE_URL;
    }
  } else {
    // No custom endpoint passed, use environment variable or default
    baseUrlToUse = _getEnv("FI_BASE_URL") ?? _getEnv("FI_COLLECTOR_ENDPOINT") ?? DEFAULT_FI_COLLECTOR_BASE_URL;
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
}

class FITracerProvider extends BasicTracerProvider {
  private _defaultProcessorAttached: boolean = false;
  private _verbose: boolean;
  private _endpoint: string; // This will store the fully constructed endpoint
  private _headers?: FIHeaders;

  constructor(config: FITracerProviderOptions = {}) {
    const idGenerator = config.idGenerator ?? new UuidIdGenerator();
    super({ resource: config.resource, idGenerator });
    this._verbose = config.verbose ?? _getEnv("FI_VERBOSE_PROVIDER")?.toLowerCase() === "true" ?? false; // Allow provider verbosity via env
    // Construct the full endpoint using the new logic
    this._endpoint = _constructFullEndpoint(config.endpoint);
    this._headers = config.headers ?? _getEnvFiAuthHeader();

    if (this._verbose) {
      diag.info(`FITracerProvider: Using full exporter endpoint: ${this._endpoint}`); // Use diag.info
    }

    // Pass the provider's verbosity to the exporter if not explicitly set for exporter
    const exporterVerbose = config.verbose; // We won't directly use FI_VERBOSE_EXPORTER here, HTTPSpanExporter handles its own env var.
                                         // If FITracerProvider is verbose, its default exporter will be too, unless HTTPSpanExporter's option/env says otherwise.

    const exporter = new HTTPSpanExporter({ endpoint: this._endpoint, headers: this._headers, verbose: exporterVerbose });
    const defaultProcessor = new OTelSimpleSpanProcessor(exporter);
    super.addSpanProcessor(defaultProcessor);
    this._defaultProcessorAttached = true;
    // Log to confirm processor and exporter details
    if (this._verbose) {
      diag.info(`FITracerProvider: Default SimpleSpanProcessor added with HTTPSpanExporter targeting: ${this._endpoint}`);
    }

    if (this._verbose) {
      this._printTracingDetails();
    }
  }

  addSpanProcessor(spanProcessor: SpanProcessor): void {
    if (this._defaultProcessorAttached) {
      diag.warn(
        "Adding a new SpanProcessor. The default SimpleSpanProcessor will be replaced.",
      );
      (this as any)._registeredSpanProcessors = []; 
      this._defaultProcessorAttached = false; 
    }
    super.addSpanProcessor(spanProcessor);
  }

  private _printTracingDetails(): void {
    const resource = this.resource;
    const projectName = resource.attributes[PROJECT_NAME] || "N/A";
    const projectType = resource.attributes[PROJECT_TYPE] || "N/A";
    const projectVersionName = resource.attributes[PROJECT_VERSION_NAME] || "N/A";
    const evalTags = resource.attributes[EVAL_TAGS] || "N/A";
    const sessionName = resource.attributes[SESSION_NAME] || "N/A";

    const processorName = this._defaultProcessorAttached ? "SimpleSpanProcessor (default)" : "Custom/Multiple";
    const transport = "HTTP"; 

    const detailsHeader =
      process.platform === "win32"
        ? "OpenTelemetry Tracing Details"
        : "🔭 OpenTelemetry Tracing Details 🔭";

    let detailsMsg = `${detailsHeader}\n`;
    detailsMsg += `|  FI Project: ${projectName}\n`;
    detailsMsg += `|  FI Project Type: ${projectType}\n`;
    detailsMsg += `|  FI Project Version Name: ${projectVersionName}\n`;
    detailsMsg += `|  Span Processor: ${processorName}\n`;
    detailsMsg += `|  Collector Endpoint: ${this._endpoint}\n`; // Now shows the full endpoint
    detailsMsg += `|  Transport: ${transport}\n`;
    detailsMsg += `|  Transport Headers: ${this._headers ? Object.keys(this._headers).map(h => `${h}: ****`).join(', ') : 'None'}\n`;
    detailsMsg += `|  Eval Tags: ${typeof evalTags === 'string' ? evalTags : JSON.stringify(evalTags)}\n`;
    detailsMsg += `|  Session Name: ${sessionName}\n`;
    detailsMsg += "|  \n";
    if (this._defaultProcessorAttached) {
      detailsMsg += "|  Using a default SpanProcessor. `addSpanProcessor` will overwrite this default.\n";
    }
  }
  async shutdown(): Promise<void> {
    if (this._verbose) {
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
}

function register(options: RegisterOptions = {}): FITracerProvider {
  const {
    projectName: optProjectName,
    projectType = ProjectType.EXPERIMENT,
    projectVersionName: optProjectVersionName,
    evalTags = [],
    sessionName,
    metadata = {},
    batch = false,
    setGlobalTracerProvider = false,
    headers: optHeaders,
    verbose = false,
    endpoint: optEndpoint, // This is passed to _constructFullEndpoint
    idGenerator = new UuidIdGenerator(),
  } = options;

  if (projectType === ProjectType.OBSERVE) {
    if (evalTags.length > 0) {
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

  const projectName = optProjectName ?? _getEnv("FI_PROJECT_NAME") ?? "default-project";
  const projectVersionName = optProjectVersionName ?? _getEnv("FI_PROJECT_VERSION_NAME") ?? "0.0.0";
  const projectVersionId = uuidv4(); 

  const resourceAttributes: Attributes = {
    [SemanticResourceAttributes.SERVICE_NAME]: projectName, 
    [PROJECT_NAME]: projectName,
    [PROJECT_TYPE]: projectType,
    [PROJECT_VERSION_NAME]: projectVersionName,
    [PROJECT_VERSION_ID]: projectVersionId,
    [EVAL_TAGS]: JSON.stringify(evalTags),
    [METADATA]: JSON.stringify(metadata),
  };

  if (projectType === ProjectType.OBSERVE && sessionName) {
    resourceAttributes[SESSION_NAME] = sessionName;
  }

  const resource = Resource.default().merge(new Resource(resourceAttributes));
  
  // Headers for the exporter
  const exporterHeaders = optHeaders ?? _getEnvFiAuthHeader();
  // Endpoint for the exporter is now determined by FITracerProvider's constructor
  // using _constructFullEndpoint, which considers optEndpoint and env vars.

  const tracerProvider = new FITracerProvider({
    resource,
    verbose,
    idGenerator,
    endpoint: optEndpoint, // Pass the direct endpoint option here
    headers: exporterHeaders, 
  });

  if (batch) {
    // If batching, we need to create a new exporter and processor,
    // as the FITracerProvider created a SimpleSpanProcessor by default.
    // The endpoint used by FITracerProvider's default exporter is tracerProvider._endpoint
    const batchExporter = new HTTPSpanExporter({ 
        endpoint: (tracerProvider as any)._endpoint, // Use the fully resolved endpoint
        headers: exporterHeaders,
        verbose: verbose
    });
    const batchProcessor = new OTelBatchSpanProcessor(batchExporter);
    
    (tracerProvider as any)._registeredSpanProcessors = []; 
    (tracerProvider as any)._defaultProcessorAttached = false;
    tracerProvider.addSpanProcessor(batchProcessor);
  }

  if (setGlobalTracerProvider) {
    trace.setGlobalTracerProvider(tracerProvider);
    if (verbose) {
      diag.info( // Use diag.info
        "|  \n" +
        "|  `register` has set this TracerProvider as the global OpenTelemetry default.\n" +
        "|  To disable this behavior, call `register` with " +
        "`set_global_tracer_provider=false`.\n"
      );
    }
  }
  
  return tracerProvider;
}

export {
  register,
  FITracerProvider,
  SimpleSpanProcessor,
  BatchSpanProcessor,
  HTTPSpanExporter,
  UuidIdGenerator,
}


// TODO:
// - Implement prepareEvalTags (similar to Python)
// - Implement checkCustomEvalConfigExists (async, needs careful integration)
// - Refine error handling and logging
// - Add tests 