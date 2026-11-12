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
  BedrockRuntimeClient,
  InvokeModelResponse,
  ConverseResponse,
} from "@aws-sdk/client-bedrock-runtime";
import { extractInvokeModelRequestAttributes } from "./attributes/invoke-model-request-attributes";
import { extractInvokeModelResponseAttributes } from "./attributes/invoke-model-response-attributes";
import { extractConverseRequestAttributes } from "./attributes/converse-request-attributes";
import { extractConverseResponseAttributes } from "./attributes/converse-response-attributes";
import { observeBedrockStream } from "./attributes/invoke-model-streaming-response-attributes";
import {
  getSystemFromModelId,
  setBasicSpanAttributes,
} from "./attributes/attribute-helpers";

const MODULE_NAME = "@aws-sdk/client-bedrock-runtime";
const INSTRUMENTATION_NAME = "@traceai/bedrock";
const INSTRUMENTATION_VERSION = "0.1.4"; // x-release-please-version

/**
 * AWS SDK module interface for proper typing
 * Defines the structure of the @aws-sdk/client-bedrock-runtime module exports
 */
interface BedrockModuleExports {
  BedrockRuntimeClient: typeof BedrockRuntimeClient;
}

/**
 * Track if the Bedrock instrumentation is patched
 */
let _isBedrockPatched = false;

/**
 * Type guard that checks if the Bedrock instrumentation is currently enabled
 * @returns {boolean} True if instrumentation is active, false otherwise
 */
export function isPatched(): boolean {
  return _isBedrockPatched;
}

/**
 * Records a failed call on the span: the exception event and an ERROR status whose description is
 * the error message only. Values that are not an `Error` of this realm (an error from another
 * realm, a plain object, a string) are handled the same way, instead of `String(error)`, which
 * gives "Name: message" or "[object Object]".
 */
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

/**
 *
 * Supports instrumentation of:
 * - InvokeModel commands (synchronous)
 * - InvokeModelWithResponseStream commands (streaming)
 * - Converse commands (conversation API)
 *
 * @param instrumentationConfig The config for the instrumentation @see {@link InstrumentationConfig}
 * @param traceConfig The FI trace configuration. Can be used to mask or redact sensitive information on spans. @see {@link TraceConfigOptions}
 */
export class BedrockInstrumentation extends InstrumentationBase {
  private fiTracer: FITracer;
  private tracerProvider?: TracerProvider;
  private traceConfig?: TraceConfigOptions;

  constructor({
    instrumentationConfig,
    traceConfig,
    tracerProvider,
  }: {
    /**
     * The config for the instrumentation
     * @see {@link InstrumentationConfig}
     */
    instrumentationConfig?: InstrumentationConfig;
    /**
     * The FI trace configuration. Can be used to mask or redact sensitive information on spans.
     * @see {@link TraceConfigOptions}
     */
    traceConfig?: TraceConfigOptions;
    /**
     * An optional custom trace provider to be used for tracing. If not provided, a tracer will be created using the global tracer provider.
     * This is useful if you want to use a non-global tracer provider.
     *
     * @see {@link TracerProvider}
     */
    tracerProvider?: TracerProvider;
  } = {}) {
    super(
      INSTRUMENTATION_NAME,
      INSTRUMENTATION_VERSION,
      Object.assign({}, instrumentationConfig),
    );
    this.tracerProvider = tracerProvider;
    this.traceConfig = traceConfig;
    this.fiTracer = new FITracer({
      tracer:
        this.tracerProvider?.getTracer(INSTRUMENTATION_NAME) ?? this.tracer,
      traceConfig,
    });
  }

  /**
   * Manually instruments the BedrockRuntimeClient, this allows for guaranteed patching of the module when import order is hard to control.
   */
  public manuallyInstrument(module: BedrockModuleExports) {
    diag.debug(`Manually instrumenting ${MODULE_NAME}`);
    this.patch(module);
  }

  /**
   * Initializes the instrumentation module definition for AWS SDK Bedrock Runtime
   * @returns {InstrumentationModuleDefinition<BedrockModuleExports>[]} Array containing the module definition
   */
  protected init(): InstrumentationModuleDefinition[] {
    const module =
      new InstrumentationNodeModuleDefinition(
        MODULE_NAME,
        ["^3.0.0"],
        this.patch.bind(this),
        this.unpatch.bind(this),
      );
    return [module];
  }

  get tracer(): Tracer {
    if (this.tracerProvider) {
      return this.tracerProvider.getTracer(this.instrumentationName);
    }
    return super.tracer;
  }

  setTracerProvider(tracerProvider: TracerProvider): void {
    super.setTracerProvider(tracerProvider);
    this.tracerProvider = tracerProvider;
    this.fiTracer = new FITracer({
      tracer: this.tracer,
      traceConfig: this.traceConfig,
    });
  }

  /**
   * Patches the BedrockRuntimeClient to intercept and instrument send() method calls
   * Wraps the send method to capture InvokeModel, InvokeModelWithResponseStream, and Converse commands
   *
   * @param moduleExports The module exports from @aws-sdk/client-bedrock-runtime
   * @param moduleVersion The version of the module being patched
   * @returns {BedrockModuleExports} The patched module exports
   */
  private patch(
    moduleExports: BedrockModuleExports,
    moduleVersion?: string,
  ): BedrockModuleExports {
    diag.debug(`Applying patch for ${MODULE_NAME}@${moduleVersion}`);

    if (moduleExports?.BedrockRuntimeClient) {
      // eslint-disable-next-line @typescript-eslint/no-this-alias
      const instrumentation = this;

      // Wrap the client's send method to intercept commands
      this._wrap(
        moduleExports.BedrockRuntimeClient.prototype,
        "send",
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        (original: any) => {
          // eslint-disable-next-line @typescript-eslint/no-explicit-any
          return function patchedSend(this: unknown, ...args: any[]) {
            const [command] = args;
            if (command?.constructor?.name === "InvokeModelCommand") {
              return instrumentation.handleInvokeModelCommand(
                command as InvokeModelCommand,
                original,
                this,
                args,
              );
            }

            if (
              command?.constructor?.name ===
              "InvokeModelWithResponseStreamCommand"
            ) {
              return instrumentation.handleInvokeModelWithResponseStreamCommand(
                command as InvokeModelWithResponseStreamCommand,
                original,
                this,
                args,
              );
            }

            if (command?.constructor?.name === "ConverseCommand") {
              return instrumentation.handleConverseCommand(
                command as ConverseCommand,
                original,
                this,
                args,
              );
            }

            // Pass through other commands without instrumentation
            return original.apply(this, args);
          };
        },
      );

      _isBedrockPatched = true;
    }

    return moduleExports;
  }

  /**
   * Removes the instrumentation patch from the BedrockRuntimeClient
   * Unwraps the send method and resets the patched state
   *
   * @param moduleExports The module exports from @aws-sdk/client-bedrock-runtime
   * @param moduleVersion The version of the module being unpatched
   */
  private unpatch(
    moduleExports: BedrockModuleExports,
    moduleVersion?: string,
  ): void {
    diag.debug(`Removing patch for ${MODULE_NAME}@${moduleVersion}`);

    if (moduleExports?.BedrockRuntimeClient) {
      this._unwrap(moduleExports.BedrockRuntimeClient.prototype, "send");
      _isBedrockPatched = false;
    }
  }

  /**
   * Observe either send overload without converting callback calls into promises.
   * Only the callback is replaced; command, options, trailing arguments and receiver
   * are forwarded unchanged. Response processing stays synchronous for callbacks.
   */
  private sendWithResponse<T>(
    original: (...args: unknown[]) => Promise<T> | void,
    client: unknown,
    args: unknown[],
    span: Span,
    onResponse: (response: T) => T,
  ): Promise<T> | void {
    const callbackIndex = typeof args[1] === "function"
      ? 1
      : typeof args[2] === "function" ? 2 : -1;
    const onError = (error: unknown) => {
      recordBedrockError(span, error);
      span.end();
    };
    let callbackInvoked = false;

    try {
      if (callbackIndex !== -1) {
        const callback = args[callbackIndex] as (...args: unknown[]) => unknown;
        args[callbackIndex] = function (this: unknown, ...callbackArgs: unknown[]) {
          callbackInvoked = true;
          try {
            if (callbackArgs[0]) {
              onError(callbackArgs[0]);
            } else {
              callbackArgs[1] = onResponse(callbackArgs[1] as T);
            }
          } catch (error) {
            // Instrumentation failures must not prevent delivery to the application.
            diag.warn("Error processing Bedrock callback response:", error);
            span.end();
          }
          // Keep application exceptions outside the instrumentation error boundary.
          return callback.apply(this, callbackArgs);
        };
        return original.apply(client, args);
      }

      const result = original.apply(client, args) as Promise<T>;
      return result.then(onResponse).catch((error: unknown) => {
        onError(error);
        throw error;
      });
    } catch (error) {
      // A synchronous application callback can throw through send(). Do not record
      // it as an SDK failure or invoke the callback a second time.
      if (!callbackInvoked) onError(error);
      throw error;
    }
  }

  /**
   * Handles instrumentation for synchronous InvokeModel commands
   * Creates a span, extracts request attributes, executes the command, and processes the response
   *
   * @param command The InvokeModelCommand to instrument
   * @param original The original send method from the Bedrock client
   * @param client The Bedrock client instance
   * @param args The complete send argument list
   * @returns A response promise, or void for the callback overload
   */
  private handleInvokeModelCommand(
    command: InvokeModelCommand,
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    original: any,
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    client: any,
    args: unknown[],
  ): Promise<InvokeModelResponse> | void {
    const span = this.fiTracer.startSpan("bedrock.invoke_model", {
      kind: SpanKind.INTERNAL,
    });

    const modelId = command.input.modelId;
    const system = getSystemFromModelId(modelId ?? "");
    setBasicSpanAttributes(span, system);

    const contextAttributes = getAttributesFromContext(context.active());
    span.setAttributes(contextAttributes);

    extractInvokeModelRequestAttributes({ span, command, system });

    return this.sendWithResponse(
      original,
      client,
      args,
      span,
      (response: InvokeModelResponse) => {
        extractInvokeModelResponseAttributes({ span, response, system });
        span.setStatus({ code: SpanStatusCode.OK });
        span.end();
        return response;
      },
    );
  }

  /**
   * Handles instrumentation for streaming InvokeModel commands with guaranteed stream preservation
   *
   * Observes events as the application pulls them, without a background reader.
   * Consumer cancellation and iterator control are forwarded to the original stream.
   *
   * @param command The InvokeModelWithResponseStreamCommand to instrument
   * @param original The original send method from the Bedrock client
   * @param client The Bedrock client instance
   * @param args The complete send argument list
   * @returns A response promise with preserved user stream, or void for callbacks
   */
  private handleInvokeModelWithResponseStreamCommand(
    command: InvokeModelWithResponseStreamCommand,
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    original: any,
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    client: any,
    args: unknown[],
  ) {
    const startedAt = performance.now();
    const span = this.fiTracer.startSpan("bedrock.invoke_model", {
      kind: SpanKind.INTERNAL,
    });

    const modelId = command.input.modelId;
    const system = getSystemFromModelId(modelId ?? "");
    setBasicSpanAttributes(span, system);

    const contextAttributes = getAttributesFromContext(context.active());
    span.setAttributes(contextAttributes);

    extractInvokeModelRequestAttributes({
      span,
      command: command as unknown as InvokeModelCommand,
      system,
    });

    return this.sendWithResponse(
      original,
      client,
      args,
      span,
      (response: { body: AsyncIterable<unknown> }) => {
        // Guard against missing response body - return original response
        if (!response.body) {
          span.recordException(new Error("Response body is undefined"));
          span.setStatus({
            code: SpanStatusCode.ERROR,
            message: "Response body is undefined",
          });
          span.end();
          return response;
        }

        const body = observeBedrockStream({
          stream: response.body,
          span,
          modelType: system,
          startedAt,
          onEnd: (cancelled) => {
            try {
              if (cancelled) span.addEvent("stream.cancelled");
              else span.setStatus({ code: SpanStatusCode.OK });
            } finally {
              span.end();
            }
          },
          onError: (error) => {
            try {
              recordBedrockError(span, error);
            } finally {
              span.end();
            }
          },
        });

        return { ...response, body };
      },
    );
  }

  /**
   * Handles instrumentation for Converse API commands
   * Creates a span, extracts request attributes, executes the command, and processes the response
   *
   * @param command The ConverseCommand to instrument
   * @param original The original send method from the Bedrock client
   * @param client The Bedrock client instance
   * @param args The complete send argument list
   * @returns A response promise, or void for the callback overload
   */
  private handleConverseCommand(
    command: ConverseCommand,
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    original: any,
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    client: any,
    args: unknown[],
  ): Promise<ConverseResponse> | void {
    const span = this.fiTracer.startSpan("bedrock.converse", {
      kind: SpanKind.INTERNAL,
    });

    const modelId = command.input.modelId;
    const system = getSystemFromModelId(modelId ?? "");
    setBasicSpanAttributes(span, system);

    const contextAttributes = getAttributesFromContext(context.active());
    span.setAttributes(contextAttributes);

    extractConverseRequestAttributes({ span, command });

    return this.sendWithResponse(
      original,
      client,
      args,
      span,
      (response: ConverseResponse) => {
        extractConverseResponseAttributes({ span, response });
        span.setStatus({ code: SpanStatusCode.OK });
        span.end();
        return response;
      },
    );
  }
}