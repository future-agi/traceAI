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

interface SpanData {
  type: string;
  name?: string;
  input?: unknown;
  output?: unknown;
  response?: unknown;
  model?: string;
  modelConfig?: Record<string, unknown>;
  usage?: { inputTokens?: number; outputTokens?: number };
  toAgent?: string;
  fromAgent?: string;
}

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
      this.handleGenerationSpan(span, data);
    } else if (data.type === "function") {
      this.handleFunctionSpan(span, data);
    } else if (data.type === "handoff" && data.toAgent && data.fromAgent) {
      const key = `${data.toAgent}:${agentSpan.traceId}`;
      this._handoffs.set(key, data.fromAgent);
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

    // Track inputs for root span
    if (!this._traceInputs.has(agentSpan.traceId) && data.input) {
      this._traceInputs.set(agentSpan.traceId, data.input);
    }

    // Track outputs for root span
    if (data.response) {
      this._traceOutputs.set(agentSpan.traceId, data.response);
      span.setAttribute(SemanticConventions.OUTPUT_MIME_TYPE, MimeType.JSON);
      span.setAttribute(
        SemanticConventions.RAW_OUTPUT,
        safeJsonStringify(data.response)
      );
      const textOutput = getTextFromResponse(data.response);
      if (textOutput) {
        span.setAttribute(SemanticConventions.OUTPUT_VALUE, textOutput);
      }
    }

    if (data.input) {
      span.setAttribute(SemanticConventions.RAW_INPUT, safeJsonStringify(data.input));
      if (typeof data.input === "string") {
        span.setAttribute(SemanticConventions.INPUT_VALUE, data.input);
      } else {
        span.setAttribute(SemanticConventions.INPUT_MIME_TYPE, MimeType.JSON);
        span.setAttribute(
          SemanticConventions.INPUT_VALUE,
          safeJsonStringify(data.input)
        );
      }
    }
  }

  private handleGenerationSpan(span: Span, data: SpanData): void {
    if (data.model) {
      span.setAttribute(SemanticConventions.LLM_MODEL_NAME, data.model);
    }
    if (data.modelConfig) {
      span.setAttribute(
        SemanticConventions.LLM_INVOCATION_PARAMETERS,
        safeJsonStringify(data.modelConfig)
      );
    }
    if (data.input) {
      span.setAttribute(SemanticConventions.RAW_INPUT, safeJsonStringify(data.input));
      span.setAttribute(SemanticConventions.INPUT_MIME_TYPE, MimeType.JSON);
    }
    if (data.output) {
      span.setAttribute(SemanticConventions.RAW_OUTPUT, safeJsonStringify(data.output));
      span.setAttribute(SemanticConventions.OUTPUT_MIME_TYPE, MimeType.JSON);
    }
    if (data.usage) {
      if (data.usage.inputTokens !== undefined) {
        span.setAttribute(
          SemanticConventions.LLM_TOKEN_COUNT_PROMPT,
          data.usage.inputTokens
        );
      }
      if (data.usage.outputTokens !== undefined) {
        span.setAttribute(
          SemanticConventions.LLM_TOKEN_COUNT_COMPLETION,
          data.usage.outputTokens
        );
      }
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

  constructor(config: OpenAIAgentsInstrumentationConfig = {}) {
    super(
      "@traceai/fi-instrumentation-openai-agents",
      VERSION,
      config.instrumentationConfig || {}
    );
    this._traceConfig = config.traceConfig;
  }

  override enable(): void {
    super.enable();
    this.fiTracer = new FITracer({
      tracer: this.tracer,
      traceConfig: this._traceConfig,
    });
  }

  protected init() {
    const module = new InstrumentationNodeModuleDefinition(
      "@openai/agents",
      ["^0.4.0"],
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

    // Create the processor
    this._processor = new FITracingProcessor(this.fiTracer);

    // Register our processor with the agents SDK
    try {
      if (typeof agentsModule.setTraceProcessors === "function") {
        agentsModule.setTraceProcessors([this._processor]);
      } else if (typeof agentsModule.addTraceProcessor === "function") {
        agentsModule.addTraceProcessor(this._processor);
      }
    } catch (error) {
      this._diag.warn("Failed to register trace processor with @openai/agents", error);
    }

    agentsModule._fiPatched = true;
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
      agentsModule._fiPatched = false;
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
 * Check if the module has been patched.
 */
export function isPatched(): boolean {
  return _isFIPatched;
}

// Helper functions

function safeJsonStringify(obj: unknown): string {
  try {
    return JSON.stringify(obj);
  } catch {
    return String(obj);
  }
}

function getSpanName(span: AgentSpan): string {
  const data = span.spanData;
  if (data.name && typeof data.name === "string") {
    return data.name;
  }
  if (data.type === "handoff" && data.toAgent) {
    return `handoff to ${data.toAgent}`;
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
  const resp = response as { output?: Array<{ type?: string; content?: Array<{ type?: string; text?: string }> }> };
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

// Type declaration for module augmentation
declare module "@openai/agents" {
  export function setTraceProcessors(processors: TracingProcessor[]): void;
  export function addTraceProcessor(processor: TracingProcessor): void;
}
