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
  TracerProvider,
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
  private readonly manualModules = new Set<object>();
  private readonly patchedPrototypes = new Set<object>();

  constructor(config: GuardrailsInstrumentationConfig = {}) {
    // The base constructor can call enable() before traceConfig is assigned.
    super(
      "@traceai/fi-instrumentation-guardrails",
      VERSION,
      { ...config.instrumentationConfig, enabled: false }
    );
    this._traceConfig = config.traceConfig;
    this.setConfig(config.instrumentationConfig || {});
    if (this.getConfig().enabled) this.enable();
  }

  override enable(): void {
    this.fiTracer = new FITracer({
      tracer: this.tracer,
      traceConfig: this._traceConfig,
    });
    super.enable();
    for (const module of this.manualModules) this.patch(module);
  }

  override setTracerProvider(provider: TracerProvider): void {
    super.setTracerProvider(provider);
    this.fiTracer = new FITracer({
      tracer: this.tracer,
      traceConfig: this._traceConfig,
    });
  }

  override disable(): void {
    super.disable();
    for (const prototype of this.patchedPrototypes) {
      this.unpatch({ Guard: { prototype } });
    }
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
    if (guardrailsModule == null) return;
    this.manualModules.add(guardrailsModule);
    if (this.isEnabled()) this.patch(guardrailsModule);
  }

  private patch(guardrailsModule: any): any {
    const prototype = guardrailsModule?.Guard?.prototype;
    if (!prototype || patchOwners.has(prototype)) return guardrailsModule;
    if (!["validate", "parse", "call"].some(method => typeof prototype[method] === "function")) {
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

    // ESM namespaces are immutable. Track the mutable prototype, not exports.
    patchOwners.set(prototype, this);
    this.patchedPrototypes.add(prototype);
    return guardrailsModule;
  }

  private unpatch(guardrailsModule: any): void {
    const prototype = guardrailsModule?.Guard?.prototype;
    if (patchOwners.get(prototype) !== this) return;
    for (const method of ["validate", "parse", "call"]) {
      if (typeof prototype[method] === "function") this._unwrap(prototype, method);
    }
    patchOwners.delete(prototype);
    this.patchedPrototypes.delete(prototype);
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

      span.setAttributes({
        [SemanticConventions.OUTPUT_VALUE]: safeJsonStringify(result),
        [SemanticConventions.OUTPUT_MIME_TYPE]: MimeType.JSON,
        [SemanticConventions.RAW_OUTPUT]: safeJsonStringify(result),
      });
      // The JS bridge exposes ValidationOutcome.validationPassed, not valid.
      // A missing/undecodable outcome must not be reported as a success.
      const validationPassed = getValidationPassed(result);
      if (validationPassed !== undefined) {
        span.setAttribute("guardrails.validation_passed", validationPassed);
      }

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

// One owner per Guard prototype, including multiple installed SDK copies.
const patchOwners = new Map<object, GuardrailsInstrumentation>();

/** Check whether any Guard prototype is patched. */
export function isPatched(): boolean {
  return patchOwners.size > 0;
}

/** Restore patched methods and reset ownership (for testing only). */
export function _resetPatchedStateForTesting(): void {
  for (const owner of new Set(patchOwners.values())) owner.disable();
}

function getValidationPassed(result: unknown): boolean | undefined {
  if (!result || typeof result !== "object") return undefined;
  if ("validationPassed" in result && typeof result.validationPassed === "boolean") {
    return result.validationPassed;
  }
  // Retain compatibility with existing wrappers using the legacy fields.
  const validated = "validated" in result ? result.validated : undefined;
  const valid = "valid" in result ? result.valid : undefined;
  if (validated === false || valid === false) return false;
  if (validated === true || valid === true) return true;
  return undefined;
}

function safeJsonStringify(obj: unknown): string {
  try {
    return JSON.stringify(obj);
  } catch {
    return String(obj);
  }
}
