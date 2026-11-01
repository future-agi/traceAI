import { context, diag, ROOT_CONTEXT, Span, SpanKind, SpanStatusCode, trace, Tracer, TracerProvider } from "@opentelemetry/api";
import { W3CTraceContextPropagator } from "@opentelemetry/core";
import { FISpanKind, SemanticConventions } from "@traceai/fi-semantic-conventions";
import { FISpan } from "./trace/trace-config/FISpan";
import { generateTraceConfig } from "./trace/trace-config/traceConfig";

/** Structural subset of AWS Context; no AWS runtime dependency is required. */
export interface LambdaInvocationContext {
  awsRequestId?: string;
  functionName?: string;
  getRemainingTimeInMillis(): number;
  /** Callback tracing requires this to be false before invocation and through completion. */
  callbackWaitsForEmptyEventLoop?: boolean;
}

export interface LambdaTracingOptions {
  /** Application-owned provider. Undefined disables this wrapper. Never shut down per invocation. */
  provider?: TracerProvider & { forceFlush(): Promise<void> };
  /** Maximum asynchronous flush wait. Default 2000 ms; this does not cancel export. */
  maxFlushWaitMillis?: number;
  /** Time reserved for returning to the runtime. Default 250 ms. */
  remainingTimeReserveMillis?: number;
}

export type LambdaCallback<Result> = (error?: Error | string | null, result?: Result) => void;

type PendingFlush = { waiters: Set<() => void> };
// Shared across wrappers using the same provider, without retaining dead providers.
const pendingFlushes = new WeakMap<object, PendingFlush>();
const wrappedHandlers = new WeakSet<object>();
const propagator = new W3CTraceContextPropagator();

function diagnostic(message: string): void {
  // Never log exception objects, event data or configuration values, even in debug mode.
  try { diag.debug(`traceLambdaHandler: ${message}`); } catch { /* A custom logger can fail too. */ }
}

function parentContext(event: unknown) {
  if (!event || typeof event !== "object") return ROOT_CONTEXT;
  const carrier = event as Record<string, unknown>;
  const request = carrier.requestContext as Record<string, unknown> | undefined;
  // HTTP events only. Never select one queue record as a batch's parent.
  if (!request || !(typeof carrier.httpMethod === "string" || request.http || request.elb)) return ROOT_CONTEXT;
  const values: string[] = [];
  for (const source of [carrier.headers, carrier.multiValueHeaders]) {
    if (!source || typeof source !== "object") continue;
    for (const key of Object.keys(source)) {
      if (key.toLowerCase() !== "traceparent") continue;
      const value: unknown = (source as Record<string, unknown>)[key];
      if (typeof value === "string") values.push(value);
      else if (Array.isArray(value)) {
        for (const item of value) if (typeof item === "string") values.push(item);
      }
    }
  }
  // An identical single/multivalue copy is common in API Gateway v1. Reject ambiguity.
  if (!values.length || values.some(value => value !== values[0]) || values[0].length > 512) return ROOT_CONTEXT;
  return propagator.extract(ROOT_CONTEXT, { traceparent: values[0] }, {
    keys: () => ["traceparent"],
    get: (headers, key) => key === "traceparent" ? headers.traceparent : undefined,
  });
}

function errorType(error: unknown): string {
  // Do not trust error.name (or a thrown string) to be metadata rather than user data.
  if (error instanceof TypeError) return "TypeError";
  if (error instanceof RangeError) return "RangeError";
  if (error instanceof SyntaxError) return "SyntaxError";
  if (error instanceof Error) return "Error";
  return "_OTHER";
}

function metadata(value: unknown): string | undefined {
  return typeof value === "string" && value.length <= 512 ? value : undefined;
}

function flushBudget(options: LambdaTracingOptions, invocation: LambdaInvocationContext): number {
  try {
    const max = options.maxFlushWaitMillis ?? 2000;
    const reserve = options.remainingTimeReserveMillis ?? 250;
    const remaining = invocation.getRemainingTimeInMillis();
    if (![max, reserve, remaining].every(value => typeof value === "number" && Number.isFinite(value)) || max < 0 || reserve < 0) return 0;
    return Math.max(0, Math.min(max, remaining - reserve, 2 ** 31 - 1));
  } catch {
    return 0;
  }
}

async function flush(options: LambdaTracingOptions, invocation: LambdaInvocationContext): Promise<void> {
  const provider = options.provider;
  const budget = flushBudget(options, invocation);
  if (!provider || budget <= 0) return;
  let pending = pendingFlushes.get(provider);
  if (!pending) {
    pending = { waiters: new Set() };
    pendingFlushes.set(provider, pending);
    const state = pending;
    const settled = () => {
      pendingFlushes.delete(provider);
      for (const done of state.waiters) done();
    };
    // Both synchronous throws and rejected flushes are isolated. One continuation per
    // flush, not per invocation: a permanently stalled exporter cannot retain waiters.
    void Promise.resolve().then(() => provider.forceFlush()).then(settled, () => {
      diagnostic("provider flush failed");
      settled();
    });
  }
  const state = pending;
  await new Promise<void>(resolve => {
    const done = () => {
      clearTimeout(timer);
      state.waiters.delete(done);
      resolve();
    };
    const timer = setTimeout(done, budget);
    state.waiters.add(done);
  });
}

/**
 * Trace an explicit Lambda handler, with metadata-only invocation spans and bounded
 * best-effort flushing. Use instead of, never alongside, upstream/ADOT invocation
 * instrumentation. Requires an application-installed async OTel context manager.
 * Response-streaming handlers are returned unchanged (not instrumented).
 */
export function traceLambdaHandler<Event, Result, C extends LambdaInvocationContext>(
  handler: (event: Event, invocation: C) => Result | Promise<Result>,
  options: LambdaTracingOptions & { completion?: "promise" },
): (event: Event, invocation: C) => Promise<Result>;
/**
 * Accept AWS Handler-typed async functions (whose declared signature also requires
 * a callback). Promise completion observes the return value, not the callback;
 * callback implementations must explicitly select completion: "callback".
 */
export function traceLambdaHandler<Event, Result, C extends LambdaInvocationContext>(
  handler: (event: Event, invocation: C, callback: LambdaCallback<Result>) => void | Promise<Result>,
  options: LambdaTracingOptions & { completion?: "promise" },
): (event: Event, invocation: C) => Promise<Result>;
/**
 * Callback completion is supported only on the AWS Node 22 runtime. Set
 * context.callbackWaitsForEmptyEventLoop = false before calling the wrapper and
 * keep it false through completion. Otherwise the original handler runs without
 * invocation tracing or flushing. The wrapper never changes this flag itself.
 */
export function traceLambdaHandler<Event, Result, C extends LambdaInvocationContext>(
  handler: (event: Event, invocation: C, callback: LambdaCallback<Result>) => void,
  options: LambdaTracingOptions & { completion: "callback" },
): (event: Event, invocation: C, callback: LambdaCallback<Result>) => void;
export function traceLambdaHandler<Event, Result, C extends LambdaInvocationContext>(
  handler: (event: Event, invocation: C, callback: LambdaCallback<Result>) => unknown,
  options: LambdaTracingOptions & { completion?: "promise" | "callback" },
): (event: Event, invocation: C, callback: LambdaCallback<Result>) => unknown {
  if (wrappedHandlers.has(handler) || Reflect.get(handler, Symbol.for("aws.lambda.runtime.handler.streaming")) === "response") return handler;
  let firstInvocation = true;
  let tracer: Tracer | undefined;
  // A deliberately stricter root policy: environment flags cannot opt in to error content.
  const config = generateTraceConfig({ hideInputs: true, hideOutputs: true, hideErrorDetails: true });
  try {
    tracer = options.provider?.getTracer("@traceai/fi-core/aws-lambda");
  } catch { diagnostic("provider initialization failed"); }

  async function invoke(this: unknown, event: Event, invocation: C): Promise<Result> {
    const cold = firstInvocation;
    firstInvocation = false;
    let span: Span | undefined;
    let active = ROOT_CONTEXT;
    try {
      const parent = parentContext(event);
      if (tracer) span = new FISpan({ config, span: tracer.startSpan("aws.lambda.invoke", { kind: SpanKind.SERVER, attributes: {
        [SemanticConventions.FI_SPAN_KIND]: FISpanKind.CHAIN,
        "faas.coldstart": cold,
        "faas.invocation_id": metadata(invocation.awsRequestId),
        "faas.name": metadata(invocation.functionName),
      } }, parent) });
      if (span) active = trace.setSpan(parent, span);
    } catch { diagnostic("invocation span creation failed"); }

    const run = () => new Promise<Result>((resolve, reject) => {
      if (options.completion === "callback") {
        // Promise settlement preserves the first callback outcome (including sync throw).
        handler.call(this, event, invocation, (error, result) => {
          if (error != null) reject(error);
          else resolve(result as Result);
        });
      } else {
        resolve(handler.call(this, event, invocation, () => {}) as Result | Promise<Result>);
      }
    });
    let outcome: Promise<Result> | undefined;
    try {
      // A broken context manager must not run application code twice.
      context.with(active, () => { outcome = run(); });
    } catch { diagnostic("context activation failed"); }
    if (!outcome) outcome = run();
    try {
      return await outcome;
    } catch (error) {
      try {
        span?.setAttribute("error.type", errorType(error));
        span?.setStatus({ code: SpanStatusCode.ERROR });
      } catch { diagnostic("error metadata recording failed"); }
      throw error;
    } finally {
      try { span?.end(); } catch { diagnostic("invocation span end failed"); }
      try { await flush(options, invocation); } catch { diagnostic("flush waiting failed"); }
    }
  }

  const wrapped = options.completion === "callback"
    ? function(this: unknown, event: Event, invocation: C, callback: LambdaCallback<Result>) {
      // A bounded Promise wait cannot cancel the export socket. With AWS's default
      // event-loop waiting that socket would still delay a successful response.
      // Do not alter application semantics or even start a span without opt-in.
      if (invocation.callbackWaitsForEmptyEventLoop !== false) {
        firstInvocation = false;
        diagnostic("callback tracing skipped: callbackWaitsForEmptyEventLoop must be false");
        return handler.call(this, event, invocation, callback);
      }
      void invoke.call(this, event, invocation).then(
        result => callback(null, result),
        error => callback(error),
      );
    }
    // AWS classifies the exported function, not its returned Promise. Keep this
    // natively async (including CJS build output), or an unsettled invocation can
    // become a successful null response when the event loop empties.
    : async function(this: unknown, event: Event, invocation: C) { return invoke.call(this, event, invocation); };
  wrappedHandlers.add(wrapped);
  return wrapped;
}
