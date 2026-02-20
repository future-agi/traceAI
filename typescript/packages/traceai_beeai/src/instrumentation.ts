/**
 * OpenTelemetry instrumentation for IBM BeeAI Framework.
 *
 * This module provides tracing for the beeai-framework SDK by wrapping
 * agent execution, tool calls, and LLM interactions.
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
  LLMSystem,
} from "@traceai/fi-semantic-conventions";
import { VERSION } from "./version";

export interface BeeAIInstrumentationConfig {
  instrumentationConfig?: Record<string, unknown>;
  traceConfig?: TraceConfigOptions;
}

/**
 * BeeAI Instrumentation class.
 *
 * Provides automatic instrumentation for the beeai-framework SDK.
 */
export class BeeAIInstrumentation extends InstrumentationBase {
  private fiTracer!: FITracer;
  private _traceConfig?: TraceConfigOptions;

  constructor(config: BeeAIInstrumentationConfig = {}) {
    super(
      "@traceai/fi-instrumentation-beeai",
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
      "beeai-framework",
      ["^0.1.0"],
      this.patch.bind(this),
      this.unpatch.bind(this)
    );
    return module;
  }

  /**
   * Manually instrument the beeai-framework module.
   */
  manuallyInstrument(beeModule: any): void {
    this.patch(beeModule);
  }

  private patch(beeModule: any & { _fiPatched?: boolean }): any {
    if (beeModule?._fiPatched || _isFIPatched) {
      return beeModule;
    }

    const instrumentation = this;

    // Wrap BeeAgent.run method
    if (beeModule.BeeAgent?.prototype?.run) {
      this._wrap(
        beeModule.BeeAgent.prototype,
        "run",
        (original: Function) => {
          return function patchedRun(this: any, ...args: any[]) {
            return instrumentation.traceAgentRun(original, this, args);
          };
        }
      );
    }

    // Wrap ReActAgent.run method if different from BeeAgent
    if (beeModule.ReActAgent?.prototype?.run &&
        beeModule.ReActAgent !== beeModule.BeeAgent) {
      this._wrap(
        beeModule.ReActAgent.prototype,
        "run",
        (original: Function) => {
          return function patchedRun(this: any, ...args: any[]) {
            return instrumentation.traceAgentRun(original, this, args);
          };
        }
      );
    }

    beeModule._fiPatched = true;
    _isFIPatched = true;
    return beeModule;
  }

  private unpatch(beeModule: any & { _fiPatched?: boolean }): void {
    if (beeModule?.BeeAgent?.prototype?.run) {
      this._unwrap(beeModule.BeeAgent.prototype, "run");
    }
    if (beeModule?.ReActAgent?.prototype?.run &&
        beeModule.ReActAgent !== beeModule.BeeAgent) {
      this._unwrap(beeModule.ReActAgent.prototype, "run");
    }
    if (beeModule) {
      beeModule._fiPatched = false;
    }
    _isFIPatched = false;
  }

  private async traceAgentRun(
    original: Function,
    instance: any,
    args: any[]
  ): Promise<any> {
    const input = args[0];
    const agentName = instance.constructor?.name || "BeeAgent";

    const span = this.fiTracer.startSpan(`${agentName} Run`, {
      kind: SpanKind.INTERNAL,
      attributes: {
        [SemanticConventions.FI_SPAN_KIND]: FISpanKind.AGENT,
        [SemanticConventions.LLM_PROVIDER]: "beeai",
        [SemanticConventions.INPUT_VALUE]: safeJsonStringify(input),
        [SemanticConventions.INPUT_MIME_TYPE]: MimeType.JSON,
        [SemanticConventions.RAW_INPUT]: safeJsonStringify(input),
      },
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

      // Handle regular response
      span.setAttributes({
        [SemanticConventions.OUTPUT_VALUE]: safeJsonStringify(result),
        [SemanticConventions.OUTPUT_MIME_TYPE]: MimeType.JSON,
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

  private async *wrapAsyncIterator(
    iterator: AsyncIterable<any>,
    span: Span
  ): AsyncIterable<any> {
    const chunks: any[] = [];
    let finalOutput: any;

    try {
      for await (const chunk of iterator) {
        chunks.push(chunk);
        if (chunk.finalOutput !== undefined) {
          finalOutput = chunk.finalOutput;
        }
        yield chunk;
      }

      span.setAttributes({
        [SemanticConventions.OUTPUT_VALUE]: safeJsonStringify(finalOutput || chunks),
        [SemanticConventions.OUTPUT_MIME_TYPE]: MimeType.JSON,
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
