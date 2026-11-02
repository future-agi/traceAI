/**
 * OpenTelemetry instrumentation for AWS Strands Agents SDK.
 *
 * Legacy SDK 0.1.x outer-invocation instrumentation. SDK 1.23+ applications
 * use StrandsSpanExporter with the framework's native OpenTelemetry spans.
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

export interface StrandsInstrumentationConfig {
  instrumentationConfig?: Record<string, unknown>;
  traceConfig?: TraceConfigOptions;
}

/**
 * Strands Instrumentation class.
 *
 * Provides automatic instrumentation for the @strands-agents/sdk.
 */
export class StrandsInstrumentation extends InstrumentationBase {
  private fiTracer!: FITracer;
  private _traceConfig?: TraceConfigOptions;
  private patchedModules?: Set<object>;

  constructor(config: StrandsInstrumentationConfig = {}) {
    super(
      "@traceai/fi-instrumentation-strands",
      VERSION,
      config.instrumentationConfig || {}
    );
    this._traceConfig = config.traceConfig;
    // InstrumentationBase invokes enable() before the subclass config exists.
    this.fiTracer = new FITracer({ tracer: this.tracer, traceConfig: this._traceConfig });
  }

  override enable(): void {
    super.enable();
    this.fiTracer = new FITracer({
      tracer: this.tracer,
      traceConfig: this._traceConfig,
    });
  }

  override disable(): void {
    for (const module of this.patchedModules ?? []) this.unpatch(module);
    super.disable();
  }

  protected init() {
    const module = new InstrumentationNodeModuleDefinition(
      "@strands-agents/sdk",
      ["^0.1.0"],
      this.patch.bind(this),
      this.unpatch.bind(this)
    );
    return module;
  }

  /**
   * Manually instrument the @strands-agents/sdk module.
   */
  manuallyInstrument(strandsModule: any): void {
    this.patch(strandsModule);
  }

  private patch(strandsModule: any): any {
    // Modern Strands owns its complete span lifecycle (including streaming and
    // orchestration). Leave its ESM namespace and Agent prototype untouched.
    // Configure StrandsSpanExporter on the application's OTel provider instead.
    if (typeof strandsModule?.AgentTrace === "function") {
      return strandsModule;
    }
    const prototype = strandsModule?.Agent?.prototype;
    if (!prototype || patchedPrototypes.has(prototype)) return strandsModule;

    const instrumentation = this;

    // Wrap Agent class
    if (strandsModule.Agent?.prototype?.invoke) {
      this._wrap(
        strandsModule.Agent.prototype,
        "invoke",
        (original: Function) => {
          return function patchedInvoke(this: any, ...args: any[]) {
            return instrumentation.traceAgentInvoke(original, this, args);
          };
        }
      );
    }

    // Also wrap __call__ or run if they exist
    if (strandsModule.Agent?.prototype?.run) {
      this._wrap(
        strandsModule.Agent.prototype,
        "run",
        (original: Function) => {
          return function patchedRun(this: any, ...args: any[]) {
            return instrumentation.traceAgentInvoke(original, this, args);
          };
        }
      );
    }

    // ESM namespace objects are immutable. Track ownership outside the module.
    (this.patchedModules ??= new Set()).add(strandsModule);
    patchedPrototypes.add(prototype);
    return strandsModule;
  }

  private unpatch(strandsModule: any): void {
    if (!this.patchedModules?.delete(strandsModule)) return;
    if (strandsModule?.Agent?.prototype?.invoke) {
      this._unwrap(strandsModule.Agent.prototype, "invoke");
    }
    if (strandsModule?.Agent?.prototype?.run) {
      this._unwrap(strandsModule.Agent.prototype, "run");
    }
    patchedPrototypes.delete(strandsModule.Agent.prototype);
  }

  private async traceAgentInvoke(
    original: Function,
    instance: any,
    args: any[]
  ): Promise<any> {
    const input = args[0];
    const agentName = instance.name || instance.constructor?.name || "StrandsAgent";
    const model = instance.model || instance.modelId;

    const attributes: Record<string, any> = {
      [SemanticConventions.FI_SPAN_KIND]: FISpanKind.AGENT,
      [SemanticConventions.LLM_PROVIDER]: "strands",
      [SemanticConventions.INPUT_VALUE]: safeJsonStringify(input),
      [SemanticConventions.INPUT_MIME_TYPE]: MimeType.JSON,
      [SemanticConventions.RAW_INPUT]: safeJsonStringify(input),
    };

    if (model) {
      attributes[SemanticConventions.LLM_MODEL_NAME] = model;
      // Detect provider from model name
      if (typeof model === "string") {
        if (model.includes("anthropic") || model.includes("claude")) {
          attributes[SemanticConventions.LLM_PROVIDER] = LLMProvider.ANTHROPIC;
        } else if (model.includes("amazon") || model.includes("titan")) {
          attributes[SemanticConventions.LLM_PROVIDER] = "amazon";
        } else if (model.includes("cohere")) {
          attributes[SemanticConventions.LLM_PROVIDER] = "cohere";
        } else if (model.includes("meta") || model.includes("llama")) {
          attributes[SemanticConventions.LLM_PROVIDER] = "meta";
        }
      }
    }

    const span = this.fiTracer.startSpan(`${agentName} Invoke`, {
      kind: SpanKind.INTERNAL,
      attributes,
    });

    const execContext = trace.setSpan(context.active(), span);

    try {
      const result = await context.with(execContext, () => {
        return original.apply(instance, args);
      });

      // Handle async iterator (streaming response)
      if (result && typeof result[Symbol.asyncIterator] === "function") {
        return this.wrapAsyncIterator(result, span);
      }

      // Extract output text if available
      let outputText: string | undefined;
      if (result) {
        if (typeof result === "string") {
          outputText = result;
        } else if (result.text) {
          outputText = result.text;
        } else if (result.content) {
          outputText = typeof result.content === "string"
            ? result.content
            : safeJsonStringify(result.content);
        } else if (result.message) {
          outputText = typeof result.message === "string"
            ? result.message
            : safeJsonStringify(result.message);
        }
      }

      span.setAttributes({
        [SemanticConventions.OUTPUT_VALUE]: outputText || safeJsonStringify(result),
        [SemanticConventions.OUTPUT_MIME_TYPE]: MimeType.JSON,
        [SemanticConventions.RAW_OUTPUT]: safeJsonStringify(result),
      });

      // Extract usage if available
      if (result?.usage) {
        if (result.usage.inputTokens !== undefined) {
          span.setAttribute(
            SemanticConventions.LLM_TOKEN_COUNT_PROMPT,
            result.usage.inputTokens
          );
        }
        if (result.usage.outputTokens !== undefined) {
          span.setAttribute(
            SemanticConventions.LLM_TOKEN_COUNT_COMPLETION,
            result.usage.outputTokens
          );
        }
        if (result.usage.totalTokens !== undefined) {
          span.setAttribute(
            SemanticConventions.LLM_TOKEN_COUNT_TOTAL,
            result.usage.totalTokens
          );
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

  private async *wrapAsyncIterator(
    iterator: AsyncIterable<any>,
    span: Span
  ): AsyncIterable<any> {
    const chunks: any[] = [];
    let finalOutput: string = "";
    let usage: { inputTokens?: number; outputTokens?: number; totalTokens?: number } = {};

    try {
      for await (const chunk of iterator) {
        chunks.push(chunk);

        // Accumulate text content
        if (chunk.text) {
          finalOutput += chunk.text;
        } else if (chunk.delta?.text) {
          finalOutput += chunk.delta.text;
        }

        // Capture usage from final chunk
        if (chunk.usage) {
          usage = chunk.usage;
        }

        yield chunk;
      }

      span.setAttributes({
        [SemanticConventions.OUTPUT_VALUE]: finalOutput || safeJsonStringify(chunks),
        [SemanticConventions.OUTPUT_MIME_TYPE]: MimeType.JSON,
        [SemanticConventions.RAW_OUTPUT]: safeJsonStringify(chunks),
      });

      // Set usage metrics
      if (usage.inputTokens !== undefined) {
        span.setAttribute(SemanticConventions.LLM_TOKEN_COUNT_PROMPT, usage.inputTokens);
      }
      if (usage.outputTokens !== undefined) {
        span.setAttribute(SemanticConventions.LLM_TOKEN_COUNT_COMPLETION, usage.outputTokens);
      }
      if (usage.totalTokens !== undefined) {
        span.setAttribute(SemanticConventions.LLM_TOKEN_COUNT_TOTAL, usage.totalTokens);
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

// A prototype can be owned by only one legacy instrumentation instance.
const patchedPrototypes = new Set<object>();

/**
 * Check if the module has been patched.
 */
export function isPatched(): boolean {
  return patchedPrototypes.size > 0;
}

/**
 * Resets the internal patched state. Intended for testing only.
 */
export function _resetPatchedStateForTesting(): void {
  patchedPrototypes.clear();
}

// Helper functions

function safeJsonStringify(obj: unknown): string {
  try {
    return JSON.stringify(obj);
  } catch {
    return String(obj);
  }
}
