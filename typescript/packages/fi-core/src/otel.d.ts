import { ProjectType, EvalTag } from "./fi_types";
import { ExportResult } from "@opentelemetry/core";
import { ReadableSpan, SpanExporter, SpanProcessor } from "@opentelemetry/sdk-trace-base";
import { OTLPTraceExporter as _OTLPGRPCTraceExporter } from "@opentelemetry/exporter-trace-otlp-grpc";
import { BasicTracerProvider, BatchSpanProcessor as OTelBatchSpanProcessor, SimpleSpanProcessor as OTelSimpleSpanProcessor, IdGenerator } from "@opentelemetry/sdk-trace-node";
import { Resource } from "@opentelemetry/resources";
export declare const PROJECT_NAME = "project_name";
export declare const PROJECT_TYPE = "project_type";
export declare const PROJECT_VERSION_NAME = "project_version_name";
export declare const PROJECT_VERSION_ID = "project_version_id";
export declare const EVAL_TAGS = "eval_tags";
export declare const METADATA = "metadata";
export declare const SESSION_NAME = "session_name";
export declare enum Transport {
    HTTP = "http",
    GRPC = "grpc"
}
interface FIHeaders {
    [key: string]: string;
}
interface HTTPSpanExporterOptions {
    endpoint: string;
    headers?: FIHeaders;
    verbose?: boolean;
}
declare class UuidIdGenerator implements IdGenerator {
    generateTraceId(): string;
    generateSpanId(): string;
}
declare class HTTPSpanExporter implements SpanExporter {
    private readonly endpoint;
    private readonly headers;
    private isShutdown;
    private verbose;
    constructor(options: HTTPSpanExporterOptions);
    private formatTraceId;
    private formatSpanId;
    private convertAttributes;
    private getSpanStatusName;
    export(spans: ReadableSpan[], resultCallback: (result: ExportResult) => void): void;
    shutdown(): Promise<void>;
    forceFlush?(): Promise<void>;
}
declare class GRPCSpanExporter extends _OTLPGRPCTraceExporter {
    private verbose;
    constructor(options: {
        endpoint: string;
        headers?: FIHeaders;
        verbose?: boolean;
        [key: string]: any;
    });
    shutdown(): Promise<void>;
}
interface FITracerProviderOptions {
    resource?: Resource;
    verbose?: boolean;
    idGenerator?: IdGenerator;
    endpoint?: string;
    headers?: FIHeaders;
    transport?: Transport;
}
declare class FITracerProvider extends BasicTracerProvider {
    private defaultProcessorAttached;
    private verbose;
    private endpoint;
    private headers?;
    private transport;
    constructor(config?: FITracerProviderOptions);
    addSpanProcessor(spanProcessor: SpanProcessor): void;
    private printTracingDetails;
    shutdown(): Promise<void>;
}
declare class SimpleSpanProcessor extends OTelSimpleSpanProcessor {
}
declare class BatchSpanProcessor extends OTelBatchSpanProcessor {
}
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
    endpoint?: string;
    idGenerator?: IdGenerator;
    transport?: Transport;
}
declare function register(options?: RegisterOptions): FITracerProvider;
export interface CheckCustomEvalTemplateExistsResponse {
    result?: {
        isUserEvalTemplate?: boolean;
        evalTemplate?: any;
    };
}
declare function checkCustomEvalConfigExists(projectName: string, evalTags: any[], // Expects result of prepareEvalTags
customEndpoint?: string, // Can be base or full URL for the API call itself
verbose?: boolean): Promise<boolean>;
export declare function checkCustomEvalTemplateExists(eval_template_name: string, verbose?: boolean, customEndpoint?: string): Promise<CheckCustomEvalTemplateExistsResponse>;
export { register, FITracerProvider, SimpleSpanProcessor, BatchSpanProcessor, HTTPSpanExporter, GRPCSpanExporter, UuidIdGenerator, checkCustomEvalConfigExists };
