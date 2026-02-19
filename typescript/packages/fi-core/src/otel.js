import { diag, trace, SpanStatusCode, } from "@opentelemetry/api";
import { ProjectType, prepareEvalTags } from "./fi_types";
import { ExportResultCode } from "@opentelemetry/core";
import { OTLPTraceExporter as _OTLPGRPCTraceExporter } from "@opentelemetry/exporter-trace-otlp-grpc";
import { BasicTracerProvider, BatchSpanProcessor as OTelBatchSpanProcessor, SimpleSpanProcessor as OTelSimpleSpanProcessor, } from "@opentelemetry/sdk-trace-node";
import { resourceFromAttributes, detectResources } from "@opentelemetry/resources";
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
const DEFAULT_FI_GRPC_COLLECTOR_BASE_URL = "https://grpc.futureagi.com";
// Transport enum
export var Transport;
(function (Transport) {
    Transport["HTTP"] = "http";
    Transport["GRPC"] = "grpc";
})(Transport || (Transport = {}));
// --- Custom ID Generator (using UUIDs) ---
class UuidIdGenerator {
    generateTraceId() {
        return uuidv4().replace(/-/g, "");
    }
    generateSpanId() {
        return uuidv4().replace(/-/g, "").substring(0, 16);
    }
}
// --- Custom HTTPSpanExporter ---
class HTTPSpanExporter {
    constructor(options) {
        this.isShutdown = false;
        this.endpoint = options.endpoint; // Expects full endpoint from _normalizedEndpoint
        this.headers = {
            "Content-Type": "application/json",
            ...options.headers,
        };
        this.verbose = options.verbose ?? false;
    }
    formatTraceId(traceId) {
        return traceId;
    }
    formatSpanId(spanId) {
        return spanId;
    }
    convertAttributes(attributes) {
        if (!attributes) {
            return {};
        }
        try {
            return JSON.parse(JSON.stringify(attributes));
        }
        catch (e) {
            diag.error(`HTTPSpanExporter: Error converting attributes: ${e}`);
            return {};
        }
    }
    getSpanStatusName(status) {
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
    export(spans, resultCallback) {
        if (!spans || !resultCallback) {
            resultCallback?.({ code: ExportResultCode.FAILED });
            return;
        }
        if (this.isShutdown) {
            resultCallback({ code: ExportResultCode.FAILED });
            return;
        }
        const spansData = spans.map((span) => {
            if (!span)
                return null;
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
                latency: Math.floor((span.endTime[0] * 1e9 +
                    span.endTime[1] -
                    (span.startTime[0] * 1e9 + span.startTime[1])) /
                    1e6),
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
            }
            else {
                diag.error(`HTTPSpanExporter: Failed to export spans: ${response.status} ${response.statusText}`);
                response.text().then(text => diag.error(`HTTPSpanExporter: Server response: ${text}`));
                resultCallback({ code: ExportResultCode.FAILED });
            }
        })
            .catch((errorCaught) => {
            diag.error(`HTTPSpanExporter: Error exporting spans: ${errorCaught}`);
            resultCallback({ code: ExportResultCode.FAILED });
        });
    }
    async shutdown() {
        this.isShutdown = true;
        return Promise.resolve();
    }
    async forceFlush() {
        return Promise.resolve();
    }
}
// --- Custom GRPCSpanExporter extending OTLP gRPC exporter ---
class GRPCSpanExporter extends _OTLPGRPCTraceExporter {
    constructor(options) {
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
        }
        catch (error) {
            diag.error(`GRPCSpanExporter: Error initializing OTLP exporter:`, error);
            throw error;
        }
        this.verbose = verbose;
        if (this.verbose) {
            diag.info(`GRPCSpanExporter: Configured for endpoint: ${endpoint}`);
            diag.info(`GRPCSpanExporter: Authentication: ${Object.keys(lower_case_auth_header).length > 0 ? 'Enabled' : 'None'}`);
        }
    }
    async shutdown() {
        if (this.verbose) {
            diag.info("GRPCSpanExporter: Shutting down...");
        }
        return super.shutdown();
    }
}
// --- Helper Functions ---
function getEnv(key, defaultValue) {
    return process.env[key] ?? defaultValue;
}
function getEnvVerbose() {
    const envVar = getEnv("FI_VERBOSE_EXPORTER");
    if (envVar) {
        return envVar.toLowerCase() === "true" || envVar === "1";
    }
    return undefined;
}
function getEnvFiAuthHeader() {
    const apiKey = getEnv("FI_API_KEY");
    const secretKey = getEnv("FI_SECRET_KEY");
    if (apiKey && secretKey) {
        // Use lowercase headers for gRPC compatibility
        return { "x-api-key": apiKey, "x-secret-key": secretKey };
    }
    return undefined;
}
function getEnvGrpcCollectorEndpoint() {
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
function constructFullEndpoint(customEndpoint) {
    let baseUrlToUse;
    if (customEndpoint) {
        try {
            const parsedCustom = new URL(customEndpoint);
            if (!parsedCustom.protocol || !parsedCustom.host) {
                diag.warn(`Custom endpoint '${customEndpoint}' is missing protocol or host. Falling back to environment or default.`);
                baseUrlToUse = getEnv("FI_BASE_URL") ?? getEnv("FI_COLLECTOR_ENDPOINT") ?? DEFAULT_FI_COLLECTOR_BASE_URL;
            }
            else if (parsedCustom.pathname !== "/" && parsedCustom.pathname !== FI_COLLECTOR_PATH) {
                diag.warn(`Using custom endpoint as full URL: ${customEndpoint}`);
                return customEndpoint;
            }
            else {
                baseUrlToUse = `${parsedCustom.protocol}//${parsedCustom.host}`;
            }
        }
        catch (e) {
            diag.warn(`Custom endpoint '${customEndpoint}' is not a valid URL. Falling back to environment or default.`);
            baseUrlToUse = getEnv("FI_BASE_URL") ?? getEnv("FI_COLLECTOR_ENDPOINT") ?? DEFAULT_FI_COLLECTOR_BASE_URL;
        }
    }
    else {
        baseUrlToUse = getEnv("FI_BASE_URL") ?? getEnv("FI_COLLECTOR_ENDPOINT") ?? DEFAULT_FI_COLLECTOR_BASE_URL;
    }
    // Ensure no trailing slash from baseUrl before appending path
    if (baseUrlToUse.endsWith('/')) {
        baseUrlToUse = baseUrlToUse.slice(0, -1);
    }
    return `${baseUrlToUse}${FI_COLLECTOR_PATH}`;
}
class FITracerProvider extends BasicTracerProvider {
    constructor(config = {}) {
        const idGenerator = config.idGenerator ?? new UuidIdGenerator();
        const transport = config.transport ?? Transport.HTTP;
        const verbose = config.verbose ?? (getEnv("FI_VERBOSE_PROVIDER")?.toLowerCase() === "true");
        // Construct the appropriate endpoint based on transport
        let endpoint;
        if (transport === Transport.GRPC) {
            endpoint = config.endpoint ?? getEnvGrpcCollectorEndpoint();
        }
        else {
            endpoint = constructFullEndpoint(config.endpoint);
        }
        const headers = config.headers ?? getEnvFiAuthHeader();
        if (verbose) {
            diag.info(`FITracerProvider: Using ${transport.toUpperCase()} exporter endpoint: ${endpoint}`);
        }
        // Create the appropriate exporter
        let exporter;
        if (transport === Transport.GRPC) {
            exporter = new GRPCSpanExporter({ endpoint, headers, verbose: config.verbose });
        }
        else {
            exporter = new HTTPSpanExporter({ endpoint, headers, verbose: config.verbose });
        }
        const defaultProcessor = new OTelSimpleSpanProcessor(exporter);
        super({ resource: config.resource, idGenerator, spanProcessors: [defaultProcessor] });
        this.defaultProcessorAttached = false;
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
    addSpanProcessor(spanProcessor) {
        if (this.defaultProcessorAttached) {
            diag.warn("Adding a new SpanProcessor. The default SimpleSpanProcessor will be replaced.");
            this._registeredSpanProcessors = [];
            this.defaultProcessorAttached = false;
        }
        this._registeredSpanProcessors.push(spanProcessor);
    }
    printTracingDetails() {
        const resource = this.resource;
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
        const detailsHeader = process.platform === "win32"
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
    async shutdown() {
        if (this.verbose) {
            diag.info("Shutting down FI TracerProvider...");
        }
        return super.shutdown();
    }
}
class SimpleSpanProcessor extends OTelSimpleSpanProcessor {
}
class BatchSpanProcessor extends OTelBatchSpanProcessor {
}
function register(options = {}) {
    const { projectName: optProjectName, projectType = ProjectType.EXPERIMENT, projectVersionName: optProjectVersionName, evalTags: optEvalTags = [], sessionName, metadata = {}, batch = false, setGlobalTracerProvider = true, headers: optHeaders, verbose = false, endpoint: optEndpoint, idGenerator = new UuidIdGenerator(), transport = Transport.HTTP, } = options;
    const preparedEvalTags = prepareEvalTags(optEvalTags);
    if (projectType === ProjectType.OBSERVE) {
        if (preparedEvalTags.length > 0) {
            throw new Error("Eval tags are not allowed for project type OBSERVE");
        }
        if (optProjectVersionName) {
            throw new Error("Project Version Name not allowed for project type OBSERVE");
        }
    }
    if (projectType === ProjectType.EXPERIMENT) {
        if (sessionName) {
            throw new Error("Session name is not allowed for project type EXPERIMENT");
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
        checkCustomEvalConfigExists(projectName, preparedEvalTags, optEndpoint, verbose).then(customEvalConfigExists => {
            if (customEvalConfigExists) {
                // Log an error instead of throwing, as register has already returned.
                diag.error(`register: Custom eval configuration already exists for project '${projectName}'. ` +
                    "The SDK will continue to initialize, but this may lead to unexpected behavior or duplicate configurations. " +
                    "Please use a different project name or disable/modify the existing custom eval configuration.");
            }
        }).catch(error => {
            // Log any error from the check itself
            diag.error(`register: Error during background checkCustomEvalConfigExists for project '${projectName}': ${error}`);
        });
    }
    const resourceAttributes = {
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
        let batchExporter;
        if (transport === Transport.GRPC) {
            batchExporter = new GRPCSpanExporter({
                endpoint: tracerProvider.endpoint,
                headers: exporterHeaders,
                verbose: verbose
            });
        }
        else {
            batchExporter = new HTTPSpanExporter({
                endpoint: tracerProvider.endpoint,
                headers: exporterHeaders,
                verbose: verbose
            });
        }
        const batchProcessor = new OTelBatchSpanProcessor(batchExporter);
        tracerProvider._registeredSpanProcessors = [];
        tracerProvider._defaultProcessorAttached = false;
        tracerProvider.addSpanProcessor(batchProcessor);
    }
    if (setGlobalTracerProvider) {
        trace.setGlobalTracerProvider(tracerProvider);
        if (verbose) {
            diag.info("|  \n" +
                "|  `register` has set this TracerProvider as the global OpenTelemetry default.\n" +
                "|  To disable this behavior, call `register` with " +
                "`set_global_tracer_provider=false`.\n");
        }
    }
    return tracerProvider;
}
async function checkCustomEvalConfigExists(projectName, evalTags, // Expects result of prepareEvalTags
customEndpoint, // Can be base or full URL for the API call itself
verbose) {
    if (!evalTags || evalTags.length === 0) {
        return false;
    }
    let apiBaseUrl;
    if (customEndpoint) {
        try {
            const parsedCustom = new URL(customEndpoint);
            if (parsedCustom.pathname.endsWith(FI_CUSTOM_EVAL_CONFIG_CHECK_PATH)) {
                if (verbose)
                    diag.info(`checkCustomEvalConfigExists: Using custom full endpoint: ${customEndpoint}`);
                apiBaseUrl = customEndpoint.substring(0, customEndpoint.lastIndexOf(FI_CUSTOM_EVAL_CONFIG_CHECK_PATH));
            }
            else if (parsedCustom.pathname === "/" || parsedCustom.pathname === "") {
                apiBaseUrl = `${parsedCustom.protocol}//${parsedCustom.host}`;
            }
            else {
                apiBaseUrl = customEndpoint;
            }
        }
        catch (e) {
            if (verbose)
                diag.warn(`checkCustomEvalConfigExists: Custom endpoint '${customEndpoint}' is not a valid URL. Falling back to environment or default.`);
            apiBaseUrl = getEnv("FI_BASE_URL") ?? getEnv("FI_COLLECTOR_ENDPOINT") ?? DEFAULT_FI_COLLECTOR_BASE_URL;
        }
    }
    else {
        apiBaseUrl = getEnv("FI_BASE_URL") ?? getEnv("FI_COLLECTOR_ENDPOINT") ?? DEFAULT_FI_COLLECTOR_BASE_URL;
    }
    if (apiBaseUrl.endsWith('/')) {
        apiBaseUrl = apiBaseUrl.slice(0, -1);
    }
    const url = `${apiBaseUrl}${FI_CUSTOM_EVAL_CONFIG_CHECK_PATH}`;
    const headers = {
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
            diag.error(`checkCustomEvalConfigExists: Failed to check custom eval config: ${response.status} ${response.statusText} - ${errorText}`);
            return false;
        }
        const result = await response.json();
        if (verbose) {
            diag.info("checkCustomEvalConfigExists: Response from server:", JSON.stringify(result, null, 2));
        }
        return result?.result?.exists === true;
    }
    catch (error) {
        diag.error(`checkCustomEvalConfigExists: Error checking custom eval config: ${error}`);
        return false;
    }
}
export async function checkCustomEvalTemplateExists(eval_template_name, verbose, customEndpoint) {
    if (!eval_template_name || eval_template_name.length === 0) {
        const response = {
            result: {
                isUserEvalTemplate: false,
                evalTemplate: null
            }
        };
        return response;
    }
    let apiBaseUrl;
    if (customEndpoint) {
        try {
            const parsedCustom = new URL(customEndpoint);
            if (parsedCustom.pathname.endsWith(FI_CUSTOM_EVAL_TEMPLATE_CHECK_PATH)) {
                if (verbose)
                    diag.info(`checkCustomEvalTemplateExists: Using custom full endpoint: ${customEndpoint}`);
                apiBaseUrl = customEndpoint.substring(0, customEndpoint.lastIndexOf(FI_CUSTOM_EVAL_TEMPLATE_CHECK_PATH));
            }
            else if (parsedCustom.pathname === "/" || parsedCustom.pathname === "") {
                apiBaseUrl = `${parsedCustom.protocol}//${parsedCustom.host}`;
            }
            else {
                apiBaseUrl = customEndpoint;
            }
        }
        catch (e) {
            if (verbose)
                diag.warn(`checkCustomEvalTemplateExists: Custom endpoint '${customEndpoint}' is not a valid URL. Falling back to environment or default.`);
            apiBaseUrl = getEnv("FI_BASE_URL") ?? getEnv("FI_COLLECTOR_ENDPOINT") ?? DEFAULT_FI_COLLECTOR_BASE_URL;
        }
    }
    else {
        apiBaseUrl = getEnv("FI_BASE_URL") ?? getEnv("FI_COLLECTOR_ENDPOINT") ?? DEFAULT_FI_COLLECTOR_BASE_URL;
    }
    if (apiBaseUrl.endsWith('/')) {
        apiBaseUrl = apiBaseUrl.slice(0, -1);
    }
    const url = `${apiBaseUrl}${FI_CUSTOM_EVAL_TEMPLATE_CHECK_PATH}`;
    const headers = {
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
            diag.error(`checkCustomEvalTemplateExists: Failed to check custom eval template: ${response.status} ${response.statusText} - ${errorText}`);
            return {
                result: {
                    isUserEvalTemplate: false,
                    evalTemplate: null
                }
            };
        }
        const result = await response.json();
        if (verbose) {
            diag.info("checkCustomEvalTemplateExists: Response from server:", JSON.stringify(result, null, 2));
        }
        return result;
    }
    catch (error) {
        diag.error(`checkCustomEvalTemplateExists: Error checking custom eval template: ${error}`);
        return {
            result: {
                isUserEvalTemplate: false,
                evalTemplate: null
            }
        };
    }
}
export { register, FITracerProvider, SimpleSpanProcessor, BatchSpanProcessor, HTTPSpanExporter, GRPCSpanExporter, UuidIdGenerator, checkCustomEvalConfigExists };
// TODO:
// - Implement prepareEvalTags (similar to Python)
// - Implement checkCustomEvalConfigExists
// - Refine error handling and logging
// - Add tests 
//# sourceMappingURL=data:application/json;base64,eyJ2ZXJzaW9uIjozLCJmaWxlIjoib3RlbC5qcyIsInNvdXJjZVJvb3QiOiIiLCJzb3VyY2VzIjpbIm90ZWwudHMiXSwibmFtZXMiOltdLCJtYXBwaW5ncyI6IkFBQUEsT0FBTyxFQUVMLElBQUksRUFFSixLQUFLLEVBR0wsY0FBYyxHQUNmLE1BQU0sb0JBQW9CLENBQUM7QUFDNUIsT0FBTyxFQUFFLFdBQVcsRUFBVyxlQUFlLEVBQUUsTUFBTSxZQUFZLENBQUM7QUFDbkUsT0FBTyxFQUFnQixnQkFBZ0IsRUFBRSxNQUFNLHFCQUFxQixDQUFDO0FBTXJFLE9BQU8sRUFBRSxpQkFBaUIsSUFBSSxzQkFBc0IsRUFBRSxNQUFNLHlDQUF5QyxDQUFDO0FBQ3RHLE9BQU8sRUFDTCxtQkFBbUIsRUFDbkIsa0JBQWtCLElBQUksc0JBQXNCLEVBQzVDLG1CQUFtQixJQUFJLHVCQUF1QixHQUUvQyxNQUFNLCtCQUErQixDQUFDO0FBQ3ZDLE9BQU8sRUFBWSxzQkFBc0IsRUFBRSxlQUFlLEVBQW1CLE1BQU0sMEJBQTBCLENBQUM7QUFDOUcsT0FBTyxFQUFFLDBCQUEwQixFQUFFLE1BQU0scUNBQXFDLENBQUM7QUFDakYsT0FBTyxFQUFFLEVBQUUsSUFBSSxNQUFNLEVBQUUsTUFBTSxNQUFNLENBQUMsQ0FBQyxzQkFBc0I7QUFFM0Qsb0NBQW9DO0FBQ3BDLE9BQU8sS0FBSyxJQUFJLE1BQU0sZUFBZSxDQUFDO0FBRXRDLDRDQUE0QztBQUM1QyxNQUFNLENBQUMsTUFBTSxZQUFZLEdBQUcsY0FBYyxDQUFDO0FBQzNDLE1BQU0sQ0FBQyxNQUFNLFlBQVksR0FBRyxjQUFjLENBQUM7QUFDM0MsTUFBTSxDQUFDLE1BQU0sb0JBQW9CLEdBQUcsc0JBQXNCLENBQUM7QUFDM0QsTUFBTSxDQUFDLE1BQU0sa0JBQWtCLEdBQUcsb0JBQW9CLENBQUM7QUFDdkQsTUFBTSxDQUFDLE1BQU0sU0FBUyxHQUFHLFdBQVcsQ0FBQztBQUNyQyxNQUFNLENBQUMsTUFBTSxRQUFRLEdBQUcsVUFBVSxDQUFDO0FBQ25DLE1BQU0sQ0FBQyxN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QUFnQixFQUNoQixnQkFBZ0IsRUFDaEIsZUFBZSxFQUNmLDJCQUEyQixFQUM1QixDQUFBO0FBR0QsUUFBUTtBQUNSLGtEQUFrRDtBQUNsRCwwQ0FBMEM7QUFDMUMsc0NBQXNDO0FBQ3RDLGVBQWUiLCJzb3VyY2VzQ29udGVudCI6WyJpbXBvcnQge1xuICBBdHRyaWJ1dGVzLFxuICBkaWFnLFxuICBUcmFjZUZsYWdzLFxuICB0cmFjZSxcbiAgU3BhbkNvbnRleHQsXG4gIFNwYW5TdGF0dXMsXG4gIFNwYW5TdGF0dXNDb2RlLFxufSBmcm9tIFwiQG9wZW50ZWxlbWV0cnkvYXBpXCI7XG5pbXBvcnQgeyBQcm9qZWN0VHlwZSwgRXZhbFRhZywgcHJlcGFyZUV2YWxUYWdzIH0gZnJvbSBcIi4vZmlfdHlwZXNcIjtcbmltcG9ydCB7IEV4cG9ydFJlc3VsdCwgRXhwb3J0UmVzdWx0Q29kZSB9IGZyb20gXCJAb3BlbnRlbGVtZXRyeS9jb3JlXCI7XG5pbXBvcnQge1xuICBSZWFkYWJsZVNwYW4sXG4gIFNwYW5FeHBvcnRlcixcbiAgU3BhblByb2Nlc3Nvcixcbn0gZnJvbSBcIkBvcGVudGVsZW1ldHJ5L3Nkay10cmFjZS1iYXNlXCI7XG5pbXBvcnQgeyBPVExQVHJhY2VFeHBvcnRlciBhcyBfT1RMUEdSUENUcmFjZUV4cG9ydGVyIH0gZnJvbSBcIkBvcGVudGVsZW1ldHJ5L2V4cG9ydGVyLXRyYWNlLW90bHAtZ3JwY1wiO1xuaW1wb3J0IHtcbiAgQmFzaWNUcmFjZXJQcm92aWRlcixcbiAgQmF0Y2hTcGFuUHJvY2Vzc29yIGFzIE9UZWxCYXRjaFNwYW5Qcm9jZXNzb3IsXG4gIFNpbXBsZVNwYW5Qcm9jZXNzb3IgYXMgT1RlbFNpbXBsZVNwYW5Qcm9jZXNzb3IsXG4gIElkR2VuZXJhdG9yLFxufSBmcm9tIFwiQG9wZW50ZWxlbWV0cnkvc2RrLXRyYWNlLW5vZGVcIjtcbmltcG9ydCB7IFJlc291cmNlLCByZXNvdXJjZUZyb21BdHRyaWJ1dGVzLCBkZXRlY3RSZXNvdXJjZXMsIGRlZmF1bHRSZXNvdXJjZSB9IGZyb20gXCJAb3BlbnRlbGVtZXRyeS9yZXNvdXJjZXNcIjtcbmltcG9ydCB7IFNlbWFudGljUmVzb3VyY2VBdHRyaWJ1dGVzIH0gZnJvbSBcIkBvcGVudGVsZW1ldHJ5L3NlbWFudGljLWNvbnZlbnRpb25zXCI7XG5pbXBvcnQgeyB2NCBhcyB1dWlkdjQgfSBmcm9tIFwidXVpZFwiOyAvLyBGb3IgVVVJRCBnZW5lcmF0aW9uXG5cbi8vIEltcG9ydCBncnBjIGZvciBtZXRhZGF0YSBoYW5kbGluZ1xuaW1wb3J0ICogYXMgZ3JwYyBmcm9tIFwiQGdycGMvZ3JwYy1qc1wiO1xuXG4vLyAtLS0gQ29uc3RhbnRzIGZvciBSZXNvdXJjZSBBdHRyaWJ1dGVzIC0tLVxuZXhwb3J0IGNvbnN0IFBST0pFQ1RfTkFNRSA9IFwicHJvamVjdF9uYW1lXCI7XG5leHBvcnQgY29uc3QgUFJPSkVDVF9UWVBFID0gXCJwcm9qZWN0X3R5cGVcIjtcbmV4cG9ydCBjb25zdCBQUk9KRUNUX1ZFUlNJT05fTkFNRSA9IFwicHJvamVjdF92ZXJzaW9uX25hbWVcIjtcbmV4cG9ydCBjb25zdCBQUk9KRUNUX1ZFUlNJT05fSUQgPSBcInByb2plY3RfdmVyc2lvbl9pZFwiO1xuZXhwb3J0IGNvbnN0IEVWQUxfVEFHUyA9IFwiZXZhbF90YWdzXCI7XG5leHBvcnQgY29uc3QgTUVUQURBVEEgPSBcIm1ldGFkYXRhXCI7XG5leHBvcnQgY29uc3QgU0VTU0lPTl9OQU1FID0gXCJzZXNzaW9uX25hbWVcIjtcblxuXG5cbi8vIERlZmF1bHQgYmFzZSBVUkwgaWYgbm90IG92ZXJyaWRkZW4gYnkgZW52aXJvbm1lbnQgb3IgZGlyZWN0IGNvbmZpZ1xuY29uc3QgREVGQVVMVF9GSV9DT0xMRUNUT1JfQkFTRV9VUkwgPSBcImh0dHBzOi8vYXBpLmZ1dHVyZWFnaS5jb21cIjtcbmNvbnN0IEZJX0NPTExFQ1RPUl9QQVRIID0gXCIvdHJhY2VyL29ic2VydmF0aW9uLXNwYW4vY3JlYXRlX290ZWxfc3Bhbi9cIjtcbmNvbnN0IEZJX0NVU1RPTV9FVkFMX0NPTkZJR19DSEVDS19QQVRIID0gXCIvdHJhY2VyL2N1c3RvbS1ldmFsLWNvbmZpZy9jaGVja19leGlzdHMvXCI7XG5jb25zdCBGSV9DVVNUT01fRVZBTF9URU1QTEFURV9DSEVDS19QQVRIID0gXCIvdHJhY2VyL2N1c3RvbS1ldmFsLWNvbmZpZy9nZXRfY3VzdG9tX2V2YWxfYnlfbmFtZS9cIjtcblxuLy8gRGVmYXVsdCBnUlBDIGVuZHBvaW50ICBcbmNvbnN0IERFRkFVTFRfRklfR1JQQ19DT0xMRUNUT1JfQkFTRV9VUkwgPSBcImh0dHBzOi8vZ3JwYy5mdXR1cmVhZ2kuY29tXCI7XG5cbi8vIFRyYW5zcG9ydCBlbnVtXG5leHBvcnQgZW51bSBUcmFuc3BvcnQge1xuICBIVFRQID0gXCJodHRwXCIsXG4gIEdSUEMgPSBcImdycGNcIixcbn1cblxuXG5cbmludGVyZmFjZSBGSUhlYWRlcnMge1xuICBba2V5OiBzdHJpbmddOiBzdHJpbmc7XG59XG5cbmludGVyZmFjZSBIVFRQU3BhbkV4cG9ydGVyT3B0aW9ucyB7XG4gIGVuZHBvaW50OiBzdHJpbmc7IC8vIFRoaXMgc2hvdWxkIGJlIHRoZSBmdWxsIFVSTFxuICBoZWFkZXJzPzogRklIZWFkZXJzO1xuICB2ZXJib3NlPzogYm9vbGVhbjtcbn1cblxuLy8gLS0tIEN1c3RvbSBJRCBHZW5lcmF0b3IgKHVzaW5nIFVVSURzKSAtLS1cbmNsYXNzIFV1aWRJZEdlbmVyYXRvciBpbXBsZW1lbnRzIElkR2VuZXJhdG9yIHtcbiAgZ2VuZXJhdGVUcmFjZUlkKCk6IHN0cmluZyB7XG4gICAgcmV0dXJuIHV1aWR2NCgpLnJlcGxhY2UoLy0vZywgXCJcIik7XG4gIH1cbiAgZ2VuZXJhdGVTcGFuSWQoKTogc3RyaW5nIHtcbiAgICByZXR1cm4gdXVpZHY0KCkucmVwbGFjZSgvLS9nLCBcIlwiKS5zdWJzdHJpbmcoMCwgMTYpO1xuICB9XG59XG5cbi8vIC0tLSBDdXN0b20gSFRUUFNwYW5FeHBvcnRlciAtLS1cbmNsYXNzIEhUVFBTcGFuRXhwb3J0ZXIgaW1wbGVtZW50cyBTcGFuRXhwb3J0ZXIge1xuICBwcml2YXRlIHJlYWRvbmx5IGVuZHBvaW50OiBzdHJpbmc7XG4gIHByaXZhdGUgcmVhZG9ubHkgaGVhZGVyczogRklIZWFkZXJzO1xuICBwcml2YXRlIGlzU2h1dGRvd24gPSBmYWxzZTtcbiAgcHJpdmF0ZSB2ZXJib3NlOiBib29sZWFuO1xuXG4gIGNvbnN0cnVjdG9yKG9wdGlvbnM6IEhUVFBTcGFuRXhwb3J0ZXJPcHRpb25zKSB7XG4gICAgdGhpcy5lbmRwb2ludCA9IG9wdGlvbnMuZW5kcG9pbnQ7IC8vIEV4cGVjdHMgZnVsbCBlbmRwb2ludCBmcm9tIF9ub3JtYWxpemVkRW5kcG9pbnRcbiAgICB0aGlzLmhlYWRlcnMgPSB7XG4gICAgICBcIkNvbnRlbnQtVHlwZVwiOiBcImFwcGxpY2F0aW9uL2pzb25cIixcbiAgICAgIC4uLm9wdGlvbnMuaGVhZGVycyxcbiAgICB9O1xuICAgIHRoaXMudmVyYm9zZSA9IG9wdGlvbnMudmVyYm9zZSA/PyBmYWxzZTtcbiAgfVxuXG4gIHByaXZhdGUgZm9ybWF0VHJhY2VJZCh0cmFjZUlkOiBzdHJpbmcpOiBzdHJpbmcge1xuICAgIHJldHVybiB0cmFjZUlkOyBcbiAgfVxuXG4gIHByaXZhdGUgZm9ybWF0U3BhbklkKHNwYW5JZDogc3RyaW5nKTogc3RyaW5nIHtcbiAgICByZXR1cm4gc3BhbklkOyBcbiAgfVxuXG4gIHByaXZhdGUgY29udmVydEF0dHJpYnV0ZXMoYXR0cmlidXRlczogQXR0cmlidXRlcyB8IHVuZGVmaW5lZCk6IFJlY29yZDxzdHJpbmcsIGFueT4ge1xuICAgIGlmICghYXR0cmlidXRlcykge1xuICAgICAgcmV0dXJuIHt9O1xuICAgIH1cbiAgICB0cnkge1xuICAgICAgcmV0dXJuIEpTT04ucGFyc2UoSlNPTi5zdHJpbmdpZnkoYXR0cmlidXRlcykpO1xuICAgIH0gY2F0Y2ggKGUpIHtcbiAgICAgIGRpYWcuZXJyb3IoYEhUVFBTcGFuRXhwb3J0ZXI6IEVycm9yIGNvbnZlcnRpbmcgYXR0cmlidXRlczogJHtlfWApO1xuICAgICAgcmV0dXJuIHt9O1xuICAgIH1cbiAgfVxuXG4gIHByaXZhdGUgZ2V0U3BhblN0YXR1c05hbWUoc3RhdHVzOiBTcGFuU3RhdHVzKTogc3RyaW5nIHtcbiAgICBzd2l0Y2ggKHN0YXR1cy5jb2RlKSB7XG4gICAgICBjYXNlIFNwYW5TdGF0dXNDb2RlLlVOU0VUOlxuICAgICAgICByZXR1cm4gXCJVTlNFVFwiO1xuICAgICAgY2FzZSBTcGFuU3RhdHVzQ29kZS5PSzpcbiAgICAgICAgcmV0dXJuIFwiT0tcIjtcbiAgICAgIGNhc2UgU3BhblN0YXR1c0NvZGUuRVJST1I6XG4gICAgICAgIHJldHVybiBcIkVSUk9SXCI7XG4gICAgICBkZWZhdWx0OlxuICAgICAgICByZXR1cm4gXCJVTktOT1dOXCI7XG4gICAgfVxuICB9XG5cbiAgZXhwb3J0KFxuICAgIHNwYW5zOiBSZWFkYWJsZVNwYW5bXSxcbiAgICByZXN1bHRDYWxsYmFjazogKHJlc3VsdDogRXhwb3J0UmVzdWx0KSA9PiB2b2lkLFxuICApOiB2b2lkIHtcbiAgICBpZiAoIXNwYW5zIHx8ICFyZXN1bHRDYWxsYmFjaykge1xuICAgICAgcmVzdWx0Q2FsbGJhY2s/Lih7IGNvZGU6IEV4cG9ydFJlc3VsdENvZGUuRkFJTEVEIH0pO1xuICAgICAgcmV0dXJuO1xuICAgIH1cbiAgICBpZiAodGhpcy5pc1NodXRkb3duKSB7XG4gICAgICByZXN1bHRDYWxsYmFjayh7IGNvZGU6IEV4cG9ydFJlc3VsdENvZGUuRkFJTEVEIH0pO1xuICAgICAgcmV0dXJuO1xuICAgIH1cblxuICAgIGNvbnN0IHNwYW5zRGF0YSA9IHNwYW5zLm1hcCgoc3BhbikgPT4ge1xuICAgICAgaWYgKCFzcGFuKSByZXR1cm4gbnVsbDtcbiAgICAgIGNvbnN0IHNwYW5Db250ZXh0ID0gc3Bhbi5zcGFuQ29udGV4dCgpO1xuICAgICAgaWYgKCFzcGFuQ29udGV4dCkge1xuICAgICAgICByZXR1cm4gbnVsbDtcbiAgICAgIH1cbiAgICAgIGNvbnN0IHBhcmVudFNwYW5JZCA9IHNwYW4ucGFyZW50U3BhbkNvbnRleHQ/LnNwYW5JZFxuICAgICAgICA/IHRoaXMuZm9ybWF0U3BhbklkKHNwYW4ucGFyZW50U3BhbkNvbnRleHQuc3BhbklkKVxuICAgICAgICA6IHVuZGVmaW5lZDtcblxuICAgICAgcmV0dXJuIHtcbiAgICAgICAgdHJhY2VfaWQ6IHRoaXMuZm9ybWF0VHJhY2VJZChzcGFuQ29udGV4dC50cmFjZUlkKSxcbiAgICAgICAgc3Bhbl9pZDogdGhpcy5mb3JtYXRTcGFuSWQoc3BhbkNvbnRleHQuc3BhbklkKSxcbiAgICAgICAgbmFtZTogc3Bhbi5uYW1lIHx8IFwidW5rbm93bi1zcGFuXCIsXG4gICAgICAgIHN0YXJ0X3RpbWU6IHNwYW4uc3RhcnRUaW1lPy5bMF0gKiAxZTkgKyBzcGFuLnN0YXJ0VGltZT8uWzFdIHx8IDAsXG4gICAgICAgIGVuZF90aW1lOiBzcGFuLmVuZFRpbWU/LlswXSAqIDFlOSArIHNwYW4uZW5kVGltZT8uWzFdIHx8IDAsXG4gICAgICAgIGF0dHJpYnV0ZXM6IHRoaXMuY29udmVydEF0dHJpYnV0ZXMoc3Bhbi5hdHRyaWJ1dGVzKSxcbiAgICAgICAgZXZlbnRzOiBzcGFuLmV2ZW50cy5tYXAoKGV2ZW50KSA9PiAoe1xuICAgICAgICAgIG5hbWU6IGV2ZW50Lm5hbWUsXG4gICAgICAgICAgYXR0cmlidXRlczogdGhpcy5jb252ZXJ0QXR0cmlidXRlcyhldmVudC5hdHRyaWJ1dGVzKSxcbiAgICAgICAgICB0aW1lc3RhbXA6IGV2ZW50LnRpbWVbMF0gKiAxZTkgKyBldmVudC50aW1lWzFdLCBcbiAgICAgICAgfSkpLFxuICAgICAgICBzdGF0dXM6IHRoaXMuZ2V0U3BhblN0YXR1c05hbWUoc3Bhbi5zdGF0dXMpLFxuICAgICAgICBwYXJlbnRfaWQ6IHBhcmVudFNwYW5JZCxcbiAgICAgICAgcHJvamVjdF9uYW1lOiBzcGFuLnJlc291cmNlPy5hdHRyaWJ1dGVzW1BST0pFQ1RfTkFNRV0sXG4gICAgICAgIHByb2plY3RfdHlwZTogc3Bhbi5yZXNvdXJjZT8uYXR0cmlidXRlc1tQUk9KRUNUX1RZUEVdLFxuICAgICAgICBwcm9qZWN0X3ZlcnNpb25fbmFtZTogc3Bhbi5yZXNvdXJjZT8uYXR0cmlidXRlc1tQUk9KRUNUX1ZFUlNJT05fTkFNRV0sXG4gICAgICAgIHByb2plY3RfdmVyc2lvbl9pZDogc3Bhbi5yZXNvdXJjZT8uYXR0cmlidXRlc1tQUk9KRUNUX1ZFUlNJT05fSURdLFxuICAgICAgICBsYXRlbmN5OiBNYXRoLmZsb29yKFxuICAgICAgICAgIChzcGFuLmVuZFRpbWVbMF0gKiAxZTkgK1xuICAgICAgICAgICAgc3Bhbi5lbmRUaW1lWzFdIC1cbiAgICAgICAgICAgIChzcGFuLnN0YXJ0VGltZVswXSAqIDFlOSArIHNwYW4uc3RhcnRUaW1lWzFdKSkgL1xuICAgICAgICAgICAgMWU2LCBcbiAgICAgICAgKSxcbiAgICAgICAgZXZhbF90YWdzOiBzcGFuLnJlc291cmNlPy5hdHRyaWJ1dGVzW0VWQUxfVEFHU10sIFxuICAgICAgICBtZXRhZGF0YTogc3Bhbi5yZXNvdXJjZT8uYXR0cmlidXRlc1tNRVRBREFUQV0sIFxuICAgICAgICBzZXNzaW9uX25hbWU6IHNwYW4ucmVzb3VyY2U/LmF0dHJpYnV0ZXNbU0VTU0lPTl9OQU1FXSxcbiAgICAgIH07XG4gICAgfSkuZmlsdGVyKEJvb2xlYW4pOyBcblxuICAgIGlmICh0aGlzLnZlcmJvc2UpIHtcbiAgICAgICAgZGlhZy5pbmZvKFwiSFRUUFNwYW5FeHBvcnRlcjogU2VuZGluZyBwYXlsb2FkOlwiLCBKU09OLnN0cmluZ2lmeShzcGFuc0RhdGEsIG51bGwsIDIpKTtcbiAgICB9XG5cbiAgICBmZXRjaCh0aGlzLmVuZHBvaW50LCB7XG4gICAgICBtZXRob2Q6IFwiUE9TVFwiLFxuICAgICAgaGVhZGVyczogdGhpcy5oZWFkZXJzLFxuICAgICAgYm9keTogSlNPTi5zdHJpbmdpZnkoc3BhbnNEYXRhKSxcbiAgICB9KVxuICAgICAgLnRoZW4oKHJlc3BvbnNlKSA9PiB7XG4gICAgICAgIGlmIChyZXNwb25zZS5vaykge1xuICAgICAgICAgIHJlc3VsdENhbGxiYWNrKHsgY29kZTogRXhwb3J0UmVzdWx0Q29kZS5TVUNDRVNTIH0pO1xuICAgICAgICB9IGVsc2Uge1xuICAgICAgICAgIGRpYWcuZXJyb3IoXG4gICAgICAgICAgICBgSFRUUFNwYW5FeHBvcnRlcjogRmFpbGVkIHRvIGV4cG9ydCBzcGFuczogJHtyZXNwb25zZS5zdGF0dXN9ICR7cmVzcG9uc2Uuc3RhdHVzVGV4dH1gLFxuICAgICAgICAgICk7XG4gICAgICAgICAgcmVzcG9uc2UudGV4dCgpLnRoZW4odGV4dCA9PiBkaWFnLmVycm9yKGBIVFRQU3BhbkV4cG9ydGVyOiBTZXJ2ZXIgcmVzcG9uc2U6ICR7dGV4dH1gKSk7XG4gICAgICAgICAgcmVzdWx0Q2FsbGJhY2soeyBjb2RlOiBFeHBvcnRSZXN1bHRDb2RlLkZBSUxFRCB9KTtcbiAgICAgICAgfVxuICAgICAgfSlcbiAgICAgIC5jYXRjaCgoZXJyb3JDYXVnaHQpID0+IHsgXG4gICAgICAgIGRpYWcuZXJyb3IoYEhUVFBTcGFuRXhwb3J0ZXI6IEVycm9yIGV4cG9ydGluZyBzcGFuczogJHtlcnJvckNhdWdodH1gKTtcbiAgICAgICAgcmVzdWx0Q2FsbGJhY2soeyBjb2RlOiBFeHBvcnRSZXN1bHRDb2RlLkZBSUxFRCB9KTtcbiAgICAgIH0pO1xuICB9XG5cbiAgYXN5bmMgc2h1dGRvd24oKTogUHJvbWlzZTx2b2lkPiB7XG4gICAgdGhpcy5pc1NodXRkb3duID0gdHJ1ZTtcbiAgICByZXR1cm4gUHJvbWlzZS5yZXNvbHZlKCk7XG4gIH1cblxuICBhc3luYyBmb3JjZUZsdXNoPygpOiBQcm9taXNlPHZvaWQ+IHtcbiAgICByZXR1cm4gUHJvbWlzZS5yZXNvbHZlKCk7XG4gIH1cbn1cblxuLy8gLS0tIEN1c3RvbSBHUlBDU3BhbkV4cG9ydGVyIGV4dGVuZGluZyBPVExQIGdSUEMgZXhwb3J0ZXIgLS0tXG5jbGFzcyBHUlBDU3BhbkV4cG9ydGVyIGV4dGVuZHMgX09UTFBHUlBDVHJhY2VFeHBvcnRlciB7XG4gIHByaXZhdGUgdmVyYm9zZTogYm9vbGVhbjtcblxuICBjb25zdHJ1Y3RvcihvcHRpb25zOiB7IGVuZHBvaW50OiBzdHJpbmc7IGhlYWRlcnM/OiBGSUhlYWRlcnM7IHZlcmJvc2U/OiBib29sZWFuOyBba2V5OiBzdHJpbmddOiBhbnkgfSkge1xuICAgIGNvbnN0IHsgZW5kcG9pbnQsIGhlYWRlcnMgPSB7fSwgdmVyYm9zZSA9IGZhbHNlLCAuLi5yZXN0T3B0aW9ucyB9ID0gb3B0aW9ucztcbiAgICBcbiAgICBjb25zdCBhdXRoX2hlYWRlciA9IGhlYWRlcnM7XG4gICAgY29uc3QgbG93ZXJfY2FzZV9hdXRoX2hlYWRlciA9IGF1dGhfaGVhZGVyIFxuICAgICAgPyBPYmplY3QuZnJvbUVudHJpZXMoT2JqZWN0LmVudHJpZXMoYXV0aF9oZWFkZXIpLm1hcCgoW2ssIHZdKSA9PiBbay50b0xvd2VyQ2FzZSgpLCB2XSkpXG4gICAgICA6IHt9O1xuXG4gICAgY29uc3QgbWV0YWRhdGEgPSBuZXcgZ3JwYy5NZXRhZGF0YSgpO1xuICAgIE9iamVjdC5lbnRyaWVzKGxvd2VyX2Nhc2VfYXV0aF9oZWFkZXIpLmZvckVhY2goKFtrZXksIHZhbHVlXSkgPT4ge1xuICAgICAgbWV0YWRhdGEuc2V0KGtleSwgdmFsdWUpO1xuICAgIH0pO1xuXG4gICAgY29uc3QgZ3JwY0NvbmZpZyA9IHtcbiAgICAgIHVybDogZW5kcG9pbnQsXG4gICAgICBtZXRhZGF0YTogbWV0YWRhdGEsXG4gICAgICAuLi4oZW5kcG9pbnQuaW5jbHVkZXMoJ2xvY2FsaG9zdCcpIHx8IGVuZHBvaW50LmluY2x1ZGVzKCcxMjcuMC4wLjEnKSA/IHsgXG4gICAgICAgIGNyZWRlbnRpYWxzOiBncnBjLmNyZWRlbnRpYWxzLmNyZWF0ZUluc2VjdXJlKCkgXG4gICAgICB9IDoge30pLFxuICAgICAgLi4ucmVzdE9wdGlvbnMsXG4gICAgfTtcblxuICAgIHRyeSB7XG4gICAgICBzdXBlcihncnBjQ29uZmlnKTtcbiAgICB9IGNhdGNoIChlcnJvcikge1xuICAgICAgZGlhZy5lcnJvcihgR1JQQ1NwYW5FeHBvcnRlcjogRXJyb3IgaW5pdGlhbGl6aW5nIE9UTFAgZXhwb3J0ZXI6YCwgZXJyb3IpO1xuICAgICAgdGhyb3cgZXJyb3I7XG4gICAgfVxuICAgIFxuICAgIHRoaXMudmVyYm9zZSA9IHZlcmJvc2U7XG4gICAgXG4gICAgaWYgKHRoaXMudmVyYm9zZSkge1xuICAgICAgZGlhZy5pbmZvKGBHUlBDU3BhbkV4cG9ydGVyOiBDb25maWd1cmVkIGZvciBlbmRwb2ludDogJHtlbmRwb2ludH1gKTtcbiAgICAgIGRpYWcuaW5mbyhgR1JQQ1NwYW5FeHBvcnRlcjogQXV0aGVudGljYXRpb246ICR7T2JqZWN0LmtleXMobG93ZXJfY2FzZV9hdXRoX2hlYWRlcikubGVuZ3RoID4gMCA/ICdFbmFibGVkJyA6ICdOb25lJ31gKTtcbiAgICB9XG4gIH1cblxuICBhc3luYyBzaHV0ZG93bigpOiBQcm9taXNlPHZvaWQ+IHtcbiAgICBpZiAodGhpcy52ZXJib3NlKSB7XG4gICAgICBkaWFnLmluZm8oXCJHUlBDU3BhbkV4cG9ydGVyOiBTaHV0dGluZyBkb3duLi4uXCIpO1xuICAgIH1cbiAgICByZXR1cm4gc3VwZXIuc2h1dGRvd24oKTtcbiAgfVxufVxuXG4vLyAtLS0gSGVscGVyIEZ1bmN0aW9ucyAtLS1cbmZ1bmN0aW9uIGdldEVudihrZXk6IHN0cmluZywgZGVmYXVsdFZhbHVlPzogc3RyaW5nKTogc3RyaW5nIHwgdW5kZWZpbmVkIHtcbiAgcmV0dXJuIHByb2Nlc3MuZW52W2tleV0gPz8gZGVmYXVsdFZhbHVlO1xufVxuXG5mdW5jdGlvbiBnZXRFbnZWZXJib3NlKCk6IGJvb2xlYW4gfCB1bmRlZmluZWQge1xuICBjb25zdCBlbnZWYXIgPSBnZXRFbnYoXCJGSV9WRVJCT1NFX0VYUE9SVEVSXCIpO1xuICBpZiAoZW52VmFyKSB7XG4gICAgcmV0dXJuIGVudlZhci50b0xvd2VyQ2FzZSgpID09PSBcInRydWVcIiB8fCBlbnZWYXIgPT09IFwiMVwiO1xuICB9XG4gIHJldHVybiB1bmRlZmluZWQ7XG59XG5cbmZ1bmN0aW9uIGdldEVudkZpQXV0aEhlYWRlcigpOiBGSUhlYWRlcnMgfCB1bmRlZmluZWQge1xuICBjb25zdCBhcGlLZXkgPSBnZXRFbnYoXCJGSV9BUElfS0VZXCIpO1xuICBjb25zdCBzZWNyZXRLZXkgPSBnZXRFbnYoXCJGSV9TRUNSRVRfS0VZXCIpO1xuICBpZiAoYXBpS2V5ICYmIHNlY3JldEtleSkge1xuICAgIC8vIFVzZSBsb3dlcmNhc2UgaGVhZGVycyBmb3IgZ1JQQyBjb21wYXRpYmlsaXR5XG4gICAgcmV0dXJuIHsgXCJ4LWFwaS1rZXlcIjogYXBpS2V5LCBcIngtc2VjcmV0LWtleVwiOiBzZWNyZXRLZXkgfTtcbiAgfVxuICByZXR1cm4gdW5kZWZpbmVkO1xufVxuXG5mdW5jdGlvbiBnZXRFbnZHcnBjQ29sbGVjdG9yRW5kcG9pbnQoKTogc3RyaW5nIHtcbiAgbGV0IGVuZHBvaW50ID0gZ2V0RW52KFwiRklfR1JQQ19DT0xMRUNUT1JfRU5EUE9JTlRcIikgPz8gXG4gICAgICAgICAgICAgICAgIGdldEVudihcIkZJX0dSUENfVVJMXCIpID8/IFxuICAgICAgICAgICAgICAgICBERUZBVUxUX0ZJX0dSUENfQ09MTEVDVE9SX0JBU0VfVVJMO1xuICAgICAgICAgICAgICAgICBcbiAgLy8gUmVtb3ZlIGh0dHA6Ly8gb3IgaHR0cHM6Ly8gcHJlZml4IGlmIHByZXNlbnQgKGdSUEMgZG9lc24ndCB1c2UgSFRUUCBwcm90b2NvbCBwcmVmaXgpXG4gIGVuZHBvaW50ID0gZW5kcG9pbnQucmVwbGFjZSgvXmh0dHBzPzpcXC9cXC8vLCAnJyk7XG4gIFxuICByZXR1cm4gZW5kcG9pbnQ7XG59XG5cbi8vIFRoaXMgZnVuY3Rpb24gbm93IGNvbnN0cnVjdHMgdGhlIEZVTEwgZW5kcG9pbnQgVVJMXG4vLyBJdCBwcmlvcml0aXplcyB0aGUgZW5kcG9pbnQgcGFzc2VkIGRpcmVjdGx5IHRvIGByZWdpc3RlcmAgKGlmIGFueSksXG4vLyB0aGVuIGNoZWNrcyBGSV9CQVNFX1VSTCAob3IgYSBtb3JlIHNwZWNpZmljIEZJX0NPTExFQ1RPUl9FTkRQT0lOVCBlbnYgdmFyKSxcbi8vIGFuZCBmaW5hbGx5IGZhbGxzIGJhY2sgdG8gYSBkZWZhdWx0IGJhc2UgVVJMLlxuZnVuY3Rpb24gY29uc3RydWN0RnVsbEVuZHBvaW50KGN1c3RvbUVuZHBvaW50Pzogc3RyaW5nKTogc3RyaW5nIHtcbiAgbGV0IGJhc2VVcmxUb1VzZTogc3RyaW5nO1xuXG4gIGlmIChjdXN0b21FbmRwb2ludCkge1xuICAgIHRyeSB7XG4gICAgICBjb25zdCBwYXJzZWRDdXN0b20gPSBuZXcgVVJMKGN1c3RvbUVuZHBvaW50KTtcbiAgICAgIGlmICghcGFyc2VkQ3VzdG9tLnByb3RvY29sIHx8ICFwYXJzZWRDdXN0b20uaG9zdCkge1xuICAgICAgICBkaWFnLndhcm4oYEN1c3RvbSBlbmRwb2ludCAnJHtjdXN0b21FbmRwb2ludH0nIGlzIG1pc3NpbmcgcHJvdG9jb2wgb3IgaG9zdC4gRmFsbGluZyBiYWNrIHRvIGVudmlyb25tZW50IG9yIGRlZmF1bHQuYCk7XG4gICAgICAgIGJhc2VVcmxUb1VzZSA9IGdldEVudihcIkZJX0JBU0VfVVJMXCIpID8/IGdldEVudihcIkZJX0NPTExFQ1RPUl9FTkRQT0lOVFwiKSA/PyBERUZBVUxUX0ZJX0NPTExFQ1RPUl9CQVNFX1VSTDtcbiAgICAgIH0gZWxzZSBpZiAocGFyc2VkQ3VzdG9tLnBhdGhuYW1lICE9PSBcIi9cIiAmJiBwYXJzZWRDdXN0b20ucGF0aG5hbWUgIT09IEZJX0NPTExFQ1RPUl9QQVRIKSB7XG4gICAgICAgIGRpYWcud2FybihgVXNpbmcgY3VzdG9tIGVuZHBvaW50IGFzIGZ1bGwgVVJMOiAke2N1c3RvbUVuZHBvaW50fWApO1xuICAgICAgICByZXR1cm4gY3VzdG9tRW5kcG9pbnQ7XG4gICAgICB9IGVsc2Uge1xuICAgICAgICBiYXNlVXJsVG9Vc2UgPSBgJHtwYXJzZWRDdXN0b20ucHJvdG9jb2x9Ly8ke3BhcnNlZEN1c3RvbS5ob3N0fWA7XG4gICAgICB9XG4gICAgfSBjYXRjaCAoZSkge1xuICAgICAgZGlhZy53YXJuKGBDdXN0b20gZW5kcG9pbnQgJyR7Y3VzdG9tRW5kcG9pbnR9JyBpcyBub3QgYSB2YWxpZCBVUkwuIEZhbGxpbmcgYmFjayB0byBlbnZpcm9ubWVudCBvciBkZWZhdWx0LmApO1xuICAgICAgYmFzZVVybFRvVXNlID0gZ2V0RW52KFwiRklfQkFTRV9VUkxcIikgPz8gZ2V0RW52KFwiRklfQ09MTEVDVE9SX0VORFBPSU5UXCIpID8/IERFRkFVTFRfRklfQ09MTEVDVE9SX0JBU0VfVVJMO1xuICAgIH1cbiAgfSBlbHNlIHtcbiAgICBiYXNlVXJsVG9Vc2UgPSBnZXRFbnYoXCJGSV9CQVNFX1VSTFwiKSA/PyBnZXRFbnYoXCJGSV9DT0xMRUNUT1JfRU5EUE9JTlRcIikgPz8gREVGQVVMVF9GSV9DT0xMRUNUT1JfQkFTRV9VUkw7XG4gIH1cbiAgXG4gIC8vIEVuc3VyZSBubyB0cmFpbGluZyBzbGFzaCBmcm9tIGJhc2VVcmwgYmVmb3JlIGFwcGVuZGluZyBwYXRoXG4gIGlmIChiYXNlVXJsVG9Vc2UuZW5kc1dpdGgoJy8nKSkge1xuICAgIGJhc2VVcmxUb1VzZSA9IGJhc2VVcmxUb1VzZS5zbGljZSgwLCAtMSk7XG4gIH1cblxuICByZXR1cm4gYCR7YmFzZVVybFRvVXNlfSR7RklfQ09MTEVDVE9SX1BBVEh9YDtcbn1cblxuXG4vLyAtLS0gVHJhY2VyUHJvdmlkZXIgLS0tXG5pbnRlcmZhY2UgRklUcmFjZXJQcm92aWRlck9wdGlvbnMge1xuICByZXNvdXJjZT86IFJlc291cmNlO1xuICB2ZXJib3NlPzogYm9vbGVhbjtcbiAgaWRHZW5lcmF0b3I/OiBJZEdlbmVyYXRvcjtcbiAgZW5kcG9pbnQ/OiBzdHJpbmc7IC8vIEN1c3RvbSBlbmRwb2ludCAoY2FuIGJlIGJhc2Ugb3IgZnVsbCwgX2NvbnN0cnVjdEZ1bGxFbmRwb2ludCB3aWxsIGhhbmRsZSlcbiAgaGVhZGVycz86IEZJSGVhZGVycztcbiAgdHJhbnNwb3J0PzogVHJhbnNwb3J0OyAvLyBUcmFuc3BvcnQgdHlwZVxufVxuXG5jbGFzcyBGSVRyYWNlclByb3ZpZGVyIGV4dGVuZHMgQmFzaWNUcmFjZXJQcm92aWRlciB7XG4gIHByaXZhdGUgZGVmYXVsdFByb2Nlc3NvckF0dGFjaGVkOiBib29sZWFuID0gZmFsc2U7XG4gIHByaXZhdGUgdmVyYm9zZTogYm9vbGVhbjtcbiAgcHJpdmF0ZSBlbmRwb2ludDogc3RyaW5nOyAvLyBUaGlzIHdpbGwgc3RvcmUgdGhlIGZ1bGx5IGNvbnN0cnVjdGVkIGVuZHBvaW50XG4gIHByaXZhdGUgaGVhZGVycz86IEZJSGVhZGVycztcbiAgcHJpdmF0ZSB0cmFuc3BvcnQ6IFRyYW5zcG9ydDtcblxuICBjb25zdHJ1Y3Rvcihjb25maWc6IEZJVHJhY2VyUHJvdmlkZXJPcHRpb25zID0ge30pIHtcbiAgICBjb25zdCBpZEdlbmVyYXRvciA9IGNvbmZpZy5pZEdlbmVyYXRvciA/PyBuZXcgVXVpZElkR2VuZXJhdG9yKCk7XG4gICAgY29uc3QgdHJhbnNwb3J0ID0gY29uZmlnLnRyYW5zcG9ydCA/PyBUcmFuc3BvcnQuSFRUUDtcbiAgICBcbiAgICBjb25zdCB2ZXJib3NlID0gY29uZmlnLnZlcmJvc2UgPz8gKGdldEVudihcIkZJX1ZFUkJPU0VfUFJPVklERVJcIik/LnRvTG93ZXJDYXNlKCkgPT09IFwidHJ1ZVwiKTtcbiAgICBcbiAgICAvLyBDb25zdHJ1Y3QgdGhlIGFwcHJvcHJpYXRlIGVuZHBvaW50IGJhc2VkIG9uIHRyYW5zcG9ydFxuICAgIGxldCBlbmRwb2ludDogc3RyaW5nO1xuICAgIGlmICh0cmFuc3BvcnQgPT09IFRyYW5zcG9ydC5HUlBDKSB7XG4gICAgICBlbmRwb2ludCA9IGNvbmZpZy5lbmRwb2ludCA/PyBnZXRFbnZHcnBjQ29sbGVjdG9yRW5kcG9pbnQoKTtcbiAgICB9IGVsc2Uge1xuICAgICAgZW5kcG9pbnQgPSBjb25zdHJ1Y3RGdWxsRW5kcG9pbnQoY29uZmlnLmVuZHBvaW50KTtcbiAgICB9XG4gICAgXG4gICAgY29uc3QgaGVhZGVycyA9IGNvbmZpZy5oZWFkZXJzID8/IGdldEVudkZpQXV0aEhlYWRlcigpO1xuXG4gICAgaWYgKHZlcmJvc2UpIHtcbiAgICAgIGRpYWcuaW5mbyhgRklUcmFjZXJQcm92aWRlcjogVXNpbmcgJHt0cmFuc3BvcnQudG9VcHBlckNhc2UoKX0gZXhwb3J0ZXIgZW5kcG9pbnQ6ICR7ZW5kcG9pbnR9YCk7XG4gICAgfVxuXG4gICAgLy8gQ3JlYXRlIHRoZSBhcHByb3ByaWF0ZSBleHBvcnRlclxuICAgIGxldCBleHBvcnRlcjogU3BhbkV4cG9ydGVyO1xuICAgIGlmICh0cmFuc3BvcnQgPT09IFRyYW5zcG9ydC5HUlBDKSB7XG4gICAgICBleHBvcnRlciA9IG5ldyBHUlBDU3BhbkV4cG9ydGVyKHsgZW5kcG9pbnQsIGhlYWRlcnMsIHZlcmJvc2U6IGNvbmZpZy52ZXJib3NlIH0pO1xuICAgIH0gZWxzZSB7XG4gICAgICBleHBvcnRlciA9IG5ldyBIVFRQU3BhbkV4cG9ydGVyKHsgZW5kcG9pbnQsIGhlYWRlcnMsIHZlcmJvc2U6IGNvbmZpZy52ZXJib3NlIH0pO1xuICAgIH1cbiAgICBcbiAgICBjb25zdCBkZWZhdWx0UHJvY2Vzc29yID0gbmV3IE9UZWxTaW1wbGVTcGFuUHJvY2Vzc29yKGV4cG9ydGVyKTtcbiAgICBzdXBlcih7IHJlc291cmNlOiBjb25maWcucmVzb3VyY2UsIGlkR2VuZXJhdG9yLCBzcGFuUHJvY2Vzc29yczogW2RlZmF1bHRQcm9jZXNzb3JdIH0pO1xuICAgIHRoaXMuZGVmYXVsdFByb2Nlc3NvckF0dGFjaGVkID0gdHJ1ZTtcbiAgICB0aGlzLnZlcmJvc2UgPSB2ZXJib3NlO1xuICAgIHRoaXMuZW5kcG9pbnQgPSBlbmRwb2ludDtcbiAgICB0aGlzLmhlYWRlcnMgPSBoZWFkZXJzO1xuICAgIHRoaXMudHJhbnNwb3J0ID0gdHJhbnNwb3J0O1xuXG4gICAgLy8gTG9nIHRvIGNvbmZpcm0gcHJvY2Vzc29yIGFuZCBleHBvcnRlciBkZXRhaWxzXG4gICAgaWYgKHZlcmJvc2UpIHtcbiAgICAgIGRpYWcuaW5mbyhgRklUcmFjZXJQcm92aWRlcjogRGVmYXVsdCBTaW1wbGVTcGFuUHJvY2Vzc29yIGFkZGVkIHdpdGggJHt0cmFuc3BvcnQudG9VcHBlckNhc2UoKX1TcGFuRXhwb3J0ZXIgdGFyZ2V0aW5nOiAke2VuZHBvaW50fWApO1xuICAgIH1cblxuICAgIGlmICh2ZXJib3NlKSB7XG4gICAgICB0aGlzLnByaW50VHJhY2luZ0RldGFpbHMoKTtcbiAgICB9XG4gIH1cblxuICBhZGRTcGFuUHJvY2Vzc29yKHNwYW5Qcm9jZXNzb3I6IFNwYW5Qcm9jZXNzb3IpOiB2b2lkIHtcbiAgICBpZiAodGhpcy5kZWZhdWx0UHJvY2Vzc29yQXR0YWNoZWQpIHtcbiAgICAgIGRpYWcud2FybihcbiAgICAgICAgXCJBZGRpbmcgYSBuZXcgU3BhblByb2Nlc3Nvci4gVGhlIGRlZmF1bHQgU2ltcGxlU3BhblByb2Nlc3NvciB3aWxsIGJlIHJlcGxhY2VkLlwiLFxuICAgICAgKTtcbiAgICAgICh0aGlzIGFzIGFueSkuX3JlZ2lzdGVyZWRTcGFuUHJvY2Vzc29ycyA9IFtdOyBcbiAgICAgIHRoaXMuZGVmYXVsdFByb2Nlc3NvckF0dGFjaGVkID0gZmFsc2U7IFxuICAgIH1cbiAgICAodGhpcyBhcyBhbnkpLl9yZWdpc3RlcmVkU3BhblByb2Nlc3NvcnMucHVzaChzcGFuUHJvY2Vzc29yKTtcbiAgfVxuXG4gIHByaXZhdGUgcHJpbnRUcmFjaW5nRGV0YWlscygpOiB2b2lkIHtcbiAgICBjb25zdCByZXNvdXJjZSA9ICh0aGlzIGFzIEJhc2ljVHJhY2VyUHJvdmlkZXIgYXMgYW55KS5yZXNvdXJjZTtcbiAgICBpZiAoIXJlc291cmNlKSB7XG4gICAgICBkaWFnLndhcm4oXCJObyByZXNvdXJjZSBhdmFpbGFibGUgZm9yIHRyYWNpbmcgZGV0YWlsc1wiKTtcbiAgICAgIHJldHVybjtcbiAgICB9XG5cbiAgICBjb25zdCBwcm9qZWN0TmFtZSA9IHJlc291cmNlLmF0dHJpYnV0ZXNbUFJPSkVDVF9OQU1FXSB8fCBcIk4vQVwiO1xuICAgIGNvbnN0IHByb2plY3RUeXBlID0gcmVzb3VyY2UuYXR0cmlidXRlc1tQUk9KRUNUX1RZUEVdO1xuICAgIGNvbnN0IHByb2plY3RWZXJzaW9uTmFtZSA9IHJlc291cmNlLmF0dHJpYnV0ZXNbUFJPSkVDVF9WRVJTSU9OX05BTUVdIHx8IFwiZGVmYXVsdFwiO1xuICAgIGNvbnN0IGV2YWxUYWdzID0gcmVzb3VyY2UuYXR0cmlidXRlc1tFVkFMX1RBR1NdIHx8IFtdO1xuICAgIGNvbnN0IHNlc3Npb25OYW1lID0gcmVzb3VyY2UuYXR0cmlidXRlc1tTRVNTSU9OX05BTUVdIHx8IFwiTi9BXCI7XG5cbiAgICBjb25zdCBwcm9jZXNzb3JOYW1lID0gdGhpcy5kZWZhdWx0UHJvY2Vzc29yQXR0YWNoZWQgPyBcIlNpbXBsZVNwYW5Qcm9jZXNzb3IgKGRlZmF1bHQpXCIgOiBcIkN1c3RvbS9NdWx0aXBsZVwiO1xuICAgIGNvbnN0IHRyYW5zcG9ydE5hbWUgPSB0aGlzLnRyYW5zcG9ydC50b1VwcGVyQ2FzZSgpOyBcblxuICAgIGNvbnN0IGRldGFpbHNIZWFkZXIgPVxuICAgICAgcHJvY2Vzcy5wbGF0Zm9ybSA9PT0gXCJ3aW4zMlwiXG4gICAgICAgID8gXCJPcGVuVGVsZW1ldHJ5IFRyYWNpbmcgRGV0YWlsc1wiXG4gICAgICAgIDogXCLwn5StIE9wZW5UZWxlbWV0cnkgVHJhY2luZyBEZXRhaWxzIPCflK1cIjtcblxuICAgIGxldCBkZXRhaWxzTXNnID0gYCR7ZGV0YWlsc0hlYWRlcn1cXG5gO1xuICAgIGRldGFpbHNNc2cgKz0gYHwgIEZJIFByb2plY3Q6ICR7cHJvamVjdE5hbWV9XFxuYDtcbiAgICBkZXRhaWxzTXNnICs9IGB8ICBGSSBQcm9qZWN0IFR5cGU6ICR7cHJvamVjdFR5cGV9XFxuYDtcbiAgICBkZXRhaWxzTXNnICs9IGB8ICBGSSBQcm9qZWN0IFZlcnNpb24gTmFtZTogJHtwcm9qZWN0VmVyc2lvbk5hbWV9XFxuYDtcbiAgICBkZXRhaWxzTXNnICs9IGB8ICBTcGFuIFByb2Nlc3NvcjogJHtwcm9jZXNzb3JOYW1lfVxcbmA7XG4gICAgZGV0YWlsc01zZyArPSBgfCAgQ29sbGVjdG9yIEVuZHBvaW50OiAke3RoaXMuZW5kcG9pbnR9XFxuYDsgLy8gTm93IHNob3dzIHRoZSBmdWxsIGVuZHBvaW50XG4gICAgZGV0YWlsc01zZyArPSBgfCAgVHJhbnNwb3J0OiAke3RyYW5zcG9ydE5hbWV9XFxuYDtcbiAgICBkZXRhaWxzTXNnICs9IGB8ICBUcmFuc3BvcnQgSGVhZGVyczogJHt0aGlzLmhlYWRlcnMgPyBPYmplY3Qua2V5cyh0aGlzLmhlYWRlcnMpLm1hcChoID0+IGAke2h9OiAqKioqYCkuam9pbignLCAnKSA6ICdOb25lJ31cXG5gO1xuICAgIGRldGFpbHNNc2cgKz0gYHwgIEV2YWwgVGFnczogJHt0eXBlb2YgZXZhbFRhZ3MgPT09ICdzdHJpbmcnID8gZXZhbFRhZ3MgOiBKU09OLnN0cmluZ2lmeShldmFsVGFncyl9XFxuYDtcbiAgICBkZXRhaWxzTXNnICs9IGB8ICBTZXNzaW9uIE5hbWU6ICR7c2Vzc2lvbk5hbWV9XFxuYDtcbiAgICBkZXRhaWxzTXNnICs9IFwifCAgXFxuXCI7XG4gICAgaWYgKHRoaXMuZGVmYXVsdFByb2Nlc3NvckF0dGFjaGVkKSB7XG4gICAgICBkZXRhaWxzTXNnICs9IFwifCAgVXNpbmcgYSBkZWZhdWx0IFNwYW5Qcm9jZXNzb3IuIGBhZGRTcGFuUHJvY2Vzc29yYCB3aWxsIG92ZXJ3cml0ZSB0aGlzIGRlZmF1bHQuXFxuXCI7XG4gICAgfVxuICB9XG4gIGFzeW5jIHNodXRkb3duKCk6IFByb21pc2U8dm9pZD4ge1xuICAgIGlmICh0aGlzLnZlcmJvc2UpIHtcbiAgICAgIGRpYWcuaW5mbyhcIlNodXR0aW5nIGRvd24gRkkgVHJhY2VyUHJvdmlkZXIuLi5cIik7XG4gICAgfVxuICAgIHJldHVybiBzdXBlci5zaHV0ZG93bigpO1xuICB9XG59XG5cbmNsYXNzIFNpbXBsZVNwYW5Qcm9jZXNzb3IgZXh0ZW5kcyBPVGVsU2ltcGxlU3BhblByb2Nlc3NvciB7fVxuY2xhc3MgQmF0Y2hTcGFuUHJvY2Vzc29yIGV4dGVuZHMgT1RlbEJhdGNoU3BhblByb2Nlc3NvciB7fVxuXG5leHBvcnQgaW50ZXJmYWNlIFJlZ2lzdGVyT3B0aW9ucyB7XG4gIHByb2plY3ROYW1lPzogc3RyaW5nO1xuICBwcm9qZWN0VHlwZT86IFByb2plY3RUeXBlO1xuICBwcm9qZWN0VmVyc2lvbk5hbWU/OiBzdHJpbmc7XG4gIGV2YWxUYWdzPzogRXZhbFRhZ1tdO1xuICBzZXNzaW9uTmFtZT86IHN0cmluZztcbiAgbWV0YWRhdGE/OiBSZWNvcmQ8c3RyaW5nLCBhbnk+O1xuICBiYXRjaD86IGJvb2xlYW47XG4gIHNldEdsb2JhbFRyYWNlclByb3ZpZGVyPzogYm9vbGVhbjtcbiAgaGVhZGVycz86IEZJSGVhZGVycztcbiAgdmVyYm9zZT86IGJvb2xlYW47XG4gIGVuZHBvaW50Pzogc3RyaW5nOyAvLyBDYW4gYmUgYSBiYXNlIFVSTCBvciBhIGZ1bGwgVVJMLiBfY29uc3RydWN0RnVsbEVuZHBvaW50IHdpbGwgcmVzb2x2ZS5cbiAgaWRHZW5lcmF0b3I/OiBJZEdlbmVyYXRvcjtcbiAgdHJhbnNwb3J0PzogVHJhbnNwb3J0OyAvLyBUcmFuc3BvcnQgdHlwZTogSFRUUCBvciBHUlBDXG59XG5cbmZ1bmN0aW9uIHJlZ2lzdGVyKG9wdGlvbnM6IFJlZ2lzdGVyT3B0aW9ucyA9IHt9KTogRklUcmFjZXJQcm92aWRlciB7XG4gIGNvbnN0IHtcbiAgICBwcm9qZWN0TmFtZTogb3B0UHJvamVjdE5hbWUsXG4gICAgcHJvamVjdFR5cGUgPSBQcm9qZWN0VHlwZS5FWFBFUklNRU5ULFxuICAgIHByb2plY3RWZXJzaW9uTmFtZTogb3B0UHJvamVjdFZlcnNpb25OYW1lLFxuICAgIGV2YWxUYWdzOiBvcHRFdmFsVGFncyA9IFtdLFxuICAgIHNlc3Npb25OYW1lLFxuICAgIG1ldGFkYXRhID0ge30sXG4gICAgYmF0Y2ggPSBmYWxzZSxcbiAgICBzZXRHbG9iYWxUcmFjZXJQcm92aWRlciA9IHRydWUsXG4gICAgaGVhZGVyczogb3B0SGVhZGVycyxcbiAgICB2ZXJib3NlID0gZmFsc2UsXG4gICAgZW5kcG9pbnQ6IG9wdEVuZHBvaW50LFxuICAgIGlkR2VuZXJhdG9yID0gbmV3IFV1aWRJZEdlbmVyYXRvcigpLFxuICAgIHRyYW5zcG9ydCA9IFRyYW5zcG9ydC5IVFRQLFxuICB9ID0gb3B0aW9ucztcblxuICBjb25zdCBwcmVwYXJlZEV2YWxUYWdzID0gcHJlcGFyZUV2YWxUYWdzKG9wdEV2YWxUYWdzKTtcblxuICBpZiAocHJvamVjdFR5cGUgPT09IFByb2plY3RUeXBlLk9CU0VSVkUpIHtcbiAgICBpZiAocHJlcGFyZWRFdmFsVGFncy5sZW5ndGggPiAwKSB7XG4gICAgICB0aHJvdyBuZXcgRXJyb3IoXCJFdmFsIHRhZ3MgYXJlIG5vdCBhbGxvd2VkIGZvciBwcm9qZWN0IHR5cGUgT0JTRVJWRVwiKTtcbiAgICB9XG4gICAgaWYgKG9wdFByb2plY3RWZXJzaW9uTmFtZSkge1xuICAgICAgdGhyb3cgbmV3IEVycm9yKFxuICAgICAgICBcIlByb2plY3QgVmVyc2lvbiBOYW1lIG5vdCBhbGxvd2VkIGZvciBwcm9qZWN0IHR5cGUgT0JTRVJWRVwiLFxuICAgICAgKTtcbiAgICB9XG4gIH1cblxuICBpZiAocHJvamVjdFR5cGUgPT09IFByb2plY3RUeXBlLkVYUEVSSU1FTlQpIHtcbiAgICBpZiAoc2Vzc2lvbk5hbWUpIHtcbiAgICAgIHRocm93IG5ldyBFcnJvcihcbiAgICAgICAgXCJTZXNzaW9uIG5hbWUgaXMgbm90IGFsbG93ZWQgZm9yIHByb2plY3QgdHlwZSBFWFBFUklNRU5UXCIsXG4gICAgICApO1xuICAgIH1cbiAgfVxuXG5jb25zdCBwcm9qZWN0TmFtZSA9IG9wdFByb2plY3ROYW1lID8/IGdldEVudihcIkZJX1BST0pFQ1RfTkFNRVwiKTtcbmNvbnN0IHByb2plY3RWZXJzaW9uTmFtZSA9IG9wdFByb2plY3RWZXJzaW9uTmFtZSA/PyBnZXRFbnYoXCJGSV9QUk9KRUNUX1ZFUlNJT05fTkFNRVwiKSA/PyBcIkRFRkFVTFRcIjtcbmNvbnN0IHByb2plY3RWZXJzaW9uSWQgPSB1dWlkdjQoKTsgXG5cbmlmICghcHJvamVjdE5hbWUpIHtcbiAgdGhyb3cgbmV3IEVycm9yKFwiRklfUFJPSkVDVF9OQU1FIGlzIG5vdCBzZXRcIik7XG59XG5cbiAgY29uc3QgY3VzdG9tRXZhbE5hbWVzID0gcHJlcGFyZWRFdmFsVGFncy5tYXAodGFnID0+IHRhZy5jdXN0b21fZXZhbF9uYW1lKS5maWx0ZXIobmFtZSA9PiBuYW1lICYmIG5hbWUubGVuZ3RoID4gMCk7XG4gIGlmIChjdXN0b21FdmFsTmFtZXMubGVuZ3RoICE9PSBuZXcgU2V0KGN1c3RvbUV2YWxOYW1lcykuc2l6ZSkge1xuICAgIHRocm93IG5ldyBFcnJvcihcIkR1cGxpY2F0ZSBjdXN0b20gZXZhbCBuYW1lcyBhcmUgbm90IGFsbG93ZWRcIik7XG4gIH1cblxuICAvLyBDYWxsIGNoZWNrQ3VzdG9tRXZhbENvbmZpZ0V4aXN0cyB3aXRob3V0IGF3YWl0IChmaXJlLWFuZC1mb3JnZXQpXG4gIC8vIEl0IHdpbGwgbG9nIGFuIGVycm9yIGludGVybmFsbHkgaWYgYSBjb25maWcgZXhpc3RzLlxuICBpZiAocHJlcGFyZWRFdmFsVGFncy5sZW5ndGggPiAwKSB7XG4gICAgY2hlY2tDdXN0b21FdmFsQ29uZmlnRXhpc3RzKFxuICAgICAgcHJvamVjdE5hbWUsXG4gICAgICBwcmVwYXJlZEV2YWxUYWdzLFxuICAgICAgb3B0RW5kcG9pbnQsXG4gICAgICB2ZXJib3NlXG4gICAgKS50aGVuKGN1c3RvbUV2YWxDb25maWdFeGlzdHMgPT4ge1xuICAgICAgaWYgKGN1c3RvbUV2YWxDb25maWdFeGlzdHMpIHtcbiAgICAgICAgLy8gTG9nIGFuIGVycm9yIGluc3RlYWQgb2YgdGhyb3dpbmcsIGFzIHJlZ2lzdGVyIGhhcyBhbHJlYWR5IHJldHVybmVkLlxuICAgICAgICBkaWFnLmVycm9yKFxuICAgICAgICAgIGByZWdpc3RlcjogQ3VzdG9tIGV2YWwgY29uZmlndXJhdGlvbiBhbHJlYWR5IGV4aXN0cyBmb3IgcHJvamVjdCAnJHtwcm9qZWN0TmFtZX0nLiBgICtcbiAgICAgICAgICBcIlRoZSBTREsgd2lsbCBjb250aW51ZSB0byBpbml0aWFsaXplLCBidXQgdGhpcyBtYXkgbGVhZCB0byB1bmV4cGVjdGVkIGJlaGF2aW9yIG9yIGR1cGxpY2F0ZSBjb25maWd1cmF0aW9ucy4gXCIgK1xuICAgICAgICAgIFwiUGxlYXNlIHVzZSBhIGRpZmZlcmVudCBwcm9qZWN0IG5hbWUgb3IgZGlzYWJsZS9tb2RpZnkgdGhlIGV4aXN0aW5nIGN1c3RvbSBldmFsIGNvbmZpZ3VyYXRpb24uXCJcbiAgICAgICAgKTtcbiAgICAgIH1cbiAgICB9KS5jYXRjaChlcnJvciA9PiB7XG4gICAgICAgIC8vIExvZyBhbnkgZXJyb3IgZnJvbSB0aGUgY2hlY2sgaXRzZWxmXG4gICAgICAgIGRpYWcuZXJyb3IoYHJlZ2lzdGVyOiBFcnJvciBkdXJpbmcgYmFja2dyb3VuZCBjaGVja0N1c3RvbUV2YWxDb25maWdFeGlzdHMgZm9yIHByb2plY3QgJyR7cHJvamVjdE5hbWV9JzogJHtlcnJvcn1gKTtcbiAgICB9KTtcbiAgfVxuXG4gIGNvbnN0IHJlc291cmNlQXR0cmlidXRlczogQXR0cmlidXRlcyA9IHtcbiAgICBbU2VtYW50aWNSZXNvdXJjZUF0dHJpYnV0ZXMuU0VSVklDRV9OQU1FXTogcHJvamVjdE5hbWUsXG4gICAgW1BST0pFQ1RfTkFNRV06IHByb2plY3ROYW1lLFxuICAgIFtQUk9KRUNUX1RZUEVdOiBwcm9qZWN0VHlwZSxcbiAgICBbUFJPSkVDVF9WRVJTSU9OX05BTUVdOiBwcm9qZWN0VmVyc2lvbk5hbWUsXG4gICAgW1BST0pFQ1RfVkVSU0lPTl9JRF06IHByb2plY3RWZXJzaW9uSWQsXG4gICAgW0VWQUxfVEFHU106IEpTT04uc3RyaW5naWZ5KHByZXBhcmVkRXZhbFRhZ3MpLFxuICAgIFtNRVRBREFUQV06IEpTT04uc3RyaW5naWZ5KG1ldGFkYXRhKSxcbiAgfTtcblxuICBpZiAocHJvamVjdFR5cGUgPT09IFByb2plY3RUeXBlLk9CU0VSVkUgJiYgc2Vzc2lvbk5hbWUpIHtcbiAgICByZXNvdXJjZUF0dHJpYnV0ZXNbU0VTU0lPTl9OQU1FXSA9IHNlc3Npb25OYW1lO1xuICB9XG5cbiAgY29uc3QgZGV0ZWN0ZWQgPSBkZXRlY3RSZXNvdXJjZXMoKTtcbiAgY29uc3QgcmVzb3VyY2UgPSBkZXRlY3RlZC5tZXJnZShyZXNvdXJjZUZyb21BdHRyaWJ1dGVzKHJlc291cmNlQXR0cmlidXRlcykpO1xuXG4gIFxuICAvLyBIZWFkZXJzIGZvciB0aGUgZXhwb3J0ZXJcbiAgY29uc3QgZXhwb3J0ZXJIZWFkZXJzID0gb3B0SGVhZGVycyA/PyBnZXRFbnZGaUF1dGhIZWFkZXIoKTtcbiAgLy8gRW5kcG9pbnQgZm9yIHRoZSBleHBvcnRlciBpcyBub3cgZGV0ZXJtaW5lZCBieSBGSVRyYWNlclByb3ZpZGVyJ3MgY29uc3RydWN0b3JcbiAgLy8gdXNpbmcgX2NvbnN0cnVjdEZ1bGxFbmRwb2ludCwgd2hpY2ggY29uc2lkZXJzIG9wdEVuZHBvaW50IGFuZCBlbnYgdmFycy5cblxuICBjb25zdCB0cmFjZXJQcm92aWRlciA9IG5ldyBGSVRyYWNlclByb3ZpZGVyKHtcbiAgICByZXNvdXJjZSxcbiAgICB2ZXJib3NlLFxuICAgIGlkR2VuZXJhdG9yLFxuICAgIGVuZHBvaW50OiBvcHRFbmRwb2ludCxcbiAgICBoZWFkZXJzOiBleHBvcnRlckhlYWRlcnMsXG4gICAgdHJhbnNwb3J0LFxuICB9KTtcblxuICBpZiAoYmF0Y2gpIHtcbiAgICBsZXQgYmF0Y2hFeHBvcnRlcjogU3BhbkV4cG9ydGVyO1xuICAgIGlmICh0cmFuc3BvcnQgPT09IFRyYW5zcG9ydC5HUlBDKSB7XG4gICAgICBiYXRjaEV4cG9ydGVyID0gbmV3IEdSUENTcGFuRXhwb3J0ZXIoe1xuICAgICAgICBlbmRwb2ludDogKHRyYWNlclByb3ZpZGVyIGFzIGFueSkuZW5kcG9pbnQsXG4gICAgICAgIGhlYWRlcnM6IGV4cG9ydGVySGVhZGVycyxcbiAgICAgICAgdmVyYm9zZTogdmVyYm9zZVxuICAgICAgfSk7XG4gICAgfSBlbHNlIHtcbiAgICAgIGJhdGNoRXhwb3J0ZXIgPSBuZXcgSFRUUFNwYW5FeHBvcnRlcih7XG4gICAgICAgIGVuZHBvaW50OiAodHJhY2VyUHJvdmlkZXIgYXMgYW55KS5lbmRwb2ludCxcbiAgICAgICAgaGVhZGVyczogZXhwb3J0ZXJIZWFkZXJzLFxuICAgICAgICB2ZXJib3NlOiB2ZXJib3NlXG4gICAgICB9KTtcbiAgICB9XG4gICAgY29uc3QgYmF0Y2hQcm9jZXNzb3IgPSBuZXcgT1RlbEJhdGNoU3BhblByb2Nlc3NvcihiYXRjaEV4cG9ydGVyKTtcblxuICAgICh0cmFjZXJQcm92aWRlciBhcyBhbnkpLl9yZWdpc3RlcmVkU3BhblByb2Nlc3NvcnMgPSBbXTtcbiAgICAodHJhY2VyUHJvdmlkZXIgYXMgYW55KS5fZGVmYXVsdFByb2Nlc3NvckF0dGFjaGVkID0gZmFsc2U7XG4gICAgdHJhY2VyUHJvdmlkZXIuYWRkU3BhblByb2Nlc3NvcihiYXRjaFByb2Nlc3Nvcik7XG4gIH1cblxuICBpZiAoc2V0R2xvYmFsVHJhY2VyUHJvdmlkZXIpIHtcbiAgICB0cmFjZS5zZXRHbG9iYWxUcmFjZXJQcm92aWRlcih0cmFjZXJQcm92aWRlcik7XG4gICAgaWYgKHZlcmJvc2UpIHtcbiAgICAgIGRpYWcuaW5mbyhcbiAgICAgICAgXCJ8ICBcXG5cIiArXG4gICAgICAgIFwifCAgYHJlZ2lzdGVyYCBoYXMgc2V0IHRoaXMgVHJhY2VyUHJvdmlkZXIgYXMgdGhlIGdsb2JhbCBPcGVuVGVsZW1ldHJ5IGRlZmF1bHQuXFxuXCIgK1xuICAgICAgICBcInwgIFRvIGRpc2FibGUgdGhpcyBiZWhhdmlvciwgY2FsbCBgcmVnaXN0ZXJgIHdpdGggXCIgK1xuICAgICAgICBcImBzZXRfZ2xvYmFsX3RyYWNlcl9wcm92aWRlcj1mYWxzZWAuXFxuXCJcbiAgICAgICk7XG4gICAgfVxuICB9XG5cbiAgcmV0dXJuIHRyYWNlclByb3ZpZGVyO1xufVxuXG5pbnRlcmZhY2UgQ2hlY2tFeGlzdHNSZXNwb25zZSB7XG4gIHJlc3VsdD86IHtcbiAgICBleGlzdHM/OiBib29sZWFuO1xuICB9O1xuICAvLyBBZGQgb3RoZXIgZmllbGRzIGlmIHRoZSBBUEkgcmV0dXJucyBtb3JlXG59XG5cbmV4cG9ydCBpbnRlcmZhY2UgQ2hlY2tDdXN0b21FdmFsVGVtcGxhdGVFeGlzdHNSZXNwb25zZSB7XG4gIHJlc3VsdD86IHtcbiAgICBpc1VzZXJFdmFsVGVtcGxhdGU/OiBib29sZWFuO1xuICAgIGV2YWxUZW1wbGF0ZT86IGFueVxuICB9O1xuICAvLyBBZGQgb3RoZXIgZmllbGRzIGlmIHRoZSBBUEkgcmV0dXJucyBtb3JlXG59XG5cbmFzeW5jIGZ1bmN0aW9uIGNoZWNrQ3VzdG9tRXZhbENvbmZpZ0V4aXN0cyhcbiAgcHJvamVjdE5hbWU6IHN0cmluZyxcbiAgZXZhbFRhZ3M6IGFueVtdLCAvLyBFeHBlY3RzIHJlc3VsdCBvZiBwcmVwYXJlRXZhbFRhZ3NcbiAgY3VzdG9tRW5kcG9pbnQ/OiBzdHJpbmcsIC8vIENhbiBiZSBiYXNlIG9yIGZ1bGwgVVJMIGZvciB0aGUgQVBJIGNhbGwgaXRzZWxmXG4gIHZlcmJvc2U/OiBib29sZWFuXG4pOiBQcm9taXNlPGJvb2xlYW4+IHtcbiAgaWYgKCFldmFsVGFncyB8fCBldmFsVGFncy5sZW5ndGggPT09IDApIHtcbiAgICByZXR1cm4gZmFsc2U7XG4gIH1cblxuICBsZXQgYXBpQmFzZVVybDogc3RyaW5nO1xuICBpZiAoY3VzdG9tRW5kcG9pbnQpIHtcbiAgICB0cnkge1xuICAgICAgY29uc3QgcGFyc2VkQ3VzdG9tID0gbmV3IFVSTChjdXN0b21FbmRwb2ludCk7XG4gICAgICBpZiAocGFyc2VkQ3VzdG9tLnBhdGhuYW1lLmVuZHNXaXRoKEZJX0NVU1RPTV9FVkFMX0NPTkZJR19DSEVDS19QQVRIKSkge1xuICAgICAgICBpZiAodmVyYm9zZSkgZGlhZy5pbmZvKGBjaGVja0N1c3RvbUV2YWxDb25maWdFeGlzdHM6IFVzaW5nIGN1c3RvbSBmdWxsIGVuZHBvaW50OiAke2N1c3RvbUVuZHBvaW50fWApO1xuICAgICAgICBhcGlCYXNlVXJsID0gY3VzdG9tRW5kcG9pbnQuc3Vic3RyaW5nKDAsIGN1c3RvbUVuZHBvaW50Lmxhc3RJbmRleE9mKEZJX0NVU1RPTV9FVkFMX0NPTkZJR19DSEVDS19QQVRIKSk7XG4gICAgICB9IGVsc2UgaWYgKHBhcnNlZEN1c3RvbS5wYXRobmFtZSA9PT0gXCIvXCIgfHwgcGFyc2VkQ3VzdG9tLnBhdGhuYW1lID09PSBcIlwiKSB7IFxuICAgICAgICAgYXBpQmFzZVVybCA9IGAke3BhcnNlZEN1c3RvbS5wcm90b2NvbH0vLyR7cGFyc2VkQ3VzdG9tLmhvc3R9YDtcbiAgICAgIH0gZWxzZSB7IFxuICAgICAgICAgYXBpQmFzZVVybCA9IGN1c3RvbUVuZHBvaW50OyBcbiAgICAgIH1cbiAgICB9IGNhdGNoIChlKSB7XG4gICAgICBpZiAodmVyYm9zZSkgZGlhZy53YXJuKGBjaGVja0N1c3RvbUV2YWxDb25maWdFeGlzdHM6IEN1c3RvbSBlbmRwb2ludCAnJHtjdXN0b21FbmRwb2ludH0nIGlzIG5vdCBhIHZhbGlkIFVSTC4gRmFsbGluZyBiYWNrIHRvIGVudmlyb25tZW50IG9yIGRlZmF1bHQuYCk7XG4gICAgICBhcGlCYXNlVXJsID0gZ2V0RW52KFwiRklfQkFTRV9VUkxcIikgPz8gZ2V0RW52KFwiRklfQ09MTEVDVE9SX0VORFBPSU5UXCIpID8/IERFRkFVTFRfRklfQ09MTEVDVE9SX0JBU0VfVVJMO1xuICAgIH1cbiAgfSBlbHNlIHtcbiAgICBhcGlCYXNlVXJsID0gZ2V0RW52KFwiRklfQkFTRV9VUkxcIikgPz8gZ2V0RW52KFwiRklfQ09MTEVDVE9SX0VORFBPSU5UXCIpID8/IERFRkFVTFRfRklfQ09MTEVDVE9SX0JBU0VfVVJMO1xuICB9XG5cbiAgaWYgKGFwaUJhc2VVcmwuZW5kc1dpdGgoJy8nKSkge1xuICAgIGFwaUJhc2VVcmwgPSBhcGlCYXNlVXJsLnNsaWNlKDAsIC0xKTtcbiAgfVxuICBjb25zdCB1cmwgPSBgJHthcGlCYXNlVXJsfSR7RklfQ1VTVE9NX0VWQUxfQ09ORklHX0NIRUNLX1BBVEh9YDtcblxuICBjb25zdCBoZWFkZXJzOiBGSUhlYWRlcnMgPSB7XG4gICAgXCJDb250ZW50LVR5cGVcIjogXCJhcHBsaWNhdGlvbi9qc29uXCIsXG4gICAgLi4uKGdldEVudkZpQXV0aEhlYWRlcigpIHx8IHt9KSxcbiAgfTtcblxuICBjb25zdCBwYXlsb2FkID0ge1xuICAgIHByb2plY3RfbmFtZTogcHJvamVjdE5hbWUsXG4gICAgZXZhbF90YWdzOiBldmFsVGFncyxcbiAgfTtcblxuICBpZiAodmVyYm9zZSkge1xuICAgIGRpYWcuaW5mbyhgY2hlY2tDdXN0b21FdmFsQ29uZmlnRXhpc3RzOiBDaGVja2luZyBjdXN0b20gZXZhbCBjb25maWcgYXQgJHt1cmx9IHdpdGggcGF5bG9hZDpgLCBKU09OLnN0cmluZ2lmeShwYXlsb2FkLCBudWxsLCAyKSk7XG4gIH1cblxuICB0cnkge1xuICAgIGNvbnN0IHJlc3BvbnNlID0gYXdhaXQgZmV0Y2godXJsLCB7XG4gICAgICBtZXRob2Q6IFwiUE9TVFwiLFxuICAgICAgaGVhZGVyczogaGVhZGVycyxcbiAgICAgIGJvZHk6IEpTT04uc3RyaW5naWZ5KHBheWxvYWQpLFxuICAgIH0pO1xuXG4gICAgaWYgKCFyZXNwb25zZS5vaykge1xuICAgICAgY29uc3QgZXJyb3JUZXh0ID0gYXdhaXQgcmVzcG9uc2UudGV4dCgpO1xuICAgICAgZGlhZy5lcnJvcihcbiAgICAgICAgYGNoZWNrQ3VzdG9tRXZhbENvbmZpZ0V4aXN0czogRmFpbGVkIHRvIGNoZWNrIGN1c3RvbSBldmFsIGNvbmZpZzogJHtyZXNwb25zZS5zdGF0dXN9ICR7cmVzcG9uc2Uuc3RhdHVzVGV4dH0gLSAke2Vycm9yVGV4dH1gLFxuICAgICAgKTtcbiAgICAgIHJldHVybiBmYWxzZTsgXG4gICAgfVxuXG4gICAgY29uc3QgcmVzdWx0ID0gYXdhaXQgcmVzcG9uc2UuanNvbigpIGFzIENoZWNrRXhpc3RzUmVzcG9uc2U7XG4gICAgaWYgKHZlcmJvc2UpIHtcbiAgICAgICAgZGlhZy5pbmZvKFwiY2hlY2tDdXN0b21FdmFsQ29uZmlnRXhpc3RzOiBSZXNwb25zZSBmcm9tIHNlcnZlcjpcIiwgSlNPTi5zdHJpbmdpZnkocmVzdWx0LCBudWxsLCAyKSk7XG4gICAgfVxuICAgIHJldHVybiByZXN1bHQ/LnJlc3VsdD8uZXhpc3RzID09PSB0cnVlO1xuICB9IGNhdGNoIChlcnJvcikge1xuICAgIGRpYWcuZXJyb3IoYGNoZWNrQ3VzdG9tRXZhbENvbmZpZ0V4aXN0czogRXJyb3IgY2hlY2tpbmcgY3VzdG9tIGV2YWwgY29uZmlnOiAke2Vycm9yfWApO1xuICAgIHJldHVybiBmYWxzZTtcbiAgfVxufVxuXG5leHBvcnQgYXN5bmMgZnVuY3Rpb24gY2hlY2tDdXN0b21FdmFsVGVtcGxhdGVFeGlzdHMoXG4gIGV2YWxfdGVtcGxhdGVfbmFtZTogc3RyaW5nLFxuICB2ZXJib3NlPzogYm9vbGVhbixcbiAgY3VzdG9tRW5kcG9pbnQ/OiBzdHJpbmcsXG4pOiBQcm9taXNlPENoZWNrQ3VzdG9tRXZhbFRlbXBsYXRlRXhpc3RzUmVzcG9uc2U+IHtcbiAgaWYgKCFldmFsX3RlbXBsYXRlX25hbWUgfHwgZXZhbF90ZW1wbGF0ZV9uYW1lLmxlbmd0aCA9PT0gMCkge1xuICAgIGNvbnN0IHJlc3BvbnNlOiBDaGVja0N1c3RvbUV2YWxUZW1wbGF0ZUV4aXN0c1Jlc3BvbnNlID0ge1xuICAgICAgcmVzdWx0OiB7XG4gICAgICAgIGlzVXNlckV2YWxUZW1wbGF0ZTogZmFsc2UsXG4gICAgICAgIGV2YWxUZW1wbGF0ZTogbnVsbFxuICAgICAgfVxuICAgIH1cbiAgICByZXR1cm4gcmVzcG9uc2U7XG4gIH1cblxuICBsZXQgYXBpQmFzZVVybDogc3RyaW5nO1xuICBpZiAoY3VzdG9tRW5kcG9pbnQpIHtcbiAgICB0cnkge1xuICAgICAgY29uc3QgcGFyc2VkQ3VzdG9tID0gbmV3IFVSTChjdXN0b21FbmRwb2ludCk7XG4gICAgICBpZiAocGFyc2VkQ3VzdG9tLnBhdGhuYW1lLmVuZHNXaXRoKEZJX0NVU1RPTV9FVkFMX1RFTVBMQVRFX0NIRUNLX1BBVEgpKSB7XG4gICAgICAgIGlmICh2ZXJib3NlKSBkaWFnLmluZm8oYGNoZWNrQ3VzdG9tRXZhbFRlbXBsYXRlRXhpc3RzOiBVc2luZyBjdXN0b20gZnVsbCBlbmRwb2ludDogJHtjdXN0b21FbmRwb2ludH1gKTtcbiAgICAgICAgYXBpQmFzZVVybCA9IGN1c3RvbUVuZHBvaW50LnN1YnN0cmluZygwLCBjdXN0b21FbmRwb2ludC5sYXN0SW5kZXhPZihGSV9DVVNUT01fRVZBTF9URU1QTEFURV9DSEVDS19QQVRIKSk7XG4gICAgICB9IGVsc2UgaWYgKHBhcnNlZEN1c3RvbS5wYXRobmFtZSA9PT0gXCIvXCIgfHwgcGFyc2VkQ3VzdG9tLnBhdGhuYW1lID09PSBcIlwiKSB7IFxuICAgICAgICAgYXBpQmFzZVVybCA9IGAke3BhcnNlZEN1c3RvbS5wcm90b2NvbH0vLyR7cGFyc2VkQ3VzdG9tLmhvc3R9YDtcbiAgICAgIH0gZWxzZSB7IFxuICAgICAgICAgYXBpQmFzZVVybCA9IGN1c3RvbUVuZHBvaW50OyBcbiAgICAgIH1cbiAgICB9IGNhdGNoIChlKSB7XG4gICAgICBpZiAodmVyYm9zZSkgZGlhZy53YXJuKGBjaGVja0N1c3RvbUV2YWxUZW1wbGF0ZUV4aXN0czogQ3VzdG9tIGVuZHBvaW50ICcke2N1c3RvbUVuZHBvaW50fScgaXMgbm90IGEgdmFsaWQgVVJMLiBGYWxsaW5nIGJhY2sgdG8gZW52aXJvbm1lbnQgb3IgZGVmYXVsdC5gKTtcbiAgICAgIGFwaUJhc2VVcmwgPSBnZXRFbnYoXCJGSV9CQVNFX1VSTFwiKSA/PyBnZXRFbnYoXCJGSV9DT0xMRUNUT1JfRU5EUE9JTlRcIikgPz8gREVGQVVMVF9GSV9DT0xMRUNUT1JfQkFTRV9VUkw7XG4gICAgfVxuICB9IGVsc2Uge1xuICAgIGFwaUJhc2VVcmwgPSBnZXRFbnYoXCJGSV9CQVNFX1VSTFwiKSA/PyBnZXRFbnYoXCJGSV9DT0xMRUNUT1JfRU5EUE9JTlRcIikgPz8gREVGQVVMVF9GSV9DT0xMRUNUT1JfQkFTRV9VUkw7XG4gIH1cblxuICBpZiAoYXBpQmFzZVVybC5lbmRzV2l0aCgnLycpKSB7XG4gICAgYXBpQmFzZVVybCA9IGFwaUJhc2VVcmwuc2xpY2UoMCwgLTEpO1xuICB9XG4gIGNvbnN0IHVybCA9IGAke2FwaUJhc2VVcmx9JHtGSV9DVVNUT01fRVZBTF9URU1QTEFURV9DSEVDS19QQVRIfWA7XG5cbiAgY29uc3QgaGVhZGVyczogRklIZWFkZXJzID0ge1xuICAgIFwiQ29udGVudC1UeXBlXCI6IFwiYXBwbGljYXRpb24vanNvblwiLFxuICAgIC4uLihnZXRFbnZGaUF1dGhIZWFkZXIoKSB8fCB7fSksXG4gIH07XG5cbiAgY29uc3QgcGF5bG9hZCA9IHtcbiAgICBldmFsX3RlbXBsYXRlX25hbWU6IGV2YWxfdGVtcGxhdGVfbmFtZVxuICB9O1xuXG4gIGlmICh2ZXJib3NlKSB7XG4gICAgZGlhZy5pbmZvKGBjaGVja0N1c3RvbUV2YWxUZW1wbGF0ZUV4aXN0czogQ2hlY2tpbmcgY3VzdG9tIGV2YWwgdGVtcGxhdGUgYXQgJHt1cmx9IHdpdGggcGF5bG9hZDpgLCBKU09OLnN0cmluZ2lmeShwYXlsb2FkLCBudWxsLCAyKSk7XG4gIH1cblxuICB0cnkge1xuICAgIGNvbnN0IHJlc3BvbnNlID0gYXdhaXQgZmV0Y2godXJsLCB7XG4gICAgICBtZXRob2Q6IFwiUE9TVFwiLFxuICAgICAgaGVhZGVyczogaGVhZGVycyxcbiAgICAgIGJvZHk6IEpTT04uc3RyaW5naWZ5KHBheWxvYWQpLFxuICAgIH0pO1xuXG4gICAgaWYgKCFyZXNwb25zZS5vaykge1xuICAgICAgY29uc3QgZXJyb3JUZXh0ID0gYXdhaXQgcmVzcG9uc2UudGV4dCgpO1xuICAgICAgZGlhZy5lcnJvcihcbiAgICAgICAgYGNoZWNrQ3VzdG9tRXZhbFRlbXBsYXRlRXhpc3RzOiBGYWlsZWQgdG8gY2hlY2sgY3VzdG9tIGV2YWwgdGVtcGxhdGU6ICR7cmVzcG9uc2Uuc3RhdHVzfSAke3Jlc3BvbnNlLnN0YXR1c1RleHR9IC0gJHtlcnJvclRleHR9YCxcbiAgICAgICk7XG4gICAgICByZXR1cm4ge1xuICAgICAgICByZXN1bHQ6IHtcbiAgICAgICAgICBpc1VzZXJFdmFsVGVtcGxhdGU6IGZhbHNlLFxuICAgICAgICAgIGV2YWxUZW1wbGF0ZTogbnVsbFxuICAgICAgICB9XG4gICAgICB9O1xuICAgIH1cblxuICAgIGNvbnN0IHJlc3VsdCA9IGF3YWl0IHJlc3BvbnNlLmpzb24oKSBhcyBDaGVja0N1c3RvbUV2YWxUZW1wbGF0ZUV4aXN0c1Jlc3BvbnNlO1xuICAgIGlmICh2ZXJib3NlKSB7XG4gICAgICAgIGRpYWcuaW5mbyhcImNoZWNrQ3VzdG9tRXZhbFRlbXBsYXRlRXhpc3RzOiBSZXNwb25zZSBmcm9tIHNlcnZlcjpcIiwgSlNPTi5zdHJpbmdpZnkocmVzdWx0LCBudWxsLCAyKSk7XG4gICAgfVxuICAgIHJldHVybiByZXN1bHQ7XG4gIH0gY2F0Y2ggKGVycm9yKSB7XG4gICAgZGlhZy5lcnJvcihgY2hlY2tDdXN0b21FdmFsVGVtcGxhdGVFeGlzdHM6IEVycm9yIGNoZWNraW5nIGN1c3RvbSBldmFsIHRlbXBsYXRlOiAke2Vycm9yfWApO1xuICAgIHJldHVybiB7XG4gICAgICByZXN1bHQ6IHtcbiAgICAgICAgaXNVc2VyRXZhbFRlbXBsYXRlOiBmYWxzZSxcbiAgICAgICAgZXZhbFRlbXBsYXRlOiBudWxsXG4gICAgICB9XG4gICAgfTtcbiAgfVxufVxuXG5leHBvcnQge1xuICByZWdpc3RlcixcbiAgRklUcmFjZXJQcm92aWRlcixcbiAgU2ltcGxlU3BhblByb2Nlc3NvcixcbiAgQmF0Y2hTcGFuUHJvY2Vzc29yLFxuICBIVFRQU3BhbkV4cG9ydGVyLFxuICBHUlBDU3BhbkV4cG9ydGVyLFxuICBVdWlkSWRHZW5lcmF0b3IsXG4gIGNoZWNrQ3VzdG9tRXZhbENvbmZpZ0V4aXN0c1xufVxuXG5cbi8vIFRPRE86XG4vLyAtIEltcGxlbWVudCBwcmVwYXJlRXZhbFRhZ3MgKHNpbWlsYXIgdG8gUHl0aG9uKVxuLy8gLSBJbXBsZW1lbnQgY2hlY2tDdXN0b21FdmFsQ29uZmlnRXhpc3RzXG4vLyAtIFJlZmluZSBlcnJvciBoYW5kbGluZyBhbmQgbG9nZ2luZ1xuLy8gLSBBZGQgdGVzdHMgIl19