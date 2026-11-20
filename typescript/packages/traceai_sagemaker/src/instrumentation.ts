/**
 * Instrumentation for Amazon SageMaker Runtime endpoint calls made through
 * `@aws-sdk/client-sagemaker-runtime` (AWS SDK for JavaScript v3).
 *
 * Span names, kind and attributes follow the Python package traceAI-sagemaker
 * (python/frameworks/sagemaker): one LLM span per `InvokeEndpoint`,
 * `InvokeEndpointAsync` or `InvokeEndpointWithResponseStream` call, named `sagemaker.<operation>`
 * with `gen_ai.provider.name` `aws.sagemaker`. Differences from the Python package:
 * - the span kind is written under the TypeScript `fi.span.kind` key;
 * - streaming spans also carry `gen_ai.server.time_to_first_token` (milliseconds);
 * - the SDK reads a blocking response body before `send` resolves, so a body read error surfaces
 *   from `send` itself and the caller's body is never replaced.
 *
 * While any hide setting is on, the response is never written as a whole object (only the
 * generated text is recorded, and only when its own direction is not hidden), no raw copy of
 * either side is recorded, and request parameters (which can hold stop strings) are left out.
 * Error text can carry request or response content, so only the error class is recorded.
 *
 * Nothing here may change what the caller gets back or what is thrown.
 */
import {
  InstrumentationBase,
  InstrumentationConfig,
  InstrumentationNodeModuleDefinition,
} from "@opentelemetry/instrumentation";
import { Attributes, context, Span, SpanKind, SpanStatusCode, trace } from "@opentelemetry/api";
import { isTracingSuppressed } from "@opentelemetry/core";
import {
  FITracer,
  generateTraceConfig,
  getAttributesFromContext,
  TraceConfigOptions,
} from "@traceai/fi-core";
import { FISpanKind, SemanticConventions as S } from "@traceai/fi-semantic-conventions";
import * as payload from "./payload";
import { StreamAccumulator, wrapEventStream } from "./stream";
import { VERSION } from "./version";

const MODULE_NAME = "@aws-sdk/client-sagemaker-runtime";
const PROVIDER = "aws.sagemaker";
const ENDPOINT = "aws.sagemaker.endpoint_name";
const COMPONENT = "aws.sagemaker.inference_component";
const TARGET_VARIANT = "aws.sagemaker.target_variant";
const INVOKED_VARIANT = "aws.sagemaker.invoked_variant";
const INFERENCE_ID = "aws.sagemaker.inference_id";
const STREAM_INCOMPLETE = "aws.sagemaker.stream.incomplete";
const REQUEST_ID = "aws.request_id";
const ERROR_TYPE = "error.type";

type Operation = "invoke_endpoint" | "invoke_endpoint_async" | "invoke_endpoint_with_response_stream";
const COMMANDS: Record<string, Operation> = {
  InvokeEndpointCommand: "invoke_endpoint",
  InvokeEndpointAsyncCommand: "invoke_endpoint_async",
  InvokeEndpointWithResponseStreamCommand: "invoke_endpoint_with_response_stream",
};

type Dict = Record<string, unknown>;
type Send = (this: unknown, ...args: unknown[]) => unknown;
type SDKModule = Record<string, unknown>;
interface ClientClass { prototype: { send?: Send } }

// Ownership is per prototype, not per module name: separate SDK copies can coexist.
const owners = new WeakMap<object, SageMakerInstrumentation>();
let patchedClients = 0;
export function isPatched(): boolean {
  return patchedClients > 0;
}

const dict = payload.dict;
const text = (value: unknown): string | undefined => (typeof value === "string" && value ? value : undefined);

function clientPrototype(module: SDKModule): { send?: Send } | undefined {
  // CJS exports the class on the module; an ESM namespace may also carry it under `default`.
  const holder = module.SageMakerRuntimeClient ?? dict(module.default)?.SageMakerRuntimeClient;
  return typeof holder === "function" ? (holder as unknown as ClientClass).prototype : undefined;
}

function setIf(span: Span, key: string, value: string | number | undefined): void {
  if (value !== undefined && value !== "") span.setAttribute(key, value);
}

function recordUsage(span: Span, usage: payload.Usage): void {
  setIf(span, S.GEN_AI_RESPONSE_MODEL, usage.model);
  setIf(span, S.GEN_AI_USAGE_INPUT_TOKENS, usage.inputTokens);
  setIf(span, S.GEN_AI_USAGE_OUTPUT_TOKENS, usage.outputTokens);
  setIf(span, S.GEN_AI_USAGE_TOTAL_TOKENS, usage.totalTokens);
}

interface Settings {
  captureInput: boolean;
  captureOutput: boolean;
  /** Any hide setting on, whichever direction. */
  anyHidden: boolean;
}

function settingsOf(options: TraceConfigOptions | undefined): Settings {
  const c = generateTraceConfig(options);
  const input = Boolean(c.hideInputs || c.hideInputText || c.hideInputMessages || c.hideInputImages);
  const output = Boolean(c.hideOutputs || c.hideOutputText || c.hideOutputMessages);
  return { captureInput: !input, captureOutput: !output, anyHidden: input || output || Boolean(c.hideEmbeddingVectors) };
}

export class SageMakerInstrumentation extends InstrumentationBase {
  private _traceConfig?: TraceConfigOptions;
  private manualModules?: Set<SDKModule>;
  private patched?: Set<object>;
  public readonly instrumentationName = "@traceai/sagemaker";

  constructor({
    instrumentationConfig,
    traceConfig,
  }: { instrumentationConfig?: InstrumentationConfig; traceConfig?: TraceConfigOptions } = {}) {
    super("@traceai/sagemaker", VERSION, instrumentationConfig ?? {});
    this._traceConfig = traceConfig;
  }

  protected init() {
    return new InstrumentationNodeModuleDefinition(
      MODULE_NAME,
      [">=3.693.0 <4.0.0"],
      this.patch.bind(this),
      this.unpatch.bind(this),
    );
  }

  public override enable(): void {
    super.enable();
    this.manualModules?.forEach((module) => this.patch(module));
  }

  public override disable(): void {
    super.disable();
    // Manual instrumentation also needs cleanup, including a pre-imported ESM namespace.
    this.patched?.forEach((prototype) => this.unpatchPrototype(prototype as { send?: Send }));
  }

  manuallyInstrument(module: SDKModule) {
    (this.manualModules ??= new Set()).add(module);
    this.patch(module);
  }

  private patch(module: SDKModule) {
    const prototype = clientPrototype(module);
    if (!prototype || typeof prototype.send !== "function" || owners.has(prototype)) return module;
    this._wrap(prototype, "send", this.sendWrapper());
    owners.set(prototype, this);
    (this.patched ??= new Set()).add(prototype);
    patchedClients++;
    return module;
  }

  private unpatch(module: SDKModule) {
    const prototype = clientPrototype(module);
    if (prototype) this.unpatchPrototype(prototype);
  }

  private unpatchPrototype(prototype: { send?: Send }) {
    if (owners.get(prototype) !== this) return;
    this._unwrap(prototype, "send");
    owners.delete(prototype);
    this.patched?.delete(prototype);
    patchedClients--;
  }

  private sendWrapper() {
    // eslint-disable-next-line @typescript-eslint/no-this-alias
    const instrumentation = this;
    return (original: Send | undefined): Send | undefined => {
      if (!original) return original;
      return function patchedSend(this: unknown, ...args: unknown[]) {
        const command = args[0] as { constructor?: { name?: string } } | undefined;
        const operation = COMMANDS[command?.constructor?.name ?? ""];
        if (!operation || !instrumentation.isEnabled() || isTracingSuppressed(context.active())) {
          return original.apply(this, args);
        }
        let call: ReturnType<typeof instrumentation.begin> | undefined;
        try {
          call = instrumentation.begin(operation, command as unknown as { input?: Dict });
        } catch {
          call = undefined; // tracing must never change the call
        }
        if (!call) return original.apply(this, args);
        return instrumentation.invoke(call, original, this, args);
      };
    };
  }

  /** Run the SDK call with either send overload, observing the outcome. */
  private invoke(call: Call, original: Send, receiver: unknown, args: unknown[]): unknown {
    const callbackIndex = typeof args[1] === "function" ? 1 : typeof args[2] === "function" ? 2 : -1;
    let callbackInvoked = false;
    try {
      if (callbackIndex !== -1) {
        const callback = args[callbackIndex] as (...a: unknown[]) => unknown;
        args[callbackIndex] = function (this: unknown, ...cbArgs: unknown[]) {
          callbackInvoked = true;
          if (cbArgs[0]) call.fail(cbArgs[0]);
          else cbArgs[1] = call.succeed(cbArgs[1]);
          // Keep application exceptions outside the instrumentation error boundary.
          return callback.apply(this, cbArgs);
        };
        return context.with(call.execContext, () => original.apply(receiver, args));
      }
      const result = context.with(call.execContext, () => original.apply(receiver, args)) as Promise<unknown>;
      return result.then(
        (output) => call.succeed(output),
        (error: unknown) => {
          call.fail(error);
          throw error;
        },
      );
    } catch (error) {
      // A synchronous application callback can throw through send(); it is not an SDK failure.
      if (!callbackInvoked) call.fail(error);
      throw error;
    }
  }

  private begin(operation: Operation, command: { input?: Dict }): Call {
    const input = dict(command?.input) ?? {};
    const settings = settingsOf(this._traceConfig);
    const attributes: Attributes = {
      [S.FI_SPAN_KIND]: FISpanKind.LLM,
      [S.GEN_AI_PROVIDER_NAME]: PROVIDER,
      [S.GEN_AI_OPERATION_NAME]: operation,
    };
    const endpoint = text(input.EndpointName);
    if (endpoint) {
      attributes[S.GEN_AI_REQUEST_MODEL] = endpoint;
      attributes[ENDPOINT] = endpoint;
    }
    const component = text(input.InferenceComponentName);
    if (component) attributes[COMPONENT] = component;
    const variant = text(input.TargetVariant);
    if (variant) attributes[TARGET_VARIANT] = variant;
    const inferenceId = text(input.InferenceId);
    if (inferenceId) attributes[INFERENCE_ID] = inferenceId;
    const tracer = new FITracer({ tracer: this.tracer, traceConfig: generateTraceConfig(this._traceConfig) });
    const span = tracer.startSpan(`sagemaker.${operation}`, { kind: SpanKind.CLIENT, attributes });
    const started = performance.now();
    try {
      span.setAttributes(getAttributesFromContext(context.active()));
      this.recordRequest(span, operation, input, settings);
    } catch {
      /* a failed annotation must not change the call */
    }
    const execContext = trace.setSpan(context.active(), span);
    let ended = false;
    const end = () => {
      if (ended) return;
      ended = true;
      try {
        span.end();
      } catch {
        /* nothing to recover */
      }
    };
    const fail = (error: unknown) => {
      if (ended) return;
      try {
        // Error text can carry request or response content: record the class only.
        const name = error instanceof Error ? error.name || error.constructor?.name : undefined;
        span.setAttribute(ERROR_TYPE, name || "Error");
        span.setStatus({ code: SpanStatusCode.ERROR });
      } catch {
        /* keep the call's own error */
      }
      end();
    };
    const succeed = (output: unknown): unknown => {
      if (ended) return output;
      try {
        const out = dict(output);
        if (out) this.recordCommon(span, out);
        if (operation === "invoke_endpoint" && out) this.recordInvoke(span, out, settings);
        else if (operation === "invoke_endpoint_async" && out) setIf(span, INFERENCE_ID, text(out.InferenceId));
        else if (operation === "invoke_endpoint_with_response_stream" && out) {
          const wrapped = this.wrapStream(span, out, settings, started, end);
          if (wrapped) return wrapped; // the stream now owns the span's end
        }
        span.setStatus({ code: SpanStatusCode.OK });
      } catch {
        /* keep the span, drop the unreadable response */
      }
      end();
      return output;
    };
    return { execContext, fail, succeed };
  }

  private recordRequest(span: Span, operation: Operation, input: Dict, settings: Settings): void {
    // The payload of an async call lives in S3 (InputLocation); there is no body to capture.
    if (operation === "invoke_endpoint_async") return;
    const captured = payload.captureBody(input.Body, input.ContentType);
    if (!captured) return;
    if (settings.captureInput) {
      span.setAttribute(S.INPUT_VALUE, captured.value);
      span.setAttribute(S.INPUT_MIME_TYPE, captured.mimeType);
    }
    // Parameters can hold stop strings and free-form fields: left out while any input setting is on.
    if (!settings.captureInput) return;
    const params = payload.requestParameters(captured.parsed);
    if (params) span.setAttribute(S.GEN_AI_REQUEST_PARAMETERS, payload.clip(JSON.stringify(params)));
  }

  private recordCommon(span: Span, output: Dict): void {
    setIf(span, INVOKED_VARIANT, text(output.InvokedProductionVariant));
    setIf(span, REQUEST_ID, text(dict(output.$metadata)?.requestId));
  }

  /** The SDK reads a blocking response body before `send` resolves; it is only read here, never replaced. */
  private recordInvoke(span: Span, output: Dict, settings: Settings): void {
    const body = output.Body;
    const contentType = output.ContentType;
    if (body === undefined || body === null) return;
    const captured = payload.captureBody(body, contentType);
    if (!captured) return;
    if (!captured.isBody) {
      // Binary or unreadable output is only noted.
      if (settings.captureOutput) {
        span.setAttribute(S.OUTPUT_VALUE, captured.value);
        span.setAttribute(S.OUTPUT_MIME_TYPE, captured.mimeType);
      }
      return;
    }
    recordUsage(span, payload.usageOf(captured.parsed));
    if (!settings.captureOutput) return;
    const generated = payload.textOf(captured.parsed);
    if (generated) {
      span.setAttribute(S.OUTPUT_VALUE, payload.clip(generated));
      span.setAttribute(S.OUTPUT_MIME_TYPE, "text/plain");
    } else if (!settings.anyHidden || (captured.parsed === undefined && payload.classify(contentType) !== "json")) {
      // The whole body is written only when no hide setting is on, or when it is plain text rather
      // than a JSON document that could carry request-side fields. A JSON response that did not parse
      // (jsonlines, malformed, over the parse limit) is not plain text: it is left out.
      span.setAttribute(S.OUTPUT_VALUE, captured.value);
      span.setAttribute(S.OUTPUT_MIME_TYPE, captured.mimeType);
    }
  }

  private wrapStream(span: Span, output: Dict, settings: Settings, started: number, end: () => void): Dict | undefined {
    const body = output.Body as AsyncIterable<unknown> | undefined;
    if (!body || typeof body[Symbol.asyncIterator] !== "function") return undefined;
    const acc = new StreamAccumulator(output.ContentType, settings.captureOutput);
    let firstChunk: number | undefined;
    const wrapped = wrapEventStream(body, acc, {
      onFirstChunk: () => {
        firstChunk = performance.now() - started;
      },
      onEnd: ({ acc: done, complete, error }) => {
        try {
          if (firstChunk !== undefined) span.setAttribute(S.GEN_AI_SERVER_TIME_TO_FIRST_TOKEN, Math.round(firstChunk));
          if (error !== undefined) {
            const name = error instanceof Error ? error.name || error.constructor?.name : undefined;
            span.setAttribute(ERROR_TYPE, name || "Error");
            span.setStatus({ code: SpanStatusCode.ERROR });
          } else {
            if (!complete) span.setAttribute(STREAM_INCOMPLETE, true);
            recordUsage(span, done.usage);
            const generated = done.output();
            if (generated) {
              span.setAttribute(S.OUTPUT_VALUE, generated);
              span.setAttribute(S.OUTPUT_MIME_TYPE, "text/plain");
            }
            span.setStatus({ code: SpanStatusCode.OK });
          }
        } catch {
          /* keep the stream and the span's end */
        } finally {
          end();
        }
      },
    });
    output.Body = wrapped;
    return output;
  }
}

interface Call {
  execContext: ReturnType<typeof trace.setSpan>;
  fail: (error: unknown) => void;
  succeed: (output: unknown) => unknown;
}
