/**
 * OpenTelemetry instrumentation for OpenAI Agents SDK.
 *
 * This module provides tracing for the @openai/agents SDK by implementing
 * a TracingProcessor that captures agent runs, tool calls, and handoffs.
 */

import {
  InstrumentationBase,
  InstrumentationNodeModuleDefinition,
} from "@opentelemetry/instrumentation";
import {
  Span,
  SpanKind,
  SpanStatusCode,
  context,
  trace,
} from "@opentelemetry/api";
import { FITracer, TraceConfigOptions } from "@traceai/fi-core";
import {
  SemanticConventions,
  FISpanKind,
  MimeType,
  LLMProvider,
} from "@traceai/fi-semantic-conventions";
import { VERSION } from "./version";

/**
 * `exception.type` / `error.type` for a span the Agents SDK marked as failed. Its span errors
 * are plain `{ message, data }` objects with no class or code.
 */
export const AGENT_SPAN_ERROR_TYPE = "AgentSpanError";

// Type definitions for @openai/agents (simplified for instrumentation)
interface AgentTrace {
  traceId: string;
  name: string;
  groupId?: string | null;
  metadata?: Record<string, unknown>;
}

interface AgentSpan {
  spanId: string;
  traceId: string;
  parentId?: string | null;
  startedAt?: string | null;
  endedAt?: string | null;
  spanData: SpanData;
  error?: { message: string; data?: string | Record<string, unknown> } | null;
}

/**
 * Span data as emitted by @openai/agents (agents-core `SpanData`). The SDK
 * uses snake_case keys for every span type, from 0.4.x through 0.21.x.
 */
interface SpanData {
  type: string;
  name?: string;
  input?: unknown;
  output?: unknown;
  model?: string;
  model_config?: Record<string, unknown>;
  usage?: { input_tokens?: number; output_tokens?: number; [key: string]: unknown };
  /** Response spans: the request input and the full Responses API object. */
  _input?: unknown;
  _response?: unknown;
  response_id?: string;
  /** Handoff spans. */
  from_agent?: string;
  to_agent?: string;
  /** Guardrail and MCP tool-discovery spans. */
  triggered?: boolean;
  server?: string;
  result?: string[];
  /** User data on custom spans. */
  data?: Record<string, unknown>;
}

/**
 * @openai/agents versions this package is tested against. Used both for the
 * require-hook module definition and documented as the peer dependency range.
 */
export const SUPPORTED_AGENTS_VERSIONS = [">=0.4.0 <0.22.0"];

interface TracingProcessor {
  onTraceStart(trace: AgentTrace): Promise<void>;
  onTraceEnd(trace: AgentTrace): Promise<void>;
  onSpanStart(span: AgentSpan): Promise<void>;
  onSpanEnd(span: AgentSpan): Promise<void>;
  forceFlush(): Promise<void>;
  shutdown(): Promise<void>;
}

export interface FITracingProcessorOptions {
  /**
   * Top-level SDK trace metadata keys approved for export. Defaults to none.
   * Values (including nested content) are exported as metadata, not input/output;
   * only allow keys whose entire value is safe to send to your tracing backend.
   */
  traceMetadataKeys?: readonly string[];
}

export interface OpenAIAgentsInstrumentationConfig extends FITracingProcessorOptions {
  instrumentationConfig?: Record<string, unknown>;
  traceConfig?: TraceConfigOptions;
  /**
   * When true (the default), the Future AGI processor replaces the SDK's
   * trace processors via `setTraceProcessors`, so traces are no longer sent to
   * the OpenAI dashboard. Set to false to add the processor with
   * `addTraceProcessor` and keep the SDK's default OpenAI exporter. Mirrors
   * `exclusive_processor` in the Python `traceAI-openai-agents` package.
   */
  exclusiveProcessor?: boolean;
}

/**
 * FI Tracing Processor for OpenAI Agents SDK.
 *
 * Implements the TracingProcessor interface to capture agent traces
 * and convert them to OpenTelemetry spans.
 */
export class FITracingProcessor implements TracingProcessor {
  private _tracer: FITracer;
  private _rootSpans: Map<string, Span> = new Map();
  private _otelSpans: Map<string, Span> = new Map();
  private _traceInputs: Map<string, unknown> = new Map();
  private _traceOutputs: Map<string, unknown> = new Map();
  private _handoffs: Map<string, Map<string, string>> = new Map();
  private readonly _traceMetadataKeys: readonly string[];
  private _shutdown = false;

  constructor(tracer: FITracer, options: FITracingProcessorOptions = {}) {
    this._tracer = tracer;
    this._traceMetadataKeys = [...(options.traceMetadataKeys ?? [])];
  }

  async onTraceStart(agentTrace: AgentTrace): Promise<void> {
    if (this._shutdown || this._rootSpans.has(agentTrace.traceId)) return;
    // Do not serialize the trace object: it can also carry exporter credentials.
    const traceMetadata = agentTrace.metadata ?? {};
    const metadata = Object.fromEntries(this._traceMetadataKeys
      .filter(key => Object.prototype.hasOwnProperty.call(traceMetadata, key))
      .map(key => [key, traceMetadata[key]]));
    const span = this._tracer.startSpan(agentTrace.name, {
      kind: SpanKind.INTERNAL,
      attributes: {
        [SemanticConventions.FI_SPAN_KIND]: FISpanKind.AGENT,
        ...(agentTrace.groupId != null ? { [SemanticConventions.SESSION_ID]: agentTrace.groupId } : {}),
        ...(Object.keys(metadata).length ? { [SemanticConventions.METADATA]: safeJsonStringify(metadata) } : {}),
      },
    });
    this._rootSpans.set(agentTrace.traceId, span);
  }

  async onTraceEnd(agentTrace: AgentTrace): Promise<void> {
    if (this._shutdown) return;
    const rootSpan = this._rootSpans.get(agentTrace.traceId);
    if (!rootSpan) return;

    const input = this._traceInputs.get(agentTrace.traceId);
    if (input) {
      if (typeof input === "string") {
        rootSpan.setAttribute(SemanticConventions.INPUT_VALUE, input);
      } else {
        rootSpan.setAttribute(SemanticConventions.INPUT_MIME_TYPE, MimeType.JSON);
        rootSpan.setAttribute(
          SemanticConventions.INPUT_VALUE,
          safeJsonStringify(input)
        );
      }
    }

    const output = this._traceOutputs.get(agentTrace.traceId);
    if (output) {
      rootSpan.setAttribute(
        SemanticConventions.RAW_OUTPUT,
        safeJsonStringify(output)
      );
      const textOutput = getTextFromResponse(output);
      if (textOutput) {
        rootSpan.setAttribute(SemanticConventions.OUTPUT_VALUE, textOutput);
      }
    }

    rootSpan.setStatus({ code: SpanStatusCode.OK });
    rootSpan.end();

    this._rootSpans.delete(agentTrace.traceId);
    this._traceInputs.delete(agentTrace.traceId);
    this._traceOutputs.delete(agentTrace.traceId);
    this._handoffs.delete(agentTrace.traceId);
  }

  async onSpanStart(agentSpan: AgentSpan): Promise<void> {
    if (this._shutdown || !agentSpan.startedAt || this._otelSpans.has(agentSpan.spanId)) return;

    const parentSpan = agentSpan.parentId
      ? this._otelSpans.get(agentSpan.parentId)
      : this._rootSpans.get(agentSpan.traceId);

    const parentContext = parentSpan
      ? trace.setSpan(context.active(), parentSpan)
      : undefined;

    const spanName = getSpanName(agentSpan);
    const span = this._tracer.startSpan(
      spanName,
      {
        kind: SpanKind.INTERNAL,
        startTime: nativeTime(agentSpan.startedAt),
        // Do not dump spanData into raw.input: it may already contain outputs,
        // which would bypass hideOutputs. Capture directional fields at end.
        attributes: {
          [SemanticConventions.FI_SPAN_KIND]: getSpanKind(agentSpan.spanData),
          [SemanticConventions.LLM_PROVIDER]: LLMProvider.OPENAI,
        },
      },
      parentContext
    );

    this._otelSpans.set(agentSpan.spanId, span);
  }

  async onSpanEnd(agentSpan: AgentSpan): Promise<void> {
    if (this._shutdown) return;
    const span = this._otelSpans.get(agentSpan.spanId);
    if (!span) return;

    span.updateName(getSpanName(agentSpan));
    const data = agentSpan.spanData;

    // Handle different span types
    if (data.type === "response") {
      this.handleResponseSpan(span, agentSpan);
    } else if (data.type === "generation") {
      this.handleGenerationSpan(span, agentSpan);
    } else if (data.type === "function") {
      this.handleFunctionSpan(span, data);
    } else if (data.type === "guardrail" && typeof data.triggered === "boolean") {
      span.setAttribute("guardrail.triggered", data.triggered);
    } else if (data.type === "mcp_tools") {
      span.setAttribute(SemanticConventions.TOOL_NAME, "list_tools");
      if (data.server) span.setAttribute("mcp.server.name", data.server);
      if (data.result !== undefined) {
        span.setAttribute(SemanticConventions.OUTPUT_VALUE, safeJsonStringify(data.result));
        span.setAttribute(SemanticConventions.OUTPUT_MIME_TYPE, MimeType.JSON);
      }
    } else if (data.type === "custom" && data.data !== undefined) {
      span.setAttribute(SemanticConventions.OUTPUT_VALUE, safeJsonStringify(data.data));
      span.setAttribute(SemanticConventions.OUTPUT_MIME_TYPE, MimeType.JSON);
    } else if (data.type === "handoff" && data.to_agent && data.from_agent) {
      const handoffs = this._handoffs.get(agentSpan.traceId) ?? new Map<string, string>();
      handoffs.set(data.to_agent, data.from_agent);
      this._handoffs.set(agentSpan.traceId, handoffs);
    } else if (data.type === "agent" && data.name) {
      span.setAttribute(SemanticConventions.GRAPH_NODE_ID, data.name);
      const handoffs = this._handoffs.get(agentSpan.traceId);
      const parentNode = handoffs?.get(data.name);
      if (parentNode) {
        span.setAttribute(SemanticConventions.GRAPH_NODE_PARENT_ID, parentNode);
        handoffs?.delete(data.name);
      }
    }

    // Set status based on error
    if (agentSpan.error) {
      const { message, data } = agentSpan.error;
      // The status description is the error message only. `data` (e.g. the tool's
      // error text) can quote content, so it only goes on the exception event, which
      // FISpan strips when error details are hidden. The SDK error has no class; a
      // stable name gives exception.type / error.type a value.
      span.recordException({
        name: AGENT_SPAN_ERROR_TYPE,
        message:
          data == null || data === ""
            ? message
            : `${message}: ${typeof data === "string" ? data : safeJsonStringify(data)}`,
      });
      span.setStatus({ code: SpanStatusCode.ERROR, message });
    } else {
      span.setStatus({ code: SpanStatusCode.OK });
    }

    span.end(nativeTime(agentSpan.endedAt));
    this._otelSpans.delete(agentSpan.spanId);
  }

  private handleResponseSpan(span: Span, agentSpan: AgentSpan): void {
    const data = agentSpan.spanData;
    const input = data._input;
    const response = data._response;

    // Track inputs for root span
    if (!this._traceInputs.has(agentSpan.traceId) && input) {
      this._traceInputs.set(agentSpan.traceId, input);
    }

    // Track outputs for root span
    if (response) {
      this._traceOutputs.set(agentSpan.traceId, response);
      span.setAttribute(SemanticConventions.OUTPUT_MIME_TYPE, MimeType.JSON);
      span.setAttribute(
        SemanticConventions.RAW_OUTPUT,
        safeJsonStringify(response)
      );
      const textOutput = getTextFromResponse(response);
      if (textOutput) {
        span.setAttribute(SemanticConventions.OUTPUT_VALUE, textOutput);
      }
      const resp = response as { model?: unknown; usage?: unknown };
      if (typeof resp.model === "string") {
        span.setAttribute(SemanticConventions.LLM_MODEL_NAME, resp.model);
      }
      setTokenCounts(span, resp.usage);
    }

    if (input) {
      span.setAttribute(SemanticConventions.RAW_INPUT, safeJsonStringify(input));
      if (typeof input === "string") {
        span.setAttribute(SemanticConventions.INPUT_VALUE, input);
      } else {
        span.setAttribute(SemanticConventions.INPUT_MIME_TYPE, MimeType.JSON);
        span.setAttribute(
          SemanticConventions.INPUT_VALUE,
          safeJsonStringify(input)
        );
      }
    }
  }

  private handleGenerationSpan(span: Span, agentSpan: AgentSpan): void {
    const data = agentSpan.spanData;
    if (data.model) {
      span.setAttribute(SemanticConventions.LLM_MODEL_NAME, data.model);
    }
    if (data.model_config) {
      span.setAttribute(
        SemanticConventions.LLM_INVOCATION_PARAMETERS,
        safeJsonStringify(data.model_config)
      );
    }
    if (data.input) {
      if (!this._traceInputs.has(agentSpan.traceId)) {
        this._traceInputs.set(agentSpan.traceId, data.input);
      }
      const inputJson = safeJsonStringify(data.input);
      span.setAttribute(SemanticConventions.RAW_INPUT, inputJson);
      span.setAttribute(SemanticConventions.INPUT_VALUE, inputJson);
      span.setAttribute(SemanticConventions.INPUT_MIME_TYPE, MimeType.JSON);
    }
    if (data.output) {
      span.setAttribute(SemanticConventions.RAW_OUTPUT, safeJsonStringify(data.output));
      span.setAttribute(SemanticConventions.OUTPUT_MIME_TYPE, MimeType.JSON);
      // The Chat Completions model records the raw completion as output[0].
      const completion = Array.isArray(data.output) ? data.output[0] : undefined;
      const textOutput = getTextFromResponse(completion);
      if (textOutput) {
        span.setAttribute(SemanticConventions.OUTPUT_VALUE, textOutput);
      }
      if (completion) {
        this._traceOutputs.set(agentSpan.traceId, completion);
      }
      // Usage is only set on the span data in some code paths; fall back to
      // the completion's own usage block.
      if (!data.usage && completion && typeof completion === "object") {
        setTokenCounts(span, (completion as { usage?: unknown }).usage);
      }
    }
    if (data.usage) {
      setTokenCounts(span, data.usage);
    }
  }

  private handleFunctionSpan(span: Span, data: SpanData): void {
    if (data.name) {
      span.setAttribute(SemanticConventions.TOOL_NAME, data.name);
    }
    if (data.input) {
      span.setAttribute(
        SemanticConventions.INPUT_VALUE,
        safeJsonStringify(data.input)
      );
      span.setAttribute(SemanticConventions.INPUT_MIME_TYPE, MimeType.JSON);
    }
    if (data.output !== undefined) {
      const outputValue =
        typeof data.output === "string"
          ? data.output
          : safeJsonStringify(data.output);
      span.setAttribute(SemanticConventions.OUTPUT_VALUE, outputValue);
      if (
        typeof data.output === "string" &&
        data.output.startsWith("{") &&
        data.output.endsWith("}")
      ) {
        span.setAttribute(SemanticConventions.OUTPUT_MIME_TYPE, MimeType.JSON);
      }
    }
  }

  async forceFlush(): Promise<void> {
    // This bridge has no export queue. Flush the owning OTel provider instead.
  }

  async shutdown(): Promise<void> {
    if (this._shutdown) return;
    // The SDK has no removeTraceProcessor API and may retain these callbacks.
    this._shutdown = true;
    const endTime = new Date();
    // Children are started after their parents. Close deepest spans first.
    for (const span of [...this._otelSpans.values()].reverse().concat([...this._rootSpans.values()])) {
      span.setStatus({ code: SpanStatusCode.ERROR, message: "Tracing processor shut down before completion" });
      span.end(endTime);
    }
    this._rootSpans.clear();
    this._otelSpans.clear();
    this._traceInputs.clear();
    this._traceOutputs.clear();
    this._handoffs.clear();
  }
}

/**
 * OpenAI Agents Instrumentation class.
 *
 * Provides automatic instrumentation for the @openai/agents SDK.
 */
export class OpenAIAgentsInstrumentation extends InstrumentationBase {
  private fiTracer!: FITracer;
  private _traceConfig?: TraceConfigOptions;
  private _processor?: FITracingProcessor;
  private _exclusiveProcessor: boolean;
  private _traceMetadataKeys: readonly string[];
  private _patchedModule?: typeof import("@openai/agents") & { _fiPatched?: boolean };

  constructor(config: OpenAIAgentsInstrumentationConfig = {}) {
    super(
      "@traceai/fi-instrumentation-openai-agents",
      VERSION,
      config.instrumentationConfig || {}
    );
    this._traceConfig = config.traceConfig;
    this._exclusiveProcessor = config.exclusiveProcessor ?? true;
    this._traceMetadataKeys = [...(config.traceMetadataKeys ?? [])];
    // InstrumentationBase calls enable() from its constructor, before
    // _traceConfig is set. Build the tracer again so the config applies.
    this.fiTracer = this._createFITracer();
  }

  override enable(): void {
    super.enable();
    this.fiTracer = this._createFITracer();
  }

  override disable(): void {
    super.disable();
    // require hooks do not call unpatch for ESM/manual registration.
    if (this._patchedModule) this.unpatch(this._patchedModule);
    this._processor?.shutdown();
    this._processor = undefined;
  }

  private _createFITracer(): FITracer {
    // Always pass an options object: given undefined, fi-core returns the
    // defaults without reading FI_HIDE_INPUTS / FI_HIDE_OUTPUTS.
    return new FITracer({
      tracer: this.tracer,
      traceConfig: this._traceConfig ?? {},
    });
  }

  protected init() {
    const module = new InstrumentationNodeModuleDefinition(
      "@openai/agents",
      SUPPORTED_AGENTS_VERSIONS,
      this.patch.bind(this),
      this.unpatch.bind(this)
    );
    return module;
  }

  /**
   * Manually instrument the @openai/agents module.
   *
   * Use this when the module is imported before instrumentation is registered.
   */
  manuallyInstrument(agentsModule: typeof import("@openai/agents")): void {
    this.patch(agentsModule);
  }

  private patch(
    agentsModule: typeof import("@openai/agents") & { _fiPatched?: boolean }
  ): typeof import("@openai/agents") {
    if (agentsModule?._fiPatched || _isFIPatched) {
      return agentsModule;
    }

    // Create the processor. Rebuild the tracer first so it uses the provider
    // from setTracerProvider() when that was called after construction.
    const processor = this.getProcessor();

    // Register our processor with the agents SDK. By default it replaces the
    // SDK's processors; with exclusiveProcessor: false it is added alongside
    // the default OpenAI exporter.
    try {
      if (
        this._exclusiveProcessor &&
        typeof agentsModule.setTraceProcessors === "function"
      ) {
        agentsModule.setTraceProcessors([processor]);
      } else if (typeof agentsModule.addTraceProcessor === "function") {
        agentsModule.addTraceProcessor(processor);
      } else {
        return agentsModule;
      }
    } catch (error) {
      this._diag.warn("Failed to register trace processor with @openai/agents", error);
      return agentsModule;
    }

    this._patchedModule = agentsModule;
    markModulePatched(agentsModule, true);
    _isFIPatched = true;
    return agentsModule;
  }

  private unpatch(
    agentsModule: typeof import("@openai/agents") & { _fiPatched?: boolean }
  ): void {
    // Another instrumentation instance may own this module's registration.
    if (this._patchedModule !== agentsModule) return;
    this._processor?.shutdown();
    this._processor = undefined;
    this._patchedModule = undefined;
    markModulePatched(agentsModule, false);
    _isFIPatched = false;
  }

  /**
   * Get the tracing processor for manual registration.
   *
   * Use this if you want to register the processor yourself.
   */
  getProcessor(): FITracingProcessor {
    if (!this._processor) {
      this.fiTracer = this._createFITracer();
      this._processor = new FITracingProcessor(this.fiTracer, { traceMetadataKeys: this._traceMetadataKeys });
    }
    return this._processor;
  }
}

// Global patched flag
let _isFIPatched = false;

/**
 * Record the patched state on the module object when it allows it. An ES
 * module namespace (`import * as agents from "@openai/agents"`) is not
 * extensible, so the assignment throws there; the global flag still applies.
 */
function markModulePatched(agentsModule: { _fiPatched?: boolean }, value: boolean): void {
  try {
    agentsModule._fiPatched = value;
  } catch {
    // Not extensible (ES module namespace object).
  }
}

/**
 * Check if the module has been patched.
 */
export function isPatched(): boolean {
  return _isFIPatched;
}

/**
 * Resets the internal patched state. Intended for testing only.
 */
export function _resetPatchedStateForTesting(): void {
  _isFIPatched = false;
}

// Helper functions

function nativeTime(value?: string | null): Date | undefined {
  if (!value) return undefined;
  const time = new Date(value);
  return Number.isFinite(time.getTime()) ? time : undefined;
}

function safeJsonStringify(obj: unknown): string {
  try {
    return JSON.stringify(obj);
  } catch {
    return String(obj);
  }
}

/**
 * Set token counts from an OpenAI usage block. Accepts the Responses API and
 * agents-core shape (input_tokens / output_tokens) and the Chat Completions
 * shape (prompt_tokens / completion_tokens).
 */
function setTokenCounts(span: Span, usage: unknown): void {
  if (!usage || typeof usage !== "object") return;
  const u = usage as Record<string, unknown>;
  const prompt = firstNumber(u.input_tokens, u.prompt_tokens);
  const completion = firstNumber(u.output_tokens, u.completion_tokens);
  let total = firstNumber(u.total_tokens);
  if (total === undefined && prompt !== undefined && completion !== undefined) {
    total = prompt + completion;
  }
  if (prompt !== undefined) {
    span.setAttribute(SemanticConventions.LLM_TOKEN_COUNT_PROMPT, prompt);
  }
  if (completion !== undefined) {
    span.setAttribute(SemanticConventions.LLM_TOKEN_COUNT_COMPLETION, completion);
  }
  if (total !== undefined) {
    span.setAttribute(SemanticConventions.LLM_TOKEN_COUNT_TOTAL, total);
  }
}

function firstNumber(...values: unknown[]): number | undefined {
  for (const value of values) {
    if (typeof value === "number" && Number.isFinite(value)) return value;
  }
  return undefined;
}

function getSpanName(span: AgentSpan): string {
  const data = span.spanData;
  if (data.name && typeof data.name === "string") {
    return data.name;
  }
  if (data.type === "handoff" && data.to_agent) {
    return `handoff to ${data.to_agent}`;
  }
  return data.type;
}

function getSpanKind(data: SpanData): string {
  switch (data.type) {
    case "agent":
      return FISpanKind.AGENT;
    case "function":
    case "handoff":
    case "mcp_tools":
      return FISpanKind.TOOL;
    case "guardrail":
      return FISpanKind.GUARDRAIL;
    case "generation":
    case "response":
      return FISpanKind.LLM;
    default:
      return FISpanKind.CHAIN;
  }
}

function getTextFromResponse(response: unknown): string | undefined {
  if (!response || typeof response !== "object") return undefined;
  const resp = response as {
    output?: Array<{ type?: string; content?: Array<{ type?: string; text?: string }> }>;
    choices?: Array<{ message?: { content?: unknown } }>;
  };
  // Chat Completions object (generation spans)
  if (Array.isArray(resp.choices)) {
    const texts = resp.choices
      .map((choice) => choice?.message?.content)
      .filter((content): content is string => typeof content === "string" && content.length > 0);
    return texts.length > 0 ? texts.join(" ") : undefined;
  }
  // Responses API object (response spans)
  if (!resp.output || !Array.isArray(resp.output)) return undefined;

  const texts: string[] = [];
  for (const item of resp.output) {
    if (item.type === "message" && item.content) {
      for (const contentItem of item.content) {
        if (contentItem.type === "output_text" && contentItem.text) {
          texts.push(contentItem.text);
        }
      }
    }
  }
  return texts.length > 0 ? texts.join(" ") : undefined;
}

