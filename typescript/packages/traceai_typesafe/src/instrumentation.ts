/**
 * Span wrapper for `TypeSafeClient.systemOne` (@typesafe-ai/sdk).
 *
 * One EVALUATOR span per public call, however many questions it asks. Each question becomes a set
 * of `typesafe.question.<name>.*` attributes, not a child span: TypeSafe answers every question in
 * one HTTP response. The span name, kind and attributes match the Python package traceAI-typesafe;
 * the span kind is written under the TypeScript `fi.span.kind` key every traceAI TypeScript
 * integration uses.
 *
 * Always recorded: provider `typesafe`, request model, response model, server host, question names
 * and types, numeric answers (noul probability, score, confidence, box edges), token usage and the
 * request id. Unless inputs are hidden: `input.value` (state and questions) and a caller's
 * Idempotency-Key. Unless outputs are hidden: the chosen `choice` label and `output.value`.
 * Never: probability maps, score legends, image bytes, the API key, stack traces, or the text of
 * client-side errors.
 *
 * Nothing here may change what the caller gets back or what is thrown.
 */
import {
  InstrumentationBase,
  InstrumentationConfig,
  InstrumentationNodeModuleDefinition,
} from "@opentelemetry/instrumentation";
import { Attributes, Context, context, Span, SpanKind, SpanStatusCode, trace } from "@opentelemetry/api";
import { isTracingSuppressed } from "@opentelemetry/core";
import { FITracer, generateTraceConfig, TraceConfigOptions } from "@traceai/fi-core";
import { FISpanKind, MimeType, SemanticConventions as S } from "@traceai/fi-semantic-conventions";
import { VERSION } from "./version";

const MODULE_NAME = "@typesafe-ai/sdk";
const PROVIDER = "typesafe";
const SPAN_NAME = "typesafe.system_one";
const QUESTION_PREFIX = "typesafe.question.";
const QUESTION_COUNT = "typesafe.question_count";
const RESPONSE_MODEL = "typesafe.response.model";
const REQUEST_ID = "typesafe.request_id";
const IDEMPOTENCY_KEY = "typesafe.idempotency_key";
const SERVER_ERROR_TYPE = "typesafe.error_type";
const RETRY_AFTER_MS = "typesafe.retry_after_ms";
const CANCELLED = "typesafe.cancelled";
const SERVER_ADDRESS = "server.address";
const HTTP_STATUS_CODE = "http.response.status_code";
const ERROR_TYPE = "error.type";

// Caps bound what one span carries; counts stay exact.
const MAX_QUESTIONS = 64;
const MAX_CAPTURED_CHARS = 32768;
const MAX_ERROR_MESSAGE_CHARS = 1024;
const BOX_EDGES = ["top", "bottom", "left", "right"] as const;

type Dict = Record<string, unknown>;
type SDKModule = Record<string, unknown>;
type SystemOne = (this: unknown, ...args: unknown[]) => Promise<unknown>;
interface ClientClass { prototype: { systemOne?: SystemOne } }

// Ownership is per prototype, not per module name: separate SDK copies can coexist.
const owners = new WeakMap<object, TypeSafeInstrumentation>();
let patchedClients = 0;
export function isPatched(): boolean {
  return patchedClients > 0;
}

const dict = (value: unknown): Dict | undefined =>
  value !== null && typeof value === "object" && !Array.isArray(value) ? (value as Dict) : undefined;
const number = (value: unknown): number | undefined =>
  typeof value === "number" && Number.isFinite(value) ? value : undefined;
const text = (value: unknown): string | undefined => (typeof value === "string" && value ? value : undefined);

function clientPrototype(module: SDKModule): { systemOne?: SystemOne } | undefined {
  // CJS exports the class on the module; an ESM namespace may also carry it under `default`.
  const holder = module.TypeSafeClient ?? dict(module.default)?.TypeSafeClient;
  return typeof holder === "function" ? (holder as unknown as ClientClass).prototype : undefined;
}

/** The key the client most likely sends; the SDK keeps its own copy private. */
function redactKey(value: string): string {
  const key = process.env.TYPESAFE_API_KEY?.trim();
  return key && key.length >= 8 ? value.split(key).join("[redacted]") : value;
}

/** Binary values are noted by type and size, never serialized as numeric bytes (like Python's sanitizer). */
function binaryNote(raw: unknown): string | undefined {
  if (raw instanceof ArrayBuffer) return `<ArrayBuffer: ${raw.byteLength} bytes>`;
  if (ArrayBuffer.isView(raw)) return `<${raw.constructor.name}: ${raw.byteLength} bytes>`;
  if (typeof Blob !== "undefined" && raw instanceof Blob) return `<Blob: ${raw.size} bytes>`;
  return undefined;
}

function dump(payload: unknown): { value: string; mime: string } | undefined {
  try {
    const json = JSON.stringify(payload, function (this: unknown, key: string, value: unknown) {
      // Buffer has a toJSON, so inspect the original value, not the converted one.
      const note = binaryNote((this as Record<string, unknown>)[key]);
      return note ?? value;
    });
    if (json === undefined) return undefined;
    return dumpResult(redactKey(json));
  } catch {
    return undefined; // circular or otherwise unserializable content is left to the SDK to report
  }
}

function dumpResult(json: string): { value: string; mime: string } | undefined {
  try {
    return json.length <= MAX_CAPTURED_CHARS
      ? { value: json, mime: MimeType.JSON }
      : { value: `${json.slice(0, MAX_CAPTURED_CHARS)}...[truncated]`, mime: MimeType.TEXT };
  } catch {
    return undefined;
  }
}

function idempotencyKey(options: unknown): string | undefined {
  const headers = dict(dict(options)?.headers);
  if (!headers) return undefined;
  for (const [name, value] of Object.entries(headers)) {
    if (name.toLowerCase() === "idempotency-key" && typeof value === "string") return value;
  }
  return undefined;
}

function serverAddress(client: unknown): string | undefined {
  const base = text(dict(client)?.baseURL);
  if (!base) return undefined;
  try {
    return new URL(base).hostname || undefined;
  } catch {
    return undefined;
  }
}

function questionType(question: unknown): string | undefined {
  return text(dict(question)?.type);
}

function requestAttributes(client: unknown, request: Dict, options: unknown, hideInputs: boolean): Attributes {
  const attributes: Attributes = {
    [S.FI_SPAN_KIND]: FISpanKind.EVALUATOR,
    [S.GEN_AI_PROVIDER_NAME]: PROVIDER,
  };
  const model = text(request.model) ?? text(dict(client)?.defaultModel);
  if (model) attributes[S.GEN_AI_REQUEST_MODEL] = model;
  const host = serverAddress(client);
  if (host) attributes[SERVER_ADDRESS] = host;
  const questions = dict(request.questions);
  if (questions) {
    const names = Object.keys(questions);
    attributes[QUESTION_COUNT] = names.length;
    for (const name of names.slice(0, MAX_QUESTIONS)) {
      const kind = questionType(questions[name]);
      if (kind) attributes[`${QUESTION_PREFIX}${name}.type`] = kind;
    }
  }
  if (!hideInputs) {
    const captured = dump({ state: request.state, questions: request.questions });
    if (captured) {
      attributes[S.INPUT_VALUE] = captured.value;
      attributes[S.INPUT_MIME_TYPE] = captured.mime;
    }
    const key = idempotencyKey(options);
    if (key) attributes[IDEMPOTENCY_KEY] = key;
  }
  return attributes;
}

function decision(answer: unknown): Dict {
  const data = dict(answer) ?? {};
  const kind = questionType(data);
  const out: Dict = { type: kind ?? null };
  if (kind === "choice") out.choice = data.choice;
  else if (kind === "score" || kind === "noul") out[kind] = number(data[kind]) ?? null;
  else if (kind === "box") {
    const box = dict(data.box) ?? {};
    out.box = Object.fromEntries(BOX_EDGES.map((edge) => [edge, number(box[edge]) ?? null]));
  }
  const confidence = number(data.confidence);
  if (confidence !== undefined) out.confidence = confidence;
  return out;
}

function answerAttributes(name: string, answer: unknown, hideOutputs: boolean): Attributes {
  const prefix = `${QUESTION_PREFIX}${name}.`;
  const data = dict(answer) ?? {};
  const kind = questionType(data);
  const attributes: Attributes = {};
  if (kind) attributes[`${prefix}type`] = kind;
  if (kind === "choice") {
    if (typeof data.choice === "string" && !hideOutputs) attributes[`${prefix}choice`] = data.choice;
  } else if (kind === "score" || kind === "noul") {
    const value = number(data[kind]);
    if (value !== undefined) attributes[`${prefix}${kind}`] = value;
  } else if (kind === "box") {
    const box = dict(data.box) ?? {};
    for (const edge of BOX_EDGES) {
      const value = number(box[edge]);
      if (value !== undefined) attributes[`${prefix}box.${edge}`] = value;
    }
  }
  // Only when the API sent one: noul answers have none and box confidence may be null.
  const confidence = number(data.confidence);
  if (confidence !== undefined) attributes[`${prefix}confidence`] = confidence;
  return attributes;
}

function responseAttributes(result: unknown, requestModel: string | undefined, hideOutputs: boolean): Attributes {
  const data = dict(result);
  const attributes: Attributes = {};
  if (!data) return attributes;
  const model = text(data.model);
  if (model) {
    attributes[S.GEN_AI_RESPONSE_MODEL] = model;
    if (model !== requestModel) attributes[RESPONSE_MODEL] = model;
  }
  const usage = dict(data.usage);
  const input = usage?.input_tokens;
  const output = usage?.output_tokens;
  const count = (v: unknown) => (typeof v === "number" && Number.isInteger(v) && v >= 0 ? v : undefined);
  if (count(input) !== undefined) attributes[S.GEN_AI_USAGE_INPUT_TOKENS] = count(input);
  if (count(output) !== undefined) attributes[S.GEN_AI_USAGE_OUTPUT_TOKENS] = count(output);
  if (count(input) !== undefined && count(output) !== undefined) {
    attributes[S.GEN_AI_USAGE_TOTAL_TOKENS] = (input as number) + (output as number);
  }
  const answers = dict(data.answers) ?? {};
  for (const name of Object.keys(answers).slice(0, MAX_QUESTIONS)) {
    Object.assign(attributes, answerAttributes(name, answers[name], hideOutputs));
  }
  return attributes;
}

function outputContent(result: unknown): Attributes {
  const answers = dict(dict(result)?.answers);
  if (!answers || !Object.keys(answers).length) return {};
  const captured = dump(
    Object.fromEntries(Object.keys(answers).slice(0, MAX_QUESTIONS).map((name) => [name, decision(answers[name])])),
  );
  return captured ? { [S.OUTPUT_VALUE]: captured.value, [S.OUTPUT_MIME_TYPE]: captured.mime } : {};
}

/** The API's own message without the leading status, for HTTP errors only. */
function serverMessage(error: Dict): string | undefined {
  let message = text(error.message);
  const status = number(error.status);
  if (message && status !== undefined && message.startsWith(`${status} `)) message = message.slice(`${status} `.length);
  return message?.trim() || undefined;
}

function className(error: unknown): string {
  return (error as { constructor?: { name?: string } })?.constructor?.name || "Error";
}

function isCancellation(error: unknown): boolean {
  const name = className(error);
  return name === "APIUserAbortError" || (error as Error | undefined)?.name === "AbortError";
}

function recordError(span: Span, error: unknown, hideInputs: boolean, hideOutputs: boolean): void {
  const data = dict(error) ?? {};
  const kind = className(error);
  const attributes: Attributes = { [ERROR_TYPE]: kind };
  const status = number(data.status);
  if (status !== undefined) attributes[HTTP_STATUS_CODE] = status;
  const requestId = text(data.requestId);
  if (requestId) attributes[REQUEST_ID] = requestId;
  const detail = dict(dict(data.body)?.detail);
  const serverType = text(detail?.error_type);
  if (serverType) attributes[SERVER_ERROR_TYPE] = serverType;
  const retryAfter = number(data.retryAfterMs);
  if (retryAfter !== undefined) attributes[RETRY_AFTER_MS] = retryAfter;
  span.setAttributes(attributes);
  let description = status !== undefined ? `${kind} (HTTP ${status})` : kind;
  const event: Attributes = { "exception.type": kind };
  // Client-side TypeSafeError text can name a local file, so only the API's own message is used,
  // and only while neither side's content is hidden (an API can echo request values).
  if (status !== undefined && !hideInputs && !hideOutputs) {
    const message = serverMessage(data)?.slice(0, MAX_ERROR_MESSAGE_CHARS);
    if (message) {
      event["exception.message"] = message;
      description = `${description}: ${message}`;
    }
  }
  span.addEvent("exception", event);
  span.setStatus({ code: SpanStatusCode.ERROR, message: description });
}

/**
 * Observe only the consumption route the caller chooses. The SDK's APIPromise is lazy: `then`
 * parses the body, `asResponse()` must not, so no handler is attached eagerly.
 */
function observePromise<T>(
  promise: Promise<T>,
  observe: (value: T) => T,
  fail: (error: unknown) => void,
  done: () => void,
  execContext: Context,
  onRequestId: (id: string) => void,
): Promise<T> {
  let observed: Promise<T> | undefined;
  let valueObserved = false;
  let value: T;
  const onValue = (result: T) => {
    if (!valueObserved) {
      value = observe(result);
      valueObserved = true;
    }
    return value;
  };
  const onError = (error: unknown): never => {
    fail(error);
    throw error;
  };
  // The request id lives on the response headers, not in the parsed data. Reading it from the
  // response promise (already buffered by the SDK) does not touch the body the parse will read.
  const readRequestId = () => {
    try {
      const asResponse = Reflect.get(promise, "asResponse", promise);
      if (typeof asResponse !== "function") return;
      (asResponse.call(promise) as Promise<{ headers?: { get(name: string): string | null } }>).then(
        (response) => {
          const id = response?.headers?.get("x-typesafe-request-id");
          if (id) onRequestId(id);
        },
        () => undefined,
      );
    } catch {
      /* the id is optional */
    }
  };
  const parsed = () =>
    (observed ??= context.with(execContext, () => {
      readRequestId();
      return promise.then(onValue, onError);
    }));
  return new Proxy(promise, {
    get(target, key) {
      if (key === "then") return (...args: Parameters<Promise<T>["then"]>) => parsed().then(...args);
      if (key === "catch") return (...args: Parameters<Promise<T>["catch"]>) => parsed().catch(...args);
      if (key === "finally") return (...args: Parameters<Promise<T>["finally"]>) => parsed().finally(...args);
      const method = Reflect.get(target, key, target);
      if (key === "map" && typeof method === "function") {
        // The derived APIPromise reads the SDK's own parse, which this observer does not see.
        // Start the observed parse (the SDK shares one parse), so the span ends on its result.
        return (...args: unknown[]) => {
          parsed().catch(() => undefined);
          return method.apply(target, args);
        };
      }
      if (key === "withResponse" && typeof method === "function") {
        return (...args: unknown[]) =>
          context
            .with(execContext, () => method.apply(target, args))
            .then((result: { data: T; requestId?: string }) => {
              if (result.requestId) onRequestId(result.requestId);
              return { ...result, data: onValue(result.data) };
            }, onError);
      }
      if (key === "asResponse" && typeof method === "function") {
        return (...args: unknown[]) =>
          context
            .with(execContext, () => method.apply(target, args))
            .then((response: unknown) => {
              done();
              return response;
            }, onError);
      }
      return typeof method === "function" && key !== "constructor" ? method.bind(target) : method;
    },
  });
}

export class TypeSafeInstrumentation extends InstrumentationBase {
  private _traceConfig?: TraceConfigOptions;
  private manualModules?: Set<SDKModule>;
  private patched?: Set<object>;
  public readonly instrumentationName = "@traceai/typesafe";

  constructor({
    instrumentationConfig,
    traceConfig,
  }: { instrumentationConfig?: InstrumentationConfig; traceConfig?: TraceConfigOptions } = {}) {
    super("@traceai/typesafe", VERSION, instrumentationConfig ?? {});
    this._traceConfig = traceConfig;
  }

  protected init() {
    return new InstrumentationNodeModuleDefinition(
      MODULE_NAME,
      [">=0.5.7 <1.0.0"],
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
    this.patched?.forEach((prototype) => this.unpatchPrototype(prototype as { systemOne?: SystemOne }));
  }

  manuallyInstrument(module: SDKModule) {
    (this.manualModules ??= new Set()).add(module);
    this.patch(module);
  }

  private patch(module: SDKModule) {
    const prototype = clientPrototype(module);
    if (!prototype || typeof prototype.systemOne !== "function" || owners.has(prototype)) return module;
    this._wrap(prototype, "systemOne", this.systemOneWrapper());
    owners.set(prototype, this);
    (this.patched ??= new Set()).add(prototype);
    patchedClients++;
    return module;
  }

  private unpatch(module: SDKModule) {
    const prototype = clientPrototype(module);
    if (prototype) this.unpatchPrototype(prototype);
  }

  private unpatchPrototype(prototype: { systemOne?: SystemOne }) {
    if (owners.get(prototype) !== this) return;
    this._unwrap(prototype, "systemOne");
    owners.delete(prototype);
    this.patched?.delete(prototype);
    patchedClients--;
  }

  private systemOneWrapper() {
    const instrumentation = this;
    return (original: SystemOne | undefined): SystemOne | undefined => {
      if (!original) return original;
      return function (this: unknown, ...args: unknown[]) {
        if (!instrumentation.isEnabled() || isTracingSuppressed(context.active())) {
          return original.apply(this, args);
        }
        let begun: ReturnType<typeof instrumentation.begin> | undefined;
        try {
          begun = instrumentation.begin(this, args);
        } catch {
          begun = undefined; // tracing must never change the call
        }
        if (!begun) return original.apply(this, args);
        const { execContext, finish, ok, requestModel, setRequestId } = begun;
        try {
          const promise = context.with(execContext, () => original.apply(this, args));
          return observePromise(
            promise,
            (result) => {
              try {
                ok(result, requestModel);
              } catch {
                finish();
              }
              return result;
            },
            (error) => finish(error),
            () => finish(),
            execContext,
            (id) => setRequestId(id),
          );
        } catch (error) {
          finish(error);
          throw error;
        }
      };
    };
  }

  private begin(client: unknown, args: unknown[]) {
    const request = dict(args[0]);
    if (!request) return undefined; // not a request object: leave the SDK to report it, unspanned
    const config = generateTraceConfig(this._traceConfig);
    const hideInputs = Boolean(config.hideInputs || config.hideInputText);
    const hideOutputs = Boolean(config.hideOutputs || config.hideOutputText);
    const attributes = requestAttributes(client, request, args[1], hideInputs);
    const requestModel = text(attributes[S.GEN_AI_REQUEST_MODEL]);
    const tracer = new FITracer({ tracer: this.tracer, traceConfig: config });
    const span = tracer.startSpan(SPAN_NAME, { kind: SpanKind.CLIENT, attributes });
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
    const finish = (error?: unknown) => {
      if (ended) return;
      try {
        if (error === undefined) span.setStatus({ code: SpanStatusCode.OK });
        else if (isCancellation(error)) {
          span.setAttribute(CANCELLED, true);
          span.setStatus({ code: SpanStatusCode.ERROR, message: "cancelled" });
        } else recordError(span, error, hideInputs, hideOutputs);
      } catch {
        /* a failed annotation must not hide the call's own result */
      }
      end();
    };
    let requestId: string | undefined;
    const setRequestId = (id: string) => {
      requestId = id;
    };
    const ok = (result: unknown, model: string | undefined) => {
      if (ended) return;
      try {
        if (requestId) span.setAttribute(REQUEST_ID, requestId);
        span.setAttributes(responseAttributes(result, model, hideOutputs));
        if (!hideOutputs) span.setAttributes(outputContent(result));
      } catch {
        /* keep the span, drop the unreadable response */
      }
      finish();
    };
    return { execContext, finish, ok, requestModel, setRequestId };
  }
}
