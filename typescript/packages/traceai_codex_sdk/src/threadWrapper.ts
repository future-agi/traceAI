/**
 * Patch `Thread` so every turn is traced.
 *
 * `Thread.run()` and `Thread.runStreamed()` both drive the private async
 * generator `runStreamedInternal(input, turnOptions)` (every stable SDK
 * release from 0.44.0 to 0.162.1), so wrapping that one method traces both
 * entry points with real per-event timing. The wrapper yields the SDK's own
 * event objects unchanged and forwards `return()` / `throw()`, so `run()`
 * keeps its exact behaviour (including `break` on `turn.failed`).
 *
 * If a future SDK drops that method, the wrapper falls back to the public
 * `runStreamed()` and logs once that `run()` may be untraced.
 */
import { Tracer, context as otelContext, diag } from "@opentelemetry/api";
import { ContentPolicy, TurnTracer, clockMs } from "./spans";
import { InputLike, ThreadEventLike, ThreadLike, TurnOptionsLike } from "./types";

const PATCHED = Symbol.for("@traceai/codex-sdk/patched");
const LOG = "@traceai/codex-sdk";

export interface TurnTracingContext {
  tracer: Tracer;
  policy: ContentPolicy;
}

type GetContext = () => TurnTracingContext | undefined;

interface PatchRecord {
  target: ThreadLike;
  method: "runStreamedInternal" | "runStreamed";
  original: unknown;
  hadOwn: boolean;
}

export function isPatched(target: unknown): boolean {
  return !!target && (target as Record<symbol, unknown>)[PATCHED] === true;
}

/**
 * Patch a Thread prototype or a single Thread instance. Returns an undo
 * function, or `undefined` when the target was already patched or has no
 * method to wrap.
 */
export function patchThread(target: ThreadLike, getContext: GetContext): (() => void) | undefined {
  if (!target || typeof target !== "object" || isPatched(target)) return undefined;

  let record: PatchRecord | undefined;
  if (typeof target.runStreamedInternal === "function") {
    const original = target.runStreamedInternal;
    record = { target, method: "runStreamedInternal", original, hadOwn: Object.prototype.hasOwnProperty.call(target, "runStreamedInternal") };
    const traced = function tracedRunStreamedInternal(this: ThreadLike, input: InputLike, turnOptions?: TurnOptionsLike) {
      const startTimeMs = clockMs();
      const parentContext = otelContext.active();
      // eslint-disable-next-line prefer-rest-params
      const generator = original.apply(this, arguments as unknown as [InputLike, TurnOptionsLike?]);
      return traceEvents(generator, this, input, turnOptions, parentContext, startTimeMs, getContext);
    };
    defineMethod(target, "runStreamedInternal", traced);
  } else if (typeof target.runStreamed === "function") {
    const original = target.runStreamed;
    record = { target, method: "runStreamed", original, hadOwn: Object.prototype.hasOwnProperty.call(target, "runStreamed") };
    diag.warn(`${LOG}: Thread.runStreamedInternal not found; tracing runStreamed() only, run() may be untraced`);
    const traced = async function tracedRunStreamed(this: ThreadLike, input: InputLike, turnOptions?: TurnOptionsLike) {
      const startTimeMs = clockMs();
      const parentContext = otelContext.active();
      // eslint-disable-next-line prefer-rest-params
      const result = await original.apply(this, arguments as unknown as [InputLike, TurnOptionsLike?]);
      if (!result || typeof result !== "object" || !result.events) return result;
      return {
        ...result,
        events: traceEvents(result.events, this, input, turnOptions, parentContext, startTimeMs, getContext),
      };
    };
    defineMethod(target, "runStreamed", traced);
  } else {
    return undefined;
  }

  Object.defineProperty(target, PATCHED, { value: true, configurable: true });
  const done = record;
  return () => {
    try {
      if (done.hadOwn) defineMethod(done.target, done.method, done.original);
      else delete (done.target as Record<string, unknown>)[done.method];
      delete (done.target as Record<symbol, unknown>)[PATCHED];
    } catch (error) {
      diag.debug(`${LOG}: unpatch failed: ${error}`);
    }
  };
}

function defineMethod(target: object, name: string, value: unknown): void {
  Object.defineProperty(target, name, { value, writable: true, configurable: true, enumerable: false });
}

/**
 * Return a generator that yields exactly what `source` yields and drives a
 * `TurnTracer`. A tracing failure disables tracing for that turn only.
 */
export function traceEvents(
  source: AsyncGenerator<ThreadEventLike>,
  thread: ThreadLike,
  input: InputLike,
  turnOptions: TurnOptionsLike | undefined,
  parentContext: ReturnType<typeof otelContext.active>,
  startTimeMs: number,
  getContext: GetContext,
): AsyncGenerator<ThreadEventLike> {
  let turnTracer: TurnTracer | undefined;
  try {
    const ctx = getContext();
    if (ctx) {
      turnTracer = new TurnTracer({
        tracer: ctx.tracer,
        policy: ctx.policy,
        parentContext,
        input,
        turnOptions,
        threadOptions: readThreadOptions(thread),
        threadId: readThreadId(thread),
        startTimeMs,
      });
    }
  } catch (error) {
    diag.debug(`${LOG}: tracing disabled for this turn: ${error}`);
  }
  if (!turnTracer || !source || typeof source.next !== "function") return source;
  const tracer = turnTracer;

  const safe = (fn: () => void) => {
    try {
      fn();
    } catch (error) {
      diag.debug(`${LOG}: ${error}`);
    }
  };

  const proxy: AsyncGenerator<ThreadEventLike> = {
    async next(...args: [] | [unknown]) {
      safe(() => tracer.start());
      let result: IteratorResult<ThreadEventLike>;
      try {
        result = await source.next(...(args as []));
      } catch (error) {
        safe(() => tracer.finish({ kind: "error", error }));
        throw error;
      }
      if (result.done) safe(() => tracer.finish({ kind: "completed" }));
      else safe(() => tracer.onEvent(result.value));
      return result;
    },
    async return(value?: unknown) {
      try {
        return await source.return(value as never);
      } finally {
        safe(() => tracer.finish({ kind: "returned" }));
      }
    },
    async throw(error?: unknown) {
      let result: IteratorResult<ThreadEventLike>;
      try {
        result = await source.throw(error);
      } catch (thrown) {
        safe(() => tracer.finish({ kind: "error", error: thrown }));
        throw thrown;
      }
      if (result.done) safe(() => tracer.finish({ kind: "returned" }));
      else safe(() => tracer.onEvent(result.value));
      return result;
    },
    [Symbol.asyncIterator]() {
      return proxy;
    },
  } as AsyncGenerator<ThreadEventLike>;
  const asyncDispose = (Symbol as unknown as { asyncDispose?: symbol }).asyncDispose;
  if (asyncDispose) {
    (proxy as unknown as Record<symbol, unknown>)[asyncDispose] = async () => {
      await proxy.return(undefined);
    };
  }
  return proxy;
}

function readThreadId(thread: ThreadLike): string | null {
  try {
    const id = thread?.id;
    return typeof id === "string" && id ? id : null;
  } catch {
    return null;
  }
}

function readThreadOptions(thread: ThreadLike): ThreadLike["_threadOptions"] {
  try {
    const options = thread?._threadOptions;
    return options && typeof options === "object" ? options : undefined;
  } catch {
    return undefined;
  }
}
