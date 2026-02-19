/**
 * OpenTelemetry instrumentation for Portkey AI Gateway.
 *
 * Portkey is an AI gateway that provides unified access to multiple LLM providers.
 * This instrumentation captures gateway requests and responses.
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
import { FITracer, TraceConfigOptions, safelyJSONStringify } from "@traceai/fi-core";
import {
  SemanticConventions,
  FISpanKind,
  MimeType,
} from "@traceai/fi-semantic-conventions";
import { VERSION } from "./version";

export interface PortkeyInstrumentationConfig {
  instrumentationConfig?: Record<string, unknown>;
  traceConfig?: TraceConfigOptions;
}

/**
 * Portkey Instrumentation class.
 *
 * Provides automatic instrumentation for the portkey-ai SDK.
 */
export class PortkeyInstrumentation extends InstrumentationBase {
  private fiTracer!: FITracer;
  private _traceConfig?: TraceConfigOptions;

  constructor(config: PortkeyInstrumentationConfig = {}) {
    super(
      "@traceai/fi-instrumentation-portkey",
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
      "portkey-ai",
      ["^1.0.0"],
      this.patch.bind(this),
      this.unpatch.bind(this)
    );
    return module;
  }

  /**
   * Manually instrument the portkey-ai module.
   */
  manuallyInstrument(portkeyModule: any): void {
    this.patch(portkeyModule);
  }

  private patch(portkeyModule: any & { _fiPatched?: boolean }): any {
    if (portkeyModule?._fiPatched || _isFIPatched) {
      return portkeyModule;
    }

    const instrumentation = this;

    // Wrap Portkey chat completions
    if (portkeyModule.Portkey?.prototype?.chat?.completions?.create) {
      this._wrap(
        portkeyModule.Portkey.prototype.chat.completions,
        "create",
        (original: Function) => {
          return function patchedCreate(this: any, ...args: any[]) {
            return instrumentation.traceChatCompletion(original, this, args);
          };
        }
      );
    }

    // Wrap Portkey completions
    if (portkeyModule.Portkey?.prototype?.completions?.create) {
      this._wrap(
        portkeyModule.Portkey.prototype.completions,
        "create",
        (original: Function) => {
          return function patchedCreate(this: any, ...args: any[]) {
            return instrumentation.traceCompletion(original, this, args);
          };
        }
      );
    }

    // Wrap Portkey embeddings
    if (portkeyModule.Portkey?.prototype?.embeddings?.create) {
      this._wrap(
        portkeyModule.Portkey.prototype.embeddings,
        "create",
        (original: Function) => {
          return function patchedCreate(this: any, ...args: any[]) {
            return instrumentation.traceEmbedding(original, this, args);
          };
        }
      );
    }

    portkeyModule._fiPatched = true;
    _isFIPatched = true;
    return portkeyModule;
  }

  private unpatch(portkeyModule: any & { _fiPatched?: boolean }): void {
    if (portkeyModule?.Portkey?.prototype?.chat?.completions?.create) {
      this._unwrap(portkeyModule.Portkey.prototype.chat.completions, "create");
    }
    if (portkeyModule?.Portkey?.prototype?.completions?.create) {
      this._unwrap(portkeyModule.Portkey.prototype.completions, "create");
    }
    if (portkeyModule?.Portkey?.prototype?.embeddings?.create) {
      this._unwrap(portkeyModule.Portkey.prototype.embeddings, "create");
    }
    if (portkeyModule) {
      portkeyModule._fiPatched = false;
    }
    _isFIPatched = false;
  }

  private async traceChatCompletion(
    original: Function,
    instance: any,
    args: any[]
  ): Promise<any> {
    const params = args[0] || {};
    const model = params.model || "unknown";
    const messages = params.messages || [];

    // Build input messages as JSON blob
    const serializedMessages = messages.map((msg: any) => {
      const obj: Record<string, unknown> = {};
      if (msg.role) obj.role = msg.role;
      if (msg.content) obj.content = typeof msg.content === "string" ? msg.content : safeJsonStringify(msg.content);
      return obj;
    });

    const span = this.fiTracer.startSpan("Portkey Chat Completions", {
      kind: SpanKind.INTERNAL,
      attributes: {
        [SemanticConventions.FI_SPAN_KIND]: FISpanKind.LLM,
        [SemanticConventions.LLM_PROVIDER]: "portkey",
        [SemanticConventions.LLM_MODEL_NAME]: model,
        [SemanticConventions.GEN_AI_OPERATION_NAME]: "chat",
        [SemanticConventions.INPUT_VALUE]: safeJsonStringify(params),
        [SemanticConventions.INPUT_MIME_TYPE]: MimeType.JSON,
        [SemanticConventions.LLM_INPUT_MESSAGES]: safelyJSONStringify(serializedMessages) ?? "[]",
        [SemanticConventions.RAW_INPUT]: safeJsonStringify(params),
      },
    });

    const execContext = trace.setSpan(context.active(), span);

    try {
      const result = await context.with(execContext, () => {
        return original.apply(instance, args);
      });

      // Handle streaming
      if (result && typeof result[Symbol.asyncIterator] === "function") {
        return this.wrapStream(result, span);
      }

      // Non-streaming response
      this.setResponseAttributes(span, result);
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

  private async traceCompletion(
    original: Function,
    instance: any,
    args: any[]
  ): Promise<any> {
    const params = args[0] || {};
    const model = params.model || "unknown";

    const span = this.fiTracer.startSpan("Portkey Completions", {
      kind: SpanKind.INTERNAL,
      attributes: {
        [SemanticConventions.FI_SPAN_KIND]: FISpanKind.LLM,
        [SemanticConventions.LLM_PROVIDER]: "portkey",
        [SemanticConventions.LLM_MODEL_NAME]: model,
        [SemanticConventions.GEN_AI_OPERATION_NAME]: "text_completion",
        [SemanticConventions.INPUT_VALUE]: params.prompt || safeJsonStringify(params),
        [SemanticConventions.RAW_INPUT]: safeJsonStringify(params),
      },
    });

    const execContext = trace.setSpan(context.active(), span);

    try {
      const result = await context.with(execContext, () => {
        return original.apply(instance, args);
      });

      span.setAttributes({
        [SemanticConventions.OUTPUT_VALUE]: safeJsonStringify(result),
        [SemanticConventions.RAW_OUTPUT]: safeJsonStringify(result),
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

  private async traceEmbedding(
    original: Function,
    instance: any,
    args: any[]
  ): Promise<any> {
    const params = args[0] || {};
    const model = params.model || "unknown";
    const input = params.input;

    const span = this.fiTracer.startSpan("Portkey Embeddings", {
      kind: SpanKind.INTERNAL,
      attributes: {
        [SemanticConventions.FI_SPAN_KIND]: FISpanKind.EMBEDDING,
        [SemanticConventions.LLM_PROVIDER]: "portkey",
        [SemanticConventions.LLM_MODEL_NAME]: model,
        [SemanticConventions.GEN_AI_OPERATION_NAME]: "embeddings",
        [SemanticConventions.EMBEDDING_MODEL_NAME]: model,
        [SemanticConventions.INPUT_VALUE]: Array.isArray(input)
          ? safeJsonStringify(input)
          : input,
        [SemanticConventions.RAW_INPUT]: safeJsonStringify(params),
      },
    });

    const execContext = trace.setSpan(context.active(), span);

    try {
      const result = await context.with(execContext, () => {
        return original.apply(instance, args);
      });

      if (result?.data?.length) {
        span.setAttribute("embedding.vector_count", result.data.length);
      }
      if (result?.usage) {
        span.setAttribute(SemanticConventions.LLM_TOKEN_COUNT_PROMPT, result.usage.prompt_tokens);
        span.setAttribute(SemanticConventions.LLM_TOKEN_COUNT_TOTAL, result.usage.total_tokens);
      }

      span.setAttributes({
        [SemanticConventions.RAW_OUTPUT]: safeJsonStringify(result),
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

  private setResponseAttributes(span: Span, result: any): void {
    if (!result) return;

    if (result.model) {
      span.setAttribute(SemanticConventions.LLM_MODEL_NAME, result.model);
      span.setAttribute(SemanticConventions.GEN_AI_RESPONSE_MODEL, result.model);
    }
    if (result.id) {
      span.setAttribute(SemanticConventions.GEN_AI_RESPONSE_ID, result.id);
    }

    if (result.choices?.[0]?.message) {
      const message = result.choices[0].message;
      const msg: Record<string, unknown> = { role: message.role || "assistant" };
      if (message.content) {
        msg.content = message.content;
        span.setAttribute(SemanticConventions.OUTPUT_VALUE, message.content);
      }
      span.setAttribute(
        SemanticConventions.LLM_OUTPUT_MESSAGES,
        safelyJSONStringify([msg]) ?? "[]"
      );
      span.setAttribute(
        SemanticConventions.GEN_AI_RESPONSE_FINISH_REASONS,
        safelyJSONStringify(result.choices.map((c: any) => c.finish_reason)) ?? "[]"
      );
    }

    if (result.usage) {
      span.setAttribute(SemanticConventions.LLM_TOKEN_COUNT_PROMPT, result.usage.prompt_tokens);
      span.setAttribute(SemanticConventions.LLM_TOKEN_COUNT_COMPLETION, result.usage.completion_tokens);
      span.setAttribute(SemanticConventions.LLM_TOKEN_COUNT_TOTAL, result.usage.total_tokens);
    }

    span.setAttribute(SemanticConventions.RAW_OUTPUT, safeJsonStringify(result));
  }

  private async *wrapStream(stream: AsyncIterable<any>, span: Span): AsyncIterable<any> {
    const chunks: any[] = [];
    let fullContent = "";

    try {
      for await (const chunk of stream) {
        chunks.push(chunk);
        if (chunk.choices?.[0]?.delta?.content) {
          fullContent += chunk.choices[0].delta.content;
        }
        yield chunk;
      }

      span.setAttributes({
        [SemanticConventions.OUTPUT_VALUE]: fullContent,
        [SemanticConventions.LLM_OUTPUT_MESSAGES]: safelyJSONStringify([{ role: "assistant", content: fullContent }]) ?? "[]",
        [SemanticConventions.RAW_OUTPUT]: safeJsonStringify(chunks),
      });

      // Get usage from last chunk if available
      const lastChunk = chunks[chunks.length - 1];
      if (lastChunk?.usage) {
        span.setAttribute(SemanticConventions.LLM_TOKEN_COUNT_PROMPT, lastChunk.usage.prompt_tokens);
        span.setAttribute(SemanticConventions.LLM_TOKEN_COUNT_COMPLETION, lastChunk.usage.completion_tokens);
        span.setAttribute(SemanticConventions.LLM_TOKEN_COUNT_TOTAL, lastChunk.usage.total_tokens);
      }

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
