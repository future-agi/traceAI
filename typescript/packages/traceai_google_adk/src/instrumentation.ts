/**
 * OpenTelemetry instrumentation for Google AI Development Kit (ADK).
 *
 * Google ADK is a framework for building AI agents with Google's models.
 * This instrumentation captures agent execution, tool calls, and model interactions.
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
} from "@traceai/fi-semantic-conventions";
import { VERSION } from "./version";

export interface GoogleADKInstrumentationConfig {
  instrumentationConfig?: Record<string, unknown>;
  traceConfig?: TraceConfigOptions;
}

/**
 * Google ADK Instrumentation class.
 *
 * Provides automatic instrumentation for the @google/adk SDK.
 */
export class GoogleADKInstrumentation extends InstrumentationBase {
  private fiTracer!: FITracer;
  private _traceConfig?: TraceConfigOptions;
  private agentSpans: Map<string, Span> = new Map();

  constructor(config: GoogleADKInstrumentationConfig = {}) {
    super(
      "@traceai/fi-instrumentation-google-adk",
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
      "@google/adk",
      ["^0.1.0", "^0.2.0", "^1.0.0"],
      this.patch.bind(this),
      this.unpatch.bind(this)
    );
    return module;
  }

  /**
   * Manually instrument the @google/adk module.
   */
  manuallyInstrument(adkModule: any): void {
    this.patch(adkModule);
  }

  private patch(adkModule: any & { _fiPatched?: boolean }): any {
    if (adkModule?._fiPatched || _isFIPatched) {
      return adkModule;
    }

    const instrumentation = this;

    // Wrap Agent.run
    if (adkModule.Agent?.prototype?.run) {
      this._wrap(
        adkModule.Agent.prototype,
        "run",
        (original: Function) => {
          return function patchedRun(this: any, ...args: any[]) {
            return instrumentation.traceAgentRun(original, this, args);
          };
        }
      );
    }

    // Wrap Agent.invoke
    if (adkModule.Agent?.prototype?.invoke) {
      this._wrap(
        adkModule.Agent.prototype,
        "invoke",
        (original: Function) => {
          return function patchedInvoke(this: any, ...args: any[]) {
            return instrumentation.traceAgentInvoke(original, this, args);
          };
        }
      );
    }

    // Wrap Agent.stream
    if (adkModule.Agent?.prototype?.stream) {
      this._wrap(
        adkModule.Agent.prototype,
        "stream",
        (original: Function) => {
          return function patchedStream(this: any, ...args: any[]) {
            return instrumentation.traceAgentStream(original, this, args);
          };
        }
      );
    }

    // Wrap Tool execution if available
    if (adkModule.Tool?.prototype?.execute) {
      this._wrap(
        adkModule.Tool.prototype,
        "execute",
        (original: Function) => {
          return function patchedExecute(this: any, ...args: any[]) {
            return instrumentation.traceToolExecution(original, this, args);
          };
        }
      );
    }

    // Wrap Runner.run if available
    if (adkModule.Runner?.prototype?.run) {
      this._wrap(
        adkModule.Runner.prototype,
        "run",
        (original: Function) => {
          return function patchedRunnerRun(this: any, ...args: any[]) {
            return instrumentation.traceRunnerRun(original, this, args);
          };
        }
      );
    }

    // Wrap Session.send_message if available
    if (adkModule.Session?.prototype?.sendMessage) {
      this._wrap(
        adkModule.Session.prototype,
        "sendMessage",
        (original: Function) => {
          return function patchedSendMessage(this: any, ...args: any[]) {
            return instrumentation.traceSessionMessage(original, this, args);
          };
        }
      );
    }

    adkModule._fiPatched = true;
    _isFIPatched = true;
    return adkModule;
  }

  private unpatch(adkModule: any & { _fiPatched?: boolean }): void {
    if (adkModule?.Agent?.prototype?.run) {
      this._unwrap(adkModule.Agent.prototype, "run");
    }
    if (adkModule?.Agent?.prototype?.invoke) {
      this._unwrap(adkModule.Agent.prototype, "invoke");
    }
    if (adkModule?.Agent?.prototype?.stream) {
      this._unwrap(adkModule.Agent.prototype, "stream");
    }
    if (adkModule?.Tool?.prototype?.execute) {
      this._unwrap(adkModule.Tool.prototype, "execute");
    }
    if (adkModule?.Runner?.prototype?.run) {
      this._unwrap(adkModule.Runner.prototype, "run");
    }
    if (adkModule?.Session?.prototype?.sendMessage) {
      this._unwrap(adkModule.Session.prototype, "sendMessage");
    }
    if (adkModule) {
      adkModule._fiPatched = false;
    }
    _isFIPatched = false;
  }

  private async traceAgentRun(
    original: Function,
    instance: any,
    args: any[]
  ): Promise<any> {
    const input = args[0] || {};
    const agentName = instance.name || instance.constructor?.name || "Agent";
    const model = instance.model || instance._model || "unknown";

    const span = this.fiTracer.startSpan(`Google ADK Agent Run: ${agentName}`, {
      kind: SpanKind.INTERNAL,
      attributes: {
        [SemanticConventions.FI_SPAN_KIND]: FISpanKind.AGENT,
        [SemanticConventions.LLM_SYSTEM]: "google-adk",
        [SemanticConventions.LLM_MODEL_NAME]: model,
        [SemanticConventions.INPUT_VALUE]: safeJsonStringify(input),
        [SemanticConventions.INPUT_MIME_TYPE]: MimeType.JSON,
        [SemanticConventions.RAW_INPUT]: safeJsonStringify(input),
        "google_adk.agent_name": agentName,
        "google_adk.operation": "run",
      },
    });

    // Add input message
    if (typeof input === "string") {
      span.setAttribute(
        `${SemanticConventions.LLM_INPUT_MESSAGES}.0.message.role`,
        "user"
      );
      span.setAttribute(
        `${SemanticConventions.LLM_INPUT_MESSAGES}.0.message.content`,
        input
      );
    } else if (input.message || input.content) {
      span.setAttribute(
        `${SemanticConventions.LLM_INPUT_MESSAGES}.0.message.role`,
        "user"
      );
      span.setAttribute(
        `${SemanticConventions.LLM_INPUT_MESSAGES}.0.message.content`,
        input.message || input.content
      );
    }

    const agentId = generateId();
    this.agentSpans.set(agentId, span);
    const execContext = trace.setSpan(context.active(), span);

    try {
      const result = await context.with(execContext, () => {
        return original.apply(instance, args);
      });

      this.setAgentResponseAttributes(span, result);
      span.setStatus({ code: SpanStatusCode.OK });
      span.end();
      this.agentSpans.delete(agentId);

      return result;
    } catch (error) {
      span.recordException(error as Error);
      span.setStatus({
        code: SpanStatusCode.ERROR,
        message: (error as Error).message,
      });
      span.end();
      this.agentSpans.delete(agentId);
      throw error;
    }
  }

  private async traceAgentInvoke(
    original: Function,
    instance: any,
    args: any[]
  ): Promise<any> {
    const input = args[0] || {};
    const agentName = instance.name || instance.constructor?.name || "Agent";

    const span = this.fiTracer.startSpan(`Google ADK Agent Invoke: ${agentName}`, {
      kind: SpanKind.INTERNAL,
      attributes: {
        [SemanticConventions.FI_SPAN_KIND]: FISpanKind.AGENT,
        [SemanticConventions.LLM_SYSTEM]: "google-adk",
        [SemanticConventions.INPUT_VALUE]: safeJsonStringify(input),
        [SemanticConventions.INPUT_MIME_TYPE]: MimeType.JSON,
        "google_adk.agent_name": agentName,
        "google_adk.operation": "invoke",
      },
    });

    const execContext = trace.setSpan(context.active(), span);

    try {
      const result = await context.with(execContext, () => {
        return original.apply(instance, args);
      });

      this.setAgentResponseAttributes(span, result);
      span.setStatus({ code: SpanStatusCode.OK });
      span.end();

      return result;
    } catch (error) {
      span.recordException(error as Error);
      span.setStatus({
        code: SpanStatusCode.ERROR,
        message: (error as Error).message,
      });
      span.end();
      throw error;
    }
  }

  private async traceAgentStream(
    original: Function,
    instance: any,
    args: any[]
  ): Promise<any> {
    const input = args[0] || {};
    const agentName = instance.name || instance.constructor?.name || "Agent";

    const span = this.fiTracer.startSpan(`Google ADK Agent Stream: ${agentName}`, {
      kind: SpanKind.INTERNAL,
      attributes: {
        [SemanticConventions.FI_SPAN_KIND]: FISpanKind.AGENT,
        [SemanticConventions.LLM_SYSTEM]: "google-adk",
        [SemanticConventions.INPUT_VALUE]: safeJsonStringify(input),
        [SemanticConventions.INPUT_MIME_TYPE]: MimeType.JSON,
        "google_adk.agent_name": agentName,
        "google_adk.operation": "stream",
        "google_adk.streaming": true,
      },
    });

    const execContext = trace.setSpan(context.active(), span);

    try {
      const result = await context.with(execContext, () => {
        return original.apply(instance, args);
      });

      // Wrap the stream if it's async iterable
      if (result && typeof result[Symbol.asyncIterator] === "function") {
        return this.wrapAgentStream(result, span);
      }

      span.setStatus({ code: SpanStatusCode.OK });
      span.end();
      return result;
    } catch (error) {
      span.recordException(error as Error);
      span.setStatus({
        code: SpanStatusCode.ERROR,
        message: (error as Error).message,
      });
      span.end();
      throw error;
    }
  }

  private async traceToolExecution(
    original: Function,
    instance: any,
    args: any[]
  ): Promise<any> {
    const input = args[0] || {};
    const toolName = instance.name || instance.constructor?.name || "Tool";

    const span = this.fiTracer.startSpan(`Google ADK Tool: ${toolName}`, {
      kind: SpanKind.INTERNAL,
      attributes: {
        [SemanticConventions.FI_SPAN_KIND]: FISpanKind.TOOL,
        "google_adk.tool_name": toolName,
        [SemanticConventions.INPUT_VALUE]: safeJsonStringify(input),
        [SemanticConventions.INPUT_MIME_TYPE]: MimeType.JSON,
      },
    });

    const execContext = trace.setSpan(context.active(), span);

    try {
      const result = await context.with(execContext, () => {
        return original.apply(instance, args);
      });

      span.setAttributes({
        [SemanticConventions.OUTPUT_VALUE]: safeJsonStringify(result),
        [SemanticConventions.OUTPUT_MIME_TYPE]: MimeType.JSON,
      });

      span.setStatus({ code: SpanStatusCode.OK });
      span.end();

      return result;
    } catch (error) {
      span.recordException(error as Error);
      span.setStatus({
        code: SpanStatusCode.ERROR,
        message: (error as Error).message,
      });
      span.end();
      throw error;
    }
  }

  private async traceRunnerRun(
    original: Function,
    instance: any,
    args: any[]
  ): Promise<any> {
    const input = args[0] || {};
    const runnerName = instance.name || instance.constructor?.name || "Runner";

    const span = this.fiTracer.startSpan(`Google ADK Runner: ${runnerName}`, {
      kind: SpanKind.INTERNAL,
      attributes: {
        [SemanticConventions.FI_SPAN_KIND]: FISpanKind.CHAIN,
        "google_adk.runner_name": runnerName,
        [SemanticConventions.INPUT_VALUE]: safeJsonStringify(input),
        [SemanticConventions.INPUT_MIME_TYPE]: MimeType.JSON,
      },
    });

    const execContext = trace.setSpan(context.active(), span);

    try {
      const result = await context.with(execContext, () => {
        return original.apply(instance, args);
      });

      span.setAttributes({
        [SemanticConventions.OUTPUT_VALUE]: safeJsonStringify(result),
        [SemanticConventions.OUTPUT_MIME_TYPE]: MimeType.JSON,
      });

      span.setStatus({ code: SpanStatusCode.OK });
      span.end();

      return result;
    } catch (error) {
      span.recordException(error as Error);
      span.setStatus({
        code: SpanStatusCode.ERROR,
        message: (error as Error).message,
      });
      span.end();
      throw error;
    }
  }

  private async traceSessionMessage(
    original: Function,
    instance: any,
    args: any[]
  ): Promise<any> {
    const message = args[0];
    const sessionId = instance.id || instance._id || "unknown";

    const span = this.fiTracer.startSpan(`Google ADK Session Message`, {
      kind: SpanKind.CLIENT,
      attributes: {
        [SemanticConventions.FI_SPAN_KIND]: FISpanKind.LLM,
        [SemanticConventions.LLM_SYSTEM]: "google-adk",
        "google_adk.session_id": sessionId,
        [SemanticConventions.INPUT_VALUE]: safeJsonStringify(message),
        [`${SemanticConventions.LLM_INPUT_MESSAGES}.0.message.role`]: "user",
        [`${SemanticConventions.LLM_INPUT_MESSAGES}.0.message.content`]:
          typeof message === "string" ? message : safeJsonStringify(message),
      },
    });

    const execContext = trace.setSpan(context.active(), span);

    try {
      const result = await context.with(execContext, () => {
        return original.apply(instance, args);
      });

      this.setAgentResponseAttributes(span, result);
      span.setStatus({ code: SpanStatusCode.OK });
      span.end();

      return result;
    } catch (error) {
      span.recordException(error as Error);
      span.setStatus({
        code: SpanStatusCode.ERROR,
        message: (error as Error).message,
      });
      span.end();
      throw error;
    }
  }

  private setAgentResponseAttributes(span: Span, result: any): void {
    if (!result) return;

    // Handle different response formats
    const output = result.output || result.response || result.content || result;

    if (typeof output === "string") {
      span.setAttribute(SemanticConventions.OUTPUT_VALUE, output);
      span.setAttribute(
        `${SemanticConventions.LLM_OUTPUT_MESSAGES}.0.message.role`,
        "assistant"
      );
      span.setAttribute(
        `${SemanticConventions.LLM_OUTPUT_MESSAGES}.0.message.content`,
        output
      );
    } else {
      span.setAttribute(SemanticConventions.OUTPUT_VALUE, safeJsonStringify(output));
    }

    // Capture usage if available
    if (result.usage || result.usageMetadata) {
      const usage = result.usage || result.usageMetadata;
      if (usage.promptTokens || usage.promptTokenCount) {
        span.setAttribute(
          SemanticConventions.LLM_TOKEN_COUNT_PROMPT,
          usage.promptTokens || usage.promptTokenCount
        );
      }
      if (usage.completionTokens || usage.candidatesTokenCount) {
        span.setAttribute(
          SemanticConventions.LLM_TOKEN_COUNT_COMPLETION,
          usage.completionTokens || usage.candidatesTokenCount
        );
      }
      if (usage.totalTokens || usage.totalTokenCount) {
        span.setAttribute(
          SemanticConventions.LLM_TOKEN_COUNT_TOTAL,
          usage.totalTokens || usage.totalTokenCount
        );
      }
    }

    // Capture tool calls if present
    if (result.toolCalls || result.functionCalls) {
      const toolCalls = result.toolCalls || result.functionCalls;
      span.setAttribute("google_adk.tool_call_count", toolCalls.length);
      toolCalls.forEach((call: any, idx: number) => {
        span.setAttribute(`google_adk.tool_calls.${idx}.name`, call.name || call.functionName);
      });
    }

    span.setAttribute(SemanticConventions.RAW_OUTPUT, safeJsonStringify(result));
  }

  private async *wrapAgentStream(stream: AsyncIterable<any>, span: Span): AsyncIterable<any> {
    const chunks: any[] = [];
    let fullContent = "";

    try {
      for await (const chunk of stream) {
        chunks.push(chunk);

        // Extract content from chunk
        const content = chunk.content || chunk.text || chunk.output;
        if (typeof content === "string") {
          fullContent += content;
        }

        yield chunk;
      }

      span.setAttributes({
        [SemanticConventions.OUTPUT_VALUE]: fullContent || safeJsonStringify(chunks),
        [`${SemanticConventions.LLM_OUTPUT_MESSAGES}.0.message.role`]: "assistant",
        [`${SemanticConventions.LLM_OUTPUT_MESSAGES}.0.message.content`]: fullContent,
        [SemanticConventions.RAW_OUTPUT]: safeJsonStringify(chunks),
      });

      span.setStatus({ code: SpanStatusCode.OK });
    } catch (error) {
      span.recordException(error as Error);
      span.setStatus({
        code: SpanStatusCode.ERROR,
        message: (error as Error).message,
      });
      throw error;
    } finally {
      span.end();
    }
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

function generateId(): string {
  return Math.random().toString(36).substring(2, 15);
}
