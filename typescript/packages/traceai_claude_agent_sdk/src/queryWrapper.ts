/**
 * Wrap the SDK's exported `query()` function.
 *
 * The wrapper calls the original `query()` with the same params object (same
 * reference, never copied or modified) and returns a proxy around the `Query`
 * it got back. The proxy yields the same message objects the SDK yielded and
 * forwards every control method (`interrupt()`, `setModel()`, ...) to the
 * original. `next`, `return`, `throw` and `Symbol.asyncIterator` are
 * intercepted to drive the span model in `spans.ts`; `close()` and
 * `Symbol.asyncDispose` end the open spans (as cancelled unless the query was
 * idle right after a result), then forward.
 */
import { Tracer, context as otelContext, diag } from "@opentelemetry/api";
import { ContentPolicy, QueryTracer, clockMs } from "./spans";
import { QueryFunctionLike, QueryParamsLike } from "./types";

const WRAPPED = Symbol.for("@traceai/claude-agent-sdk/wrapped");

export interface QueryTracingContext {
  tracer: Tracer;
  policy: ContentPolicy;
}

export function isWrappedQuery(fn: unknown): boolean {
  return typeof fn === "function" && (fn as unknown as Record<symbol, unknown>)[WRAPPED] === true;
}

/**
 * Return a function with the same signature as `query` that traces every call.
 * `getContext` is read per call so a later tracer/config change applies.
 */
export function wrapQueryFunction<Q extends QueryFunctionLike>(
  query: Q,
  getContext: () => QueryTracingContext,
): Q {
  if (typeof query !== "function") {
    throw new TypeError("@traceai/claude-agent-sdk: wrapQuery() expects the SDK query function");
  }
  if (isWrappedQuery(query)) {
    return query;
  }

  const wrapped = function tracedQuery(this: unknown, params: QueryParamsLike) {
    const startTimeMs = clockMs();
    const parentContext = otelContext.active();
    // The SDK call itself is never wrapped in try/catch: a synchronous throw
    // from query() reaches the caller unchanged.
    // eslint-disable-next-line prefer-rest-params
    const original = query.apply(this, arguments as unknown as [QueryParamsLike]);

    let queryTracer: QueryTracer | undefined;
    try {
      const { tracer, policy } = getContext();
      queryTracer = new QueryTracer({ tracer, policy, params, parentContext, startTimeMs });
    } catch (error) {
      diag.debug(`@traceai/claude-agent-sdk: tracing disabled for this query: ${error}`);
    }
    if (!queryTracer || !original || typeof original.next !== "function") {
      return original;
    }
    return proxyQuery(original, queryTracer);
  };

  Object.defineProperty(wrapped, WRAPPED, { value: true });
  Object.defineProperty(wrapped, "name", { value: query.name || "query" });
  return wrapped as unknown as Q;
}

function proxyQuery<G extends AsyncGenerator<unknown, unknown, unknown>>(
  original: G,
  queryTracer: QueryTracer,
): G {
  const safe = (fn: () => void) => {
    try {
      fn();
    } catch (error) {
      diag.debug(`@traceai/claude-agent-sdk: ${error}`);
    }
  };

  const next = async (...args: [] | [unknown]) => {
    safe(() => queryTracer.start());
    let result: IteratorResult<unknown, unknown>;
    try {
      result = await original.next(...args);
    } catch (error) {
      safe(() => queryTracer.finish({ kind: "error", error }));
      throw error;
    }
    if (result.done) {
      safe(() => queryTracer.finish({ kind: "completed" }));
    } else {
      safe(() => queryTracer.onMessage(result.value));
    }
    return result;
  };

  const ret = async (value?: unknown) => {
    try {
      return await original.return(value);
    } finally {
      safe(() => queryTracer.finish({ kind: "returned" }));
    }
  };

  const thr = async (error?: unknown) => {
    let result: IteratorResult<unknown, unknown>;
    try {
      result = await original.throw(error);
    } catch (thrown) {
      safe(() => queryTracer.finish({ kind: "error", error: thrown }));
      throw thrown;
    }
    if (result.done) {
      safe(() => queryTracer.finish({ kind: "returned" }));
    } else {
      safe(() => queryTracer.onMessage(result.value));
    }
    return result;
  };

  const proxy: G = new Proxy(original, {
    get(target, property) {
      if (property === "next") return next;
      if (property === "return") return ret;
      if (property === "throw") return thr;
      if (property === Symbol.asyncIterator) return () => proxy;
      const value = Reflect.get(target, property, target);
      if (typeof value !== "function") return value;
      // close() is the SDK's abort path (sdk.d.ts Query.close) and asyncDispose
      // runs on `await using`: neither goes through next/return/throw, so end
      // every open span before forwarding (cancelled, unless the query was idle
      // right after a result: see QueryTracer.finish).
      if (property === "close") {
        return function close(this: unknown, ...args: unknown[]) {
          safe(() => queryTracer.finish({ kind: "aborted", reason: "close" }));
          return value.apply(target, args);
        };
      }
      if (isAsyncDisposeKey(property)) {
        return function asyncDispose(this: unknown, ...args: unknown[]) {
          safe(() => queryTracer.finish({ kind: "aborted", reason: "asyncDispose" }));
          return value.apply(target, args);
        };
      }
      // backgroundTasks() answers foreground tasks with a "running in the
      // background" tool_result: keep their subagent spans open until
      // task_notification. Undo if the call rejects or matched nothing.
      if (property === "backgroundTasks") {
        return function backgroundTasks(this: unknown, ...args: unknown[]) {
          let marked: string[] = [];
          safe(() => {
            marked = queryTracer.markBackgroundRequested(typeof args[0] === "string" ? args[0] : undefined);
          });
          let result: unknown;
          try {
            result = value.apply(target, args);
          } catch (error) {
            safe(() => queryTracer.cancelBackgroundRequest(marked));
            throw error;
          }
          if (marked.length > 0 && result && typeof (result as Promise<unknown>).then === "function") {
            // A side branch: the caller still gets the original promise and its rejection.
            (result as Promise<unknown>).then(
              (ok) => {
                if (ok === false) safe(() => queryTracer.cancelBackgroundRequest(marked));
              },
              () => safe(() => queryTracer.cancelBackgroundRequest(marked)),
            );
          }
          return result;
        };
      }
      return value.bind(target);
    },
  });
  return proxy;
}

/** `Symbol.asyncDispose`, native or the `Symbol.for` polyfill the SDK installs on older Node. */
function isAsyncDisposeKey(property: string | symbol): boolean {
  if (typeof property !== "symbol") return false;
  const native = (Symbol as unknown as { asyncDispose?: symbol }).asyncDispose;
  return property === native || property === Symbol.for("Symbol.asyncDispose");
}
