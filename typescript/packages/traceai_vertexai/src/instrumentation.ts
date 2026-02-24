/**
 * OpenTelemetry instrumentation for Google Cloud Vertex AI.
 *
 * Vertex AI provides access to Google's generative AI models including Gemini.
 * This instrumentation captures model generation, chat sessions, embeddings,
 * and streaming responses.
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

export interface VertexAIInstrumentationConfig {
  instrumentationConfig?: Record<string, unknown>;
  traceConfig?: TraceConfigOptions;
}

/**
 * Vertex AI Instrumentation class.
 *
 * Provides automatic instrumentation for the @google-cloud/vertexai SDK.
 */
export class VertexAIInstrumentation extends InstrumentationBase {
  private fiTracer!: FITracer;
  private _traceConfig?: TraceConfigOptions;

  constructor(config: VertexAIInstrumentationConfig = {}) {
    super(
      "@traceai/fi-instrumentation-vertexai",
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
      "@google-cloud/vertexai",
      ["^1.0.0"],
      this.patch.bind(this),
      this.unpatch.bind(this)
    );
    return module;
  }

  /**
   * Manually instrument the @google-cloud/vertexai module.
   */
  manuallyInstrument(vertexModule: any): void {
    if (vertexModule == null) return;
    this.patch(vertexModule);
  }

  private patch(vertexModule: any & { _fiPatched?: boolean }): any {
    if (vertexModule?._fiPatched || _isFIPatched) {
      return vertexModule;
    }

    const instrumentation = this;

    // Wrap GenerativeModel.generateContent
    if (vertexModule.GenerativeModel?.prototype?.generateContent) {
      this._wrap(
        vertexModule.GenerativeModel.prototype,
        "generateContent",
        (original: Function) => {
          return function patchedGenerateContent(this: any, ...args: any[]) {
            return instrumentation.traceGenerateContent(original, this, args);
          };
        }
      );
    }

    // Wrap GenerativeModel.generateContentStream
    if (vertexModule.GenerativeModel?.prototype?.generateContentStream) {
      this._wrap(
        vertexModule.GenerativeModel.prototype,
        "generateContentStream",
        (original: Function) => {
          return function patchedGenerateContentStream(this: any, ...args: any[]) {
            return instrumentation.traceGenerateContentStream(original, this, args);
          };
        }
      );
    }

    // Wrap ChatSession.sendMessage
    if (vertexModule.ChatSession?.prototype?.sendMessage) {
      this._wrap(
        vertexModule.ChatSession.prototype,
        "sendMessage",
        (original: Function) => {
          return function patchedSendMessage(this: any, ...args: any[]) {
            return instrumentation.traceSendMessage(original, this, args);
          };
        }
      );
    }

    // Wrap ChatSession.sendMessageStream
    if (vertexModule.ChatSession?.prototype?.sendMessageStream) {
      this._wrap(
        vertexModule.ChatSession.prototype,
        "sendMessageStream",
        (original: Function) => {
          return function patchedSendMessageStream(this: any, ...args: any[]) {
            return instrumentation.traceSendMessageStream(original, this, args);
          };
        }
      );
    }

    // Wrap GenerativeModel.countTokens
    if (vertexModule.GenerativeModel?.prototype?.countTokens) {
      this._wrap(
        vertexModule.GenerativeModel.prototype,
        "countTokens",
        (original: Function) => {
          return function patchedCountTokens(this: any, ...args: any[]) {
            return instrumentation.traceCountTokens(original, this, args);
          };
        }
      );
    }

    // Wrap TextEmbeddingModel.embed (if available)
    if (vertexModule.TextEmbeddingModel?.prototype?.embed) {
      this._wrap(
        vertexModule.TextEmbeddingModel.prototype,
        "embed",
        (original: Function) => {
          return function patchedEmbed(this: any, ...args: any[]) {
            return instrumentation.traceEmbed(original, this, args);
          };
        }
      );
    }

    vertexModule._fiPatched = true;
    _isFIPatched = true;
    return vertexModule;
  }

  private unpatch(vertexModule: any & { _fiPatched?: boolean }): void {
    if (vertexModule?.GenerativeModel?.prototype?.generateContent) {
      this._unwrap(vertexModule.GenerativeModel.prototype, "generateContent");
    }
    if (vertexModule?.GenerativeModel?.prototype?.generateContentStream) {
      this._unwrap(vertexModule.GenerativeModel.prototype, "generateContentStream");
    }
    if (vertexModule?.ChatSession?.prototype?.sendMessage) {
      this._unwrap(vertexModule.ChatSession.prototype, "sendMessage");
    }
    if (vertexModule?.ChatSession?.prototype?.sendMessageStream) {
      this._unwrap(vertexModule.ChatSession.prototype, "sendMessageStream");
    }
    if (vertexModule?.GenerativeModel?.prototype?.countTokens) {
      this._unwrap(vertexModule.GenerativeModel.prototype, "countTokens");
    }
    if (vertexModule?.TextEmbeddingModel?.prototype?.embed) {
      this._unwrap(vertexModule.TextEmbeddingModel.prototype, "embed");
    }
    if (vertexModule) {
      vertexModule._fiPatched = false;
    }
    _isFIPatched = false;
  }

  private async traceGenerateContent(
    original: Function,
    instance: any,
    args: any[]
  ): Promise<any> {
    const request = args[0] || {};
    const modelName = instance.model || instance._model || "unknown";

    const span = this.fiTracer.startSpan(`Vertex AI Generate Content`, {
      kind: SpanKind.CLIENT,
      attributes: {
        [SemanticConventions.FI_SPAN_KIND]: FISpanKind.LLM,
        [SemanticConventions.LLM_PROVIDER]: "google",
        [SemanticConventions.LLM_MODEL_NAME]: modelName,
        [SemanticConventions.GEN_AI_OPERATION_NAME]: "chat",
        [SemanticConventions.INPUT_VALUE]: safeJsonStringify(request),
        [SemanticConventions.INPUT_MIME_TYPE]: MimeType.JSON,
        [SemanticConventions.RAW_INPUT]: safeJsonStringify(request),
        ...this.getInputMessagesAttributes(request),
      },
    });

    const execContext = trace.setSpan(context.active(), span);

    try {
      const result = await context.with(execContext, () => {
        return original.apply(instance, args);
      });

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

  private async traceGenerateContentStream(
    original: Function,
    instance: any,
    args: any[]
  ): Promise<any> {
    const request = args[0] || {};
    const modelName = instance.model || instance._model || "unknown";

    const span = this.fiTracer.startSpan(`Vertex AI Generate Content Stream`, {
      kind: SpanKind.CLIENT,
      attributes: {
        [SemanticConventions.FI_SPAN_KIND]: FISpanKind.LLM,
        [SemanticConventions.LLM_PROVIDER]: "google",
        [SemanticConventions.LLM_MODEL_NAME]: modelName,
        [SemanticConventions.GEN_AI_OPERATION_NAME]: "chat",
        [SemanticConventions.INPUT_VALUE]: safeJsonStringify(request),
        [SemanticConventions.INPUT_MIME_TYPE]: MimeType.JSON,
        [SemanticConventions.RAW_INPUT]: safeJsonStringify(request),
        "vertexai.streaming": true,
        ...this.getInputMessagesAttributes(request),
      },
    });

    const execContext = trace.setSpan(context.active(), span);

    try {
      const result = await context.with(execContext, () => {
        return original.apply(instance, args);
      });

      // Wrap the stream to capture output
      return this.wrapStream(result, span);
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

  private async traceSendMessage(
    original: Function,
    instance: any,
    args: any[]
  ): Promise<any> {
    const message = args[0];
    const modelName = instance._model?.model || instance._model?._model || "unknown";

    const inputContent = typeof message === "string" ? message : safeJsonStringify(message);
    const span = this.fiTracer.startSpan(`Vertex AI Chat Send Message`, {
      kind: SpanKind.CLIENT,
      attributes: {
        [SemanticConventions.FI_SPAN_KIND]: FISpanKind.LLM,
        [SemanticConventions.LLM_PROVIDER]: "google",
        [SemanticConventions.LLM_MODEL_NAME]: modelName,
        [SemanticConventions.GEN_AI_OPERATION_NAME]: "chat",
        [SemanticConventions.INPUT_VALUE]: safeJsonStringify(message),
        [SemanticConventions.INPUT_MIME_TYPE]: MimeType.JSON,
        [SemanticConventions.LLM_INPUT_MESSAGES]: safelyJSONStringify([{ role: "user", content: inputContent }]) ?? "[]",
      },
    });

    const execContext = trace.setSpan(context.active(), span);

    try {
      const result = await context.with(execContext, () => {
        return original.apply(instance, args);
      });

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

  private async traceSendMessageStream(
    original: Function,
    instance: any,
    args: any[]
  ): Promise<any> {
    const message = args[0];
    const modelName = instance._model?.model || instance._model?._model || "unknown";

    const streamInputContent = typeof message === "string" ? message : safeJsonStringify(message);
    const span = this.fiTracer.startSpan(`Vertex AI Chat Send Message Stream`, {
      kind: SpanKind.CLIENT,
      attributes: {
        [SemanticConventions.FI_SPAN_KIND]: FISpanKind.LLM,
        [SemanticConventions.LLM_PROVIDER]: "google",
        [SemanticConventions.LLM_MODEL_NAME]: modelName,
        [SemanticConventions.GEN_AI_OPERATION_NAME]: "chat",
        [SemanticConventions.INPUT_VALUE]: safeJsonStringify(message),
        [SemanticConventions.INPUT_MIME_TYPE]: MimeType.JSON,
        "vertexai.streaming": true,
        [SemanticConventions.LLM_INPUT_MESSAGES]: safelyJSONStringify([{ role: "user", content: streamInputContent }]) ?? "[]",
      },
    });

    const execContext = trace.setSpan(context.active(), span);

    try {
      const result = await context.with(execContext, () => {
        return original.apply(instance, args);
      });

      return this.wrapStream(result, span);
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

  private async traceCountTokens(
    original: Function,
    instance: any,
    args: any[]
  ): Promise<any> {
    const request = args[0] || {};
    const modelName = instance.model || instance._model || "unknown";

    const span = this.fiTracer.startSpan(`Vertex AI Count Tokens`, {
      kind: SpanKind.CLIENT,
      attributes: {
        [SemanticConventions.FI_SPAN_KIND]: FISpanKind.LLM,
        [SemanticConventions.LLM_PROVIDER]: "google",
        [SemanticConventions.LLM_MODEL_NAME]: modelName,
        "vertexai.operation": "count_tokens",
        [SemanticConventions.INPUT_VALUE]: safeJsonStringify(request),
      },
    });

    const execContext = trace.setSpan(context.active(), span);

    try {
      const result = await context.with(execContext, () => {
        return original.apply(instance, args);
      });

      if (result?.totalTokens) {
        span.setAttribute("vertexai.total_tokens", result.totalTokens);
      }

      span.setAttributes({
        [SemanticConventions.OUTPUT_VALUE]: safeJsonStringify(result),
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

  private async traceEmbed(
    original: Function,
    instance: any,
    args: any[]
  ): Promise<any> {
    const request = args[0] || {};
    const modelName = instance.model || instance._model || "unknown";
    const texts = request.texts || request.instances || [];

    const span = this.fiTracer.startSpan(`Vertex AI Embed`, {
      kind: SpanKind.CLIENT,
      attributes: {
        [SemanticConventions.FI_SPAN_KIND]: FISpanKind.EMBEDDING,
        [SemanticConventions.LLM_PROVIDER]: "google",
        [SemanticConventions.EMBEDDING_MODEL_NAME]: modelName,
        [SemanticConventions.GEN_AI_OPERATION_NAME]: "embeddings",
        [SemanticConventions.INPUT_VALUE]: safeJsonStringify(request),
        "vertexai.input_count": Array.isArray(texts) ? texts.length : 1,
      },
    });

    const execContext = trace.setSpan(context.active(), span);

    try {
      const result = await context.with(execContext, () => {
        return original.apply(instance, args);
      });

      if (result?.embeddings) {
        span.setAttribute("vertexai.embedding_count", result.embeddings.length);
        if (result.embeddings[0]?.values) {
          span.setAttribute("vertexai.embedding_dimensions", result.embeddings[0].values.length);
        }
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

  private getInputMessagesAttributes(request: any): Record<string, string> {
    const contents = request.contents || [];
    if (!Array.isArray(contents) || contents.length === 0) {
      return {};
    }
    const messages = contents.map((content: any) => {
      const msg: Record<string, unknown> = { role: content.role || "user" };
      if (content.parts) {
        const textParts = content.parts
          .filter((p: any) => p.text)
          .map((p: any) => p.text)
          .join("");
        if (textParts) {
          msg.content = textParts;
        }
      }
      return msg;
    });
    return {
      [SemanticConventions.LLM_INPUT_MESSAGES]: safelyJSONStringify(messages) ?? "[]",
    };
  }

  private setResponseAttributes(span: Span, result: any): void {
    if (!result) return;

    const response = result.response || result;

    if (response.candidates?.[0]?.content) {
      const content = response.candidates[0].content;
      const msg: Record<string, unknown> = { role: content.role || "model" };

      if (content.parts) {
        const textParts = content.parts
          .filter((p: any) => p.text)
          .map((p: any) => p.text)
          .join("");
        if (textParts) {
          msg.content = textParts;
          span.setAttribute(SemanticConventions.OUTPUT_VALUE, textParts);
        }
      }

      span.setAttribute(
        SemanticConventions.LLM_OUTPUT_MESSAGES,
        safelyJSONStringify([msg]) ?? "[]"
      );

      // Add finish reasons
      const finishReasons = response.candidates
        ?.map((c: any) => c.finishReason)
        .filter(Boolean);
      if (finishReasons?.length) {
        span.setAttribute(
          SemanticConventions.GEN_AI_RESPONSE_FINISH_REASONS,
          safelyJSONStringify(finishReasons) ?? "[]"
        );
      }
    }

    if (response.usageMetadata) {
      const usage = response.usageMetadata;
      if (usage.promptTokenCount) {
        span.setAttribute(SemanticConventions.LLM_TOKEN_COUNT_PROMPT, usage.promptTokenCount);
      }
      if (usage.candidatesTokenCount) {
        span.setAttribute(SemanticConventions.LLM_TOKEN_COUNT_COMPLETION, usage.candidatesTokenCount);
      }
      if (usage.totalTokenCount) {
        span.setAttribute(SemanticConventions.LLM_TOKEN_COUNT_TOTAL, usage.totalTokenCount);
      }
    }

    span.setAttribute(SemanticConventions.RAW_OUTPUT, safeJsonStringify(response));
  }

  private async wrapStream(streamResult: any, span: Span): Promise<any> {
    const chunks: any[] = [];
    let fullContent = "";

    // Vertex AI streams return a response object with a stream property
    const originalStream = streamResult.stream;

    if (!originalStream || typeof originalStream[Symbol.asyncIterator] !== "function") {
      // If not a proper stream, just return as-is
      span.setStatus({ code: SpanStatusCode.OK });
      span.end();
      return streamResult;
    }

    const wrappedStream = async function* () {
      try {
        for await (const chunk of originalStream) {
          chunks.push(chunk);
          if (chunk.candidates?.[0]?.content?.parts) {
            const textParts = chunk.candidates[0].content.parts
              .filter((p: any) => p.text)
              .map((p: any) => p.text)
              .join("");
            fullContent += textParts;
          }
          yield chunk;
        }

        span.setAttributes({
          [SemanticConventions.OUTPUT_VALUE]: fullContent,
          [SemanticConventions.LLM_OUTPUT_MESSAGES]: safelyJSONStringify([{ role: "model", content: fullContent }]) ?? "[]",
          [SemanticConventions.RAW_OUTPUT]: safeJsonStringify(chunks),
        });

        // Get usage from the aggregated response if available
        if (streamResult.response) {
          const response = await streamResult.response;
          if (response?.usageMetadata) {
            const usage = response.usageMetadata;
            if (usage.promptTokenCount) {
              span.setAttribute(SemanticConventions.LLM_TOKEN_COUNT_PROMPT, usage.promptTokenCount);
            }
            if (usage.candidatesTokenCount) {
              span.setAttribute(SemanticConventions.LLM_TOKEN_COUNT_COMPLETION, usage.candidatesTokenCount);
            }
            if (usage.totalTokenCount) {
              span.setAttribute(SemanticConventions.LLM_TOKEN_COUNT_TOTAL, usage.totalTokenCount);
            }
          }
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
    };

    return {
      ...streamResult,
      stream: wrappedStream(),
    };
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

/**
 * Reset the patched state (for testing only).
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
