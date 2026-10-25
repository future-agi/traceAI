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

// Type definitions for @openai/agents (simplified for instrumentation)
interface AgentTrace {
  traceId: string;
  name: string;
}

interface AgentSpan {
  spanId: string;
  traceId: string;
  parentId?: string;
  startedAt?: string;
  endedAt?: string;
  spanData: SpanData;
  error?: { message: string; data?: string };
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
}

/**
 * @openai/agents versions this package is tested against. Used both for the
 * require-hook module definition and documented as the peer dependency range.
 */
export const SUPPORTED_AGENTS_VERSIONS = [">=0.4.0 <0.22.0"];

interface TracingProcessor {
  onTraceStart(trace: AgentTrace): void;
  onTraceEnd(trace: AgentTrace): void;
  onSpanStart(span: AgentSpan): void;
  onSpanEnd(span: AgentSpan): void;
  forceFlush(): void;
  shutdown(): void;
}

export interface OpenAIAgentsInstrumentationConfig {
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
  private _handoffs: Map<string, string> = new Map();

  constructor(tracer: FITracer) {
    this._tracer = tracer;
  }

  onTraceStart(agentTrace: AgentTrace): void {
    const span = this._tracer.startSpan(agentTrace.name, {
      kind: SpanKind.INTERNAL,
      attributes: {
        [SemanticConventions.FI_SPAN_KIND]: FISpanKind.AGENT,
      },
    });
    this._rootSpans.set(agentTrace.traceId, span);
  }

  onTraceEnd(agentTrace: AgentTrace): void {
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
  }

  onSpanStart(agentSpan: AgentSpan): void {
    if (!agentSpan.startedAt) return;

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
        attributes: {
          [SemanticConventions.FI_SPAN_KIND]: getSpanKind(agentSpan.spanData),
          [SemanticConventions.LLM_PROVIDER]: LLMProvider.OPENAI,
          [SemanticConventions.RAW_INPUT]: safeJsonStringify(agentSpan.spanData),
        },
      },
      parentContext
    );

    this._otelSpans.set(agentSpan.spanId, span);
  }

  onSpanEnd(agentSpan: AgentSpan): void {
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
    } else if (data.type === "handoff" && data.to_agent && data.from_agent) {
      const key = `${data.to_agent}:${agentSpan.traceId}`;
      this._handoffs.set(key, data.from_agent);
    } else if (data.type === "agent" && data.name) {
      span.setAttribute(SemanticConventions.GRAPH_NODE_ID, data.name);
      const key = `${data.name}:${agentSpan.traceId}`;
      const parentNode = this._handoffs.get(key);
      if (parentNode) {
        span.setAttribute(SemanticConventions.GRAPH_NODE_PARENT_ID, parentNode);
        this._handoffs.delete(key);
      }
    }

    // Set status based on error
    if (agentSpan.error) {
      span.setStatus({
        code: SpanStatusCode.ERROR,
        message: `${agentSpan.error.message}: ${agentSpan.error.data || ""}`,
      });
    } else {
      span.setStatus({ code: SpanStatusCode.OK });
    }

    span.end();
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

  forceFlush(): void {
    // No-op for now
  }

  shutdown(): void {
    // Clean up maps
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

  constructor(config: OpenAIAgentsInstrumentationConfig = {}) {
    super(
      "@traceai/fi-instrumentation-openai-agents",
      VERSION,
      config.instrumentationConfig || {}
    );
    this._traceConfig = config.traceConfig;
    this._exclusiveProcessor = config.exclusiveProcessor ?? true;
    // InstrumentationBase calls enable() from its constructor, before
    // _traceConfig is set. Build the tracer again so the config applies.
    this.fiTracer = this._createFITracer();
  }

  override enable(): void {
    super.enable();
    this.fiTracer = this._createFITracer();
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
    this.fiTracer = this._createFITracer();
    this._processor = new FITracingProcessor(this.fiTracer);

    // Register our processor with the agents SDK. By default it replaces the
    // SDK's processors; with exclusiveProcessor: false it is added alongside
    // the default OpenAI exporter.
    try {
      if (
        this._exclusiveProcessor &&
        typeof agentsModule.setTraceProcessors === "function"
      ) {
        agentsModule.setTraceProcessors([this._processor]);
      } else if (typeof agentsModule.addTraceProcessor === "function") {
        agentsModule.addTraceProcessor(this._processor);
      }
    } catch (error) {
      this._diag.warn("Failed to register trace processor with @openai/agents", error);
    }

    markModulePatched(agentsModule, true);
    _isFIPatched = true;
    return agentsModule;
  }

  private unpatch(
    agentsModule: typeof import("@openai/agents") & { _fiPatched?: boolean }
  ): void {
    if (this._processor) {
      this._processor.shutdown();
      this._processor = undefined;
    }
    if (agentsModule) {
      markModulePatched(agentsModule, false);
    }
    _isFIPatched = false;
  }

  /**
   * Get the tracing processor for manual registration.
   *
   * Use this if you want to register the processor yourself.
   */
  getProcessor(): FITracingProcessor {
    if (!this._processor) {
      this._processor = new FITracingProcessor(this.fiTracer);
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
      return FISpanKind.TOOL;
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

