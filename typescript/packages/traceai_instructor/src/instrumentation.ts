/**
 * OpenTelemetry instrumentation for Instructor.
 *
 * Instructor is a library for structured output from LLMs using Pydantic models.
 * This instrumentation captures structured extraction calls.
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

export interface InstructorInstrumentationConfig {
  instrumentationConfig?: Record<string, unknown>;
  traceConfig?: TraceConfigOptions;
}

/**
 * Instructor Instrumentation class.
 *
 * Provides automatic instrumentation for the @instructor-ai/instructor SDK.
 */
export class InstructorInstrumentation extends InstrumentationBase {
  private fiTracer!: FITracer;
  private _traceConfig?: TraceConfigOptions;

  constructor(config: InstructorInstrumentationConfig = {}) {
    super(
      "@traceai/fi-instrumentation-instructor",
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
      "@instructor-ai/instructor",
      ["^1.0.0"],
      this.patch.bind(this),
      this.unpatch.bind(this)
    );
    return module;
  }

  /**
   * Manually instrument the @instructor-ai/instructor module.
   */
  manuallyInstrument(instructorModule: any): void {
    this.patch(instructorModule);
  }

  private patch(instructorModule: any & { _fiPatched?: boolean }): any {
    if (instructorModule?._fiPatched || _isFIPatched) {
      return instructorModule;
    }

    const instrumentation = this;

    // Wrap the Instructor class create method
    if (instructorModule.Instructor?.prototype?.chat?.completions?.create) {
      this._wrap(
        instructorModule.Instructor.prototype.chat.completions,
        "create",
        (original: Function) => {
          return function patchedCreate(this: any, ...args: any[]) {
            return instrumentation.traceStructuredExtraction(original, this, args);
          };
        }
      );
    }

    // Wrap default export if it's a function
    if (typeof instructorModule.default === "function") {
      const originalDefault = instructorModule.default;
      instructorModule.default = function patchedInstructor(...args: any[]) {
        const client = originalDefault.apply(this, args);
        if (client?.chat?.completions?.create) {
          const originalCreate = client.chat.completions.create.bind(client.chat.completions);
          client.chat.completions.create = function(...createArgs: any[]) {
            return instrumentation.traceStructuredExtraction(originalCreate, this, createArgs);
          };
        }
        return client;
      };
    }

    instructorModule._fiPatched = true;
    _isFIPatched = true;
    return instructorModule;
  }

  private unpatch(instructorModule: any & { _fiPatched?: boolean }): void {
    if (instructorModule?.Instructor?.prototype?.chat?.completions?.create) {
      this._unwrap(instructorModule.Instructor.prototype.chat.completions, "create");
    }
    if (instructorModule) {
      instructorModule._fiPatched = false;
    }
    _isFIPatched = false;
  }

  private async traceStructuredExtraction(
    original: Function,
    instance: any,
    args: any[]
  ): Promise<any> {
    const params = args[0] || {};
    const model = params.model || "unknown";
    const responseModel = params.response_model?.name || params.responseModel?.name || "unknown";

    const span = this.fiTracer.startSpan(`Instructor Extract: ${responseModel}`, {
      kind: SpanKind.INTERNAL,
      attributes: {
        [SemanticConventions.FI_SPAN_KIND]: FISpanKind.LLM,
        [SemanticConventions.LLM_PROVIDER]: "instructor",
        [SemanticConventions.LLM_MODEL_NAME]: model,
        [SemanticConventions.INPUT_VALUE]: safeJsonStringify(params),
        [SemanticConventions.INPUT_MIME_TYPE]: MimeType.JSON,
        [SemanticConventions.RAW_INPUT]: safeJsonStringify(params),
        "instructor.response_model": responseModel,
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
