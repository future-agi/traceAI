/**
 * OpenTelemetry instrumentation for Guardrails AI.
 *
 * Guardrails AI provides validation and structural guarantees for LLM outputs.
 * This instrumentation captures guard execution and validation results.
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

export interface GuardrailsInstrumentationConfig {
  instrumentationConfig?: Record<string, unknown>;
  traceConfig?: TraceConfigOptions;
}

/**
 * Guardrails AI Instrumentation class.
 *
 * Provides automatic instrumentation for the @guardrails-ai/core SDK.
 */
export class GuardrailsInstrumentation extends InstrumentationBase {
  private fiTracer!: FITracer;
  private _traceConfig?: TraceConfigOptions;

  constructor(config: GuardrailsInstrumentationConfig = {}) {
    super(
      "@traceai/fi-instrumentation-guardrails",
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
      "@guardrails-ai/core",
      ["^0.1.0"],
      this.patch.bind(this),
      this.unpatch.bind(this)
    );
    return module;
  }

  /**
   * Manually instrument the @guardrails-ai/core module.
   */
  manuallyInstrument(guardrailsModule: any): void {
    this.patch(guardrailsModule);
  }

  private patch(guardrailsModule: any & { _fiPatched?: boolean }): any {
    if (guardrailsModule?._fiPatched || _isFIPatched) {
      return guardrailsModule;
    }

    const instrumentation = this;

    // Wrap Guard.validate method
    if (guardrailsModule.Guard?.prototype?.validate) {
      this._wrap(
        guardrailsModule.Guard.prototype,
        "validate",
        (original: Function) => {
          return function patchedValidate(this: any, ...args: any[]) {
            return instrumentation.traceGuardValidation(original, this, args, "validate");
          };
        }
      );
    }

    // Wrap Guard.parse method
    if (guardrailsModule.Guard?.prototype?.parse) {
      this._wrap(
        guardrailsModule.Guard.prototype,
        "parse",
        (original: Function) => {
          return function patchedParse(this: any, ...args: any[]) {
            return instrumentation.traceGuardValidation(original, this, args, "parse");
          };
        }
      );
    }

    // Wrap Guard.__call__ method if it exists
    if (guardrailsModule.Guard?.prototype?.call) {
      this._wrap(
        guardrailsModule.Guard.prototype,
        "call",
        (original: Function) => {
          return function patchedCall(this: any, ...args: any[]) {
            return instrumentation.traceGuardValidation(original, this, args, "call");
          };
        }
      );
    }

    guardrailsModule._fiPatched = true;
    _isFIPatched = true;
    return guardrailsModule;
  }

  private unpatch(guardrailsModule: any & { _fiPatched?: boolean }): void {
    if (guardrailsModule?.Guard?.prototype?.validate) {
      this._unwrap(guardrailsModule.Guard.prototype, "validate");
    }
    if (guardrailsModule?.Guard?.prototype?.parse) {
      this._unwrap(guardrailsModule.Guard.prototype, "parse");
    }
    if (guardrailsModule?.Guard?.prototype?.call) {
      this._unwrap(guardrailsModule.Guard.prototype, "call");
    }
    if (guardrailsModule) {
      guardrailsModule._fiPatched = false;
    }
    _isFIPatched = false;
  }

  private async traceGuardValidation(
    original: Function,
    instance: any,
    args: any[],
    operation: string
  ): Promise<any> {
    const input = args[0];
    const guardName = instance.name || instance.constructor?.name || "Guard";

    const span = this.fiTracer.startSpan(`Guardrails ${operation}: ${guardName}`, {
      kind: SpanKind.INTERNAL,
      attributes: {
        [SemanticConventions.FI_SPAN_KIND]: FISpanKind.GUARDRAIL,
        [SemanticConventions.INPUT_VALUE]: safeJsonStringify(input),
        [SemanticConventions.INPUT_MIME_TYPE]: MimeType.JSON,
        [SemanticConventions.RAW_INPUT]: safeJsonStringify(input),
        "guardrails.guard_name": guardName,
        "guardrails.operation": operation,
      },
    });

    const execContext = trace.setSpan(context.active(), span);

    try {
      const result = await context.with(execContext, () => {
        return original.apply(instance, args);
      });

      // Extract validation result details
      const validationPassed = result?.validated !== false && result?.valid !== false;

      span.setAttributes({
        [SemanticConventions.OUTPUT_VALUE]: safeJsonStringify(result),
        [SemanticConventions.OUTPUT_MIME_TYPE]: MimeType.JSON,
        [SemanticConventions.RAW_OUTPUT]: safeJsonStringify(result),
        "guardrails.validation_passed": validationPassed,
      });

      if (result?.reask) {
        span.setAttribute("guardrails.reask_count", result.reask_count || 1);
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
      span.setAttribute("guardrails.validation_passed", false);
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
