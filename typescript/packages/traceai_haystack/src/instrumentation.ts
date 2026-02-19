/**
 * OpenTelemetry instrumentation for Haystack.
 *
 * Haystack is a framework for building RAG pipelines and search systems.
 * This instrumentation captures pipeline execution, component runs, and retrieval operations.
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

export interface HaystackInstrumentationConfig {
  instrumentationConfig?: Record<string, unknown>;
  traceConfig?: TraceConfigOptions;
}

/**
 * Haystack Instrumentation class.
 *
 * Provides automatic instrumentation for the @haystack-ai/haystack SDK.
 */
export class HaystackInstrumentation extends InstrumentationBase {
  private fiTracer!: FITracer;
  private _traceConfig?: TraceConfigOptions;

  constructor(config: HaystackInstrumentationConfig = {}) {
    super(
      "@traceai/fi-instrumentation-haystack",
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
      "@haystack-ai/haystack",
      ["^2.0.0"],
      this.patch.bind(this),
      this.unpatch.bind(this)
    );
    return module;
  }

  /**
   * Manually instrument the @haystack-ai/haystack module.
   */
  manuallyInstrument(haystackModule: any): void {
    this.patch(haystackModule);
  }

  private patch(haystackModule: any & { _fiPatched?: boolean }): any {
    if (haystackModule?._fiPatched || _isFIPatched) {
      return haystackModule;
    }

    const instrumentation = this;

    // Wrap Pipeline.run method
    if (haystackModule.Pipeline?.prototype?.run) {
      this._wrap(
        haystackModule.Pipeline.prototype,
        "run",
        (original: Function) => {
          return function patchedRun(this: any, ...args: any[]) {
            return instrumentation.tracePipelineRun(original, this, args);
          };
        }
      );
    }

    // Wrap component run methods if accessible
    if (haystackModule.Component?.prototype?.run) {
      this._wrap(
        haystackModule.Component.prototype,
        "run",
        (original: Function) => {
          return function patchedComponentRun(this: any, ...args: any[]) {
            return instrumentation.traceComponentRun(original, this, args);
          };
        }
      );
    }

    haystackModule._fiPatched = true;
    _isFIPatched = true;
    return haystackModule;
  }

  private unpatch(haystackModule: any & { _fiPatched?: boolean }): void {
    if (haystackModule?.Pipeline?.prototype?.run) {
      this._unwrap(haystackModule.Pipeline.prototype, "run");
    }
    if (haystackModule?.Component?.prototype?.run) {
      this._unwrap(haystackModule.Component.prototype, "run");
    }
    if (haystackModule) {
      haystackModule._fiPatched = false;
    }
    _isFIPatched = false;
  }

  private async tracePipelineRun(
    original: Function,
    instance: any,
    args: any[]
  ): Promise<any> {
    const input = args[0] || {};
    const pipelineName = instance.name || instance.constructor?.name || "Pipeline";

    const span = this.fiTracer.startSpan(`Haystack Pipeline: ${pipelineName}`, {
      kind: SpanKind.INTERNAL,
      attributes: {
        [SemanticConventions.FI_SPAN_KIND]: FISpanKind.CHAIN,
        [SemanticConventions.INPUT_VALUE]: safeJsonStringify(input),
        [SemanticConventions.INPUT_MIME_TYPE]: MimeType.JSON,
        [SemanticConventions.RAW_INPUT]: safeJsonStringify(input),
        "haystack.pipeline_name": pipelineName,
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

  private async traceComponentRun(
    original: Function,
    instance: any,
    args: any[]
  ): Promise<any> {
    const input = args[0] || {};
    const componentName = instance.name || instance.constructor?.name || "Component";
    const componentType = instance.type || instance.constructor?.name || "unknown";

    // Determine span kind based on component type
    let spanKind = FISpanKind.CHAIN;
    if (componentType.toLowerCase().includes("retriever")) {
      spanKind = FISpanKind.RETRIEVER;
    } else if (componentType.toLowerCase().includes("generator") ||
               componentType.toLowerCase().includes("llm")) {
      spanKind = FISpanKind.LLM;
    } else if (componentType.toLowerCase().includes("embed")) {
      spanKind = FISpanKind.EMBEDDING;
    }

    const span = this.fiTracer.startSpan(`Haystack Component: ${componentName}`, {
      kind: SpanKind.INTERNAL,
      attributes: {
        [SemanticConventions.FI_SPAN_KIND]: spanKind,
        [SemanticConventions.INPUT_VALUE]: safeJsonStringify(input),
        [SemanticConventions.INPUT_MIME_TYPE]: MimeType.JSON,
        [SemanticConventions.RAW_INPUT]: safeJsonStringify(input),
        "haystack.component_name": componentName,
        "haystack.component_type": componentType,
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
