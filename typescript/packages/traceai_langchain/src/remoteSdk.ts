import {
  context,
  Context,
  propagation,
  Span,
  SpanKind,
  SpanStatusCode,
  trace,
  TracerProvider,
} from "@opentelemetry/api";
import { FISpanKind, SemanticConventions } from "@traceai/fi-semantic-conventions";

/** The remote LangGraph SDK API is distinct from local LangGraph graph callbacks. */
// ESM and CommonJS load separate copies of this module. Share a weak registry
// across both builds so wrapping either the original or its proxy is idempotent
// without mutating the caller's client or retaining it after garbage collection.
const CLIENTS_KEY = Symbol.for("@traceai/langchain/langgraph-sdk/wrapped-clients/v1");
const clients: WeakMap<object, object> = Reflect.get(globalThis, CLIENTS_KEY) ?? (() => {
  const registry = new WeakMap<object, object>();
  Reflect.set(globalThis, CLIENTS_KEY, registry);
  return registry;
})();
const NAME = "@traceai/langchain/langgraph-sdk";
export interface RemoteLangGraphOptions {
  /** Optional provider; otherwise the globally registered OTel provider is used. */
  tracerProvider?: TracerProvider;
}

type RequestHook = (url: URL, init: RequestInit) => RequestInit | Promise<RequestInit>;

/**
 * Compose with the SDK's official `ClientConfig.onRequest` hook (available in
 * newer SDK releases). Install an async OTel context manager before creating
 * the client. Older releases still support the explicit client wrapper, but
 * do not offer this request hook for distributed propagation.
 */
export function langGraphTraceRequestHook(previous?: RequestHook): RequestHook {
  return async (url, init) => {
    const prepared = previous ? await previous(url, init) : init;
    const headers = new Headers(prepared.headers);
    propagation.inject(context.active(), headers, {
      set(carrier, key, value) { carrier.set(key, value); },
    });
    return { ...prepared, headers };
  };
}

function error(span: Span, cause: unknown): void {
  if (cause instanceof Error && cause.name === "AbortError") {
    span.setAttribute("langgraph.cancelled", true);
    return;
  }
  // Error messages (and stack traces) can contain customer prompts or secrets.
  const name = cause instanceof TypeError ? "TypeError" : "Error";
  span.setStatus({ code: SpanStatusCode.ERROR, message: name });
  span.setAttribute("error.type", name);
}

/**
 * Explicit, non-mutating remote client wrapper. Only `runs.wait` and
 * `runs.stream` receive CLIENT spans; server-side graph/model/tool work must be
 * separately instrumented. Re-wrapping the same client never duplicates spans.
 * Request/response bodies and stream event data are never recorded.
 */
export function wrapLangGraphClient<C extends { runs: object }>(client: C, options: RemoteLangGraphOptions = {}): C {
  const existing = clients.get(client);
  if (existing) return existing as C;
  const runs = client.runs;
  const tracer = options.tracerProvider?.getTracer(NAME) ?? trace.getTracer(NAME);

  function begin(operation: "wait" | "stream", args: unknown[], parent: Context = context.active()) {
    const [threadId, assistantId] = args;
    const attributes: Record<string, string> = {
      [SemanticConventions.FI_SPAN_KIND]: FISpanKind.CHAIN,
      "langgraph.operation": operation,
    };
    if (typeof threadId === "string") {
      attributes[SemanticConventions.SESSION_ID] = threadId;
      attributes["langgraph.thread.id"] = threadId;
    }
    if (typeof assistantId === "string") attributes["langgraph.assistant.id"] = assistantId;
    return tracer.startSpan(`LangGraph runs.${operation}`, { kind: SpanKind.CLIENT, attributes }, parent);
  }

  // Current SDK versions report the created run through this documented callback.
  // Compose rather than replace it, and never attach the input or stream contents.
  function withRunCallback(args: unknown[], span: Span): unknown[] {
    const payload = args[2];
    if (payload !== undefined && (payload === null || typeof payload !== "object" || Array.isArray(payload))) return args;
    const options = (payload ?? {}) as Record<string, unknown>;
    const previous = options.onRunCreated;
    return [args[0], args[1], {
      ...options,
      onRunCreated(metadata: unknown) {
        if (metadata && typeof metadata === "object" &&
            typeof (metadata as { run_id?: unknown }).run_id === "string") {
          span.setAttribute("langgraph.run.id", (metadata as { run_id: string }).run_id);
        }
        if (typeof previous === "function") Reflect.apply(previous, options, [metadata]);
      },
    }, ...args.slice(3)];
  }

  function signalAborted(args: unknown[]): boolean {
    const payload = args[2];
    return !!payload && typeof payload === "object" &&
      (payload as { signal?: { aborted?: boolean } }).signal?.aborted === true;
  }

  const wrappedRuns = new Proxy(runs, {
    get(target, property) {
      const value: unknown = Reflect.get(target, property, target);
      if (property === "wait" && typeof value === "function") {
        return (...args: unknown[]) => {
          const span = begin("wait", args);
          const scope = trace.setSpan(context.active(), span);
          try {
            return Promise.resolve(context.with(scope, () => Reflect.apply(value, target, withRunCallback(args, span))))
              .then((result: unknown) => { span.end(); return result; }, (cause: unknown) => {
                error(span, cause); span.end(); throw cause;
              });
          } catch (cause) {
            error(span, cause); span.end(); throw cause;
          }
        };
      }
      if (property === "stream" && typeof value === "function") {
        return (...args: unknown[]) => {
          // Capture the caller context now; execute the SDK's lazy generator
          // only when the consumer first pulls a value.
          const parent = context.active();
          return (async function* () {
            const span = begin("stream", args, parent);
            const scope = trace.setSpan(parent, span);
            let iterator: AsyncIterator<unknown> | undefined;
            let completed = false;
            let failed = false;
            try {
              iterator = Reflect.apply(value, target, withRunCallback(args, span)) as AsyncIterator<unknown>;
              while (true) {
                const next = await context.with(scope, () => iterator!.next());
                if (next.done) {
                  completed = true;
                  if (signalAborted(args)) span.setAttribute("langgraph.cancelled", true);
                  return next.value;
                }
                if (next.value && typeof next.value === "object" &&
                    (next.value as { event?: unknown }).event === "error") {
                  failed = true;
                  span.setStatus({ code: SpanStatusCode.ERROR, message: "remote stream error" });
                }
                yield next.value;
              }
            } catch (cause) {
              failed = true;
              error(span, cause);
              throw cause;
            } finally {
              try {
                if (!completed) {
                  // A consumer may stop early; close the underlying SDK stream.
                  if (iterator?.return) {
                    try {
                      await context.with(scope, () => iterator!.return!(undefined));
                    } catch (cause) {
                      // Preserve the first error if a failed stream's cleanup also fails.
                      if (!failed) { error(span, cause); throw cause; }
                    }
                  }
                  if (!failed) span.setAttribute("langgraph.stream.cancelled", true);
                }
              } finally {
                span.end();
              }
            }
          })();
        };
      }
      return typeof value === "function" ? value.bind(target) : value;
    },
  });
  const wrapped = new Proxy(client, {
    get(target, property) {
      if (property === "runs") return wrappedRuns;
      const value: unknown = Reflect.get(target, property, target);
      return typeof value === "function" ? value.bind(target) : value;
    },
  });
  clients.set(client, wrapped);
  clients.set(wrapped, wrapped);
  return wrapped;
}
