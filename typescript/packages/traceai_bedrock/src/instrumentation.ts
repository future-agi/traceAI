import {
  InstrumentationBase,
  InstrumentationConfig,
  InstrumentationModuleDefinition,
  InstrumentationNodeModuleDefinition,
} from "@opentelemetry/instrumentation";
import {
  context,
  diag,
  Exception,
  Span,
  SpanKind,
  SpanStatusCode,
  trace,
  Tracer,
  TracerProvider,
} from "@opentelemetry/api";
import {
  getAttributesFromContext,
  getErrorMessage,
  FITracer,
  TraceConfigOptions,
} from "@traceai/fi-core";
import {
  InvokeModelCommand,
  InvokeModelWithResponseStreamCommand,
  ConverseCommand,
  ConverseStreamCommand,
  BedrockRuntimeClient,
  InvokeModelResponse,
  ConverseResponse,
} from "@aws-sdk/client-bedrock-runtime";
import { extractInvokeModelRequestAttributes } from "./attributes/invoke-model-request-attributes";
import { extractInvokeModelResponseAttributes } from "./attributes/invoke-model-response-attributes";
import { extractConverseRequestAttributes } from "./attributes/converse-request-attributes";
import { extractConverseResponseAttributes } from "./attributes/converse-response-attributes";
import { createInvokeModelStreamAccumulator } from "./attributes/invoke-model-streaming-response-attributes";
import { createConverseStreamAccumulator } from "./attributes/converse-streaming-response-attributes";
import { getSystemFromModelId, setBasicSpanAttributes } from "./attributes/attribute-helpers";
import { capture, observeStream } from "./stream";

const MODULE_NAME = "@aws-sdk/client-bedrock-runtime";
const INSTRUMENTATION_NAME = "@traceai/bedrock";
const INSTRUMENTATION_VERSION = "0.1.1"; // x-release-please-version

interface BedrockModuleExports {
  BedrockRuntimeClient: typeof BedrockRuntimeClient;
}
type ModelCommand = InvokeModelCommand | InvokeModelWithResponseStreamCommand | ConverseCommand | ConverseStreamCommand;
// send has Promise and callback overloads; applying the original must keep either return contract.
type Send = (this: unknown, ...args: unknown[]) => unknown;

let _isBedrockPatched = false;
export function isPatched(): boolean {
  return _isBedrockPatched;
}

/** Record errors from any realm without exposing text when FI privacy settings hide it. */
export function recordBedrockError(span: Span, error: unknown): void {
  if (error != null && typeof error === "object") {
    span.recordException(error as Exception);
    span.setStatus({ code: SpanStatusCode.ERROR, message: getErrorMessage(error) });
    return;
  }
  const text = String(error);
  span.recordException(text);
  span.setStatus({ code: SpanStatusCode.ERROR, message: text });
}

/** Instruments InvokeModel, InvokeModelWithResponseStream, Converse and ConverseStream. */
export class BedrockInstrumentation extends InstrumentationBase {
  private fiTracer: FITracer;
  private tracerProvider?: TracerProvider;
  private traceConfig?: TraceConfigOptions;

  constructor({
    instrumentationConfig,
    traceConfig,
    tracerProvider,
  }: {
    instrumentationConfig?: InstrumentationConfig;
    /** Privacy settings applied to input, output and exception attributes. */
    traceConfig?: TraceConfigOptions;
    /** Optional provider; otherwise use the global OpenTelemetry provider. */
    tracerProvider?: TracerProvider;
  } = {}) {
    super(INSTRUMENTATION_NAME, INSTRUMENTATION_VERSION, Object.assign({}, instrumentationConfig));
    this.tracerProvider = tracerProvider;
    this.traceConfig = traceConfig;
    this.fiTracer = new FITracer({
      tracer: this.tracerProvider?.getTracer(INSTRUMENTATION_NAME) ?? this.tracer,
      traceConfig,
    });
  }

  /** Patch explicitly when module loading order cannot be controlled. */
  public manuallyInstrument(module: BedrockModuleExports) {
    diag.debug(`Manually instrumenting ${MODULE_NAME}`);
    this.patch(module);
  }

  protected init(): InstrumentationModuleDefinition[] {
    return [new InstrumentationNodeModuleDefinition(
      MODULE_NAME, ["^3.0.0"], this.patch.bind(this), this.unpatch.bind(this),
    )];
  }

  get tracer(): Tracer {
    return this.tracerProvider?.getTracer(this.instrumentationName) ?? super.tracer;
  }

  setTracerProvider(tracerProvider: TracerProvider): void {
    super.setTracerProvider(tracerProvider);
    this.tracerProvider = tracerProvider;
    this.fiTracer = new FITracer({ tracer: this.tracer, traceConfig: this.traceConfig });
  }

  private patch(moduleExports: BedrockModuleExports, moduleVersion?: string): BedrockModuleExports {
    diag.debug(`Applying patch for ${MODULE_NAME}@${moduleVersion}`);
    if (moduleExports?.BedrockRuntimeClient) {
      const instrumentation = this;
      this._wrap(moduleExports.BedrockRuntimeClient.prototype, "send", (sdkSend) => {
        const original = sdkSend as Send;
        return function patchedSend(this: unknown, ...args: unknown[]) {
          const command = args[0] as ModelCommand | undefined;
          const name = command?.constructor?.name;
          if (name === "InvokeModelCommand" || name === "InvokeModelWithResponseStreamCommand" ||
              name === "ConverseCommand" || name === "ConverseStreamCommand") {
            return instrumentation.handleCommand(command!, original, this, args);
          }
          return original.apply(this, args);
        } as BedrockRuntimeClient["send"];
      });
      _isBedrockPatched = true;
    }
    return moduleExports;
  }

  private unpatch(moduleExports: BedrockModuleExports, moduleVersion?: string): void {
    diag.debug(`Removing patch for ${MODULE_NAME}@${moduleVersion}`);
    if (moduleExports?.BedrockRuntimeClient) {
      this._unwrap(moduleExports.BedrockRuntimeClient.prototype, "send");
      _isBedrockPatched = false;
    }
  }

  private handleCommand(command: ModelCommand, original: Send, client: unknown, args: unknown[]): unknown {
    const name = command.constructor.name;
    const converse = name === "ConverseCommand" || name === "ConverseStreamCommand";
    const streaming = name === "ConverseStreamCommand" || name === "InvokeModelWithResponseStreamCommand";
    const parentContext = context.active();
    const span = this.fiTracer.startSpan(converse ? "bedrock.converse" : "bedrock.invoke_model", {
      kind: SpanKind.INTERNAL,
    });
    const spanContext = trace.setSpan(parentContext, span);
    const system = getSystemFromModelId(command.input.modelId ?? "");
    capture(() => {
      setBasicSpanAttributes(span, system);
      span.setAttributes(getAttributesFromContext(parentContext));
      if (converse) extractConverseRequestAttributes({ span, command: command as ConverseCommand });
      else extractInvokeModelRequestAttributes({ span, command: command as InvokeModelCommand, system });
    });

    let ended = false;
    let failed = false;
    const recordError = (error: unknown) => {
      if (ended) return;
      failed = true;
      recordBedrockError(span, error);
    };
    const finish = (...errors: [] | [unknown]) => {
      if (ended) return;
      if (errors.length) recordError(errors[0]);
      ended = true;
      if (!failed) span.setStatus({ code: SpanStatusCode.OK });
      span.end();
    };
    const onResponse = (response: unknown): unknown => {
      if (streaming) {
        const key = converse ? "stream" : "body";
        const source = (response as Record<string, AsyncIterable<unknown>> | undefined)?.[key];
        if (source?.[Symbol.asyncIterator]) {
          const accumulator = converse
            ? createConverseStreamAccumulator(span)
            : createInvokeModelStreamAccumulator(span, system);
          return { ...response as object, [key]: observeStream(source, spanContext, accumulator, finish, recordError) };
        }
      } else {
        capture(() => {
          if (converse) extractConverseResponseAttributes({ span, response: response as ConverseResponse });
          else extractInvokeModelResponseAttributes({ span, response: response as InvokeModelResponse, system });
        });
      }
      finish();
      return response;
    };

    // AWS supports send(command, callback) and send(command, options, callback).
    const callbackIndex = typeof args[1] === "function" ? 1 : typeof args[2] === "function" ? 2 : -1;
    if (callbackIndex !== -1) {
      const callback = args[callbackIndex] as (...values: unknown[]) => unknown;
      args[callbackIndex] = function (this: unknown, ...values: unknown[]) {
        if (values[0]) finish(values[0]);
        else values[1] = onResponse(values[1]);
        // Do not swallow callback exceptions or replace its receiver/return value.
        return context.with(parentContext, () => callback.apply(this, values));
      };
    }

    let result: unknown;
    try {
      result = context.with(spanContext, () => original.apply(client, args));
    } catch (error) {
      finish(error);
      throw error;
    }
    if (callbackIndex !== -1) return result;
    if (result && typeof (result as PromiseLike<unknown>).then === "function") {
      return (result as PromiseLike<unknown>).then(onResponse, (error: unknown) => {
        finish(error);
        throw error;
      });
    }
    // Preserve non-Promise results rather than imposing an async API on a custom client.
    finish();
    return result;
  }
}
