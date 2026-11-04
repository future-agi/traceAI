import { diag } from "@opentelemetry/api";
import { consumeCallback } from "@langchain/core/callbacks/promises";

type CancelRun = () => Promise<void>;
type StreamScope = {
  runs: Set<CancelRun>;
  firstChunks: Set<() => void>;
  children: Set<StreamScope>;
  parent?: StreamScope;
  cancelled: boolean;
  cancel: (reason: unknown) => void;
  close: () => Promise<unknown>;
};
const streams = new Map<string, StreamScope>();

export function registerStreamRun(
  runId: string, cancel: CancelRun, parentRunId?: string, firstChunk?: () => void,
): () => void {
  const scope = streams.get(runId);
  if (scope != null) {
    scope.runs.add(cancel);
    if (firstChunk != null) scope.firstChunks.add(firstChunk);
    // Link actual callback run IDs, not ambient context or every active run.
    // Returning one chain must never finalize an unrelated concurrent chain.
    scope.parent = parentRunId == null ? undefined : streams.get(parentRunId);
    scope.parent?.children.add(scope);
    if (scope.parent?.cancelled) scope.cancel(new Error("Parent stream closed"));
  }
  return () => {
    scope?.runs.delete(cancel);
    if (firstChunk != null) scope?.firstChunks.delete(firstChunk);
  };
}

export function isStreamCancelled(runId: string): boolean {
  return streams.get(runId)?.cancelled === true;
}

/**
 * An iterator facade, rather than an async generator: return() must abort a
 * pending provider read immediately, not queue behind its next(). Runnable
 * wrappers leave options.signal untouched; their registered child controllers
 * supply cancellation without changing upstream per-model timeout semantics.
 */
export function wrapModelStream<T>(
  create: (signal: AbortSignal) => AsyncGenerator<T>,
  runId: string,
  signal?: AbortSignal,
  timeout?: number,
): AsyncGenerator<T> {
  const controller = new AbortController();
  const scope: StreamScope = {
    runs: new Set(), firstChunks: new Set(), children: new Set(), cancelled: false,
    cancel: (reason) => cancel(reason),
    close: () => wrapped.return(undefined),
  };
  let iterator: AsyncGenerator<T> | undefined;
  let timer: ReturnType<typeof setTimeout> | undefined;
  let completion: Promise<void> | undefined;
  let completed = false;
  let deadlineError: DOMException | undefined;

  const finish = (success: boolean) => {
    // next(), return(), and the abort listener may all finish the same iterator.
    return completion ??= (async () => {
      signal?.removeEventListener("abort", onAbort);
      clearTimeout(timer);
      completed = true;
      try {
        if (!success) {
          const results = await Promise.allSettled([...scope.children].map((child) => child.close()));
          results.push(...await Promise.allSettled([...scope.runs].map((cancelRun) => cancelRun())));
          if (results.some((result) => result.status === "rejected")) {
            diag.warn("Failed to finalize a LangChain stream");
          }
        }
      } catch {
        // Instrumentation cleanup must not replace an application's error.
        diag.warn("Failed to finalize a LangChain stream");
      } finally {
        if (streams.get(runId) === scope) streams.delete(runId);
        scope.parent?.children.delete(scope);
        scope.children.clear();
        scope.runs.clear();
        scope.firstChunks.clear();
      }
    })();
  };
  const cancel = (reason: unknown) => {
    if (completed) return;
    scope.cancelled = true;
    controller.abort(reason);
    for (const child of scope.children) child.cancel(reason);
  };
  const getIterator = () => {
    if (iterator == null) {
      // No registry entry, listener, or timer for an iterator never consumed.
      streams.set(runId, scope);
      signal?.addEventListener("abort", onAbort, { once: true });
      // Core 0.2.0 only applies timeout when no caller signal exists. Adding our
      // controller would otherwise suppress that deadline. Later cores still
      // compose timeout + a supplied signal themselves; leave that path alone.
      if (!signal && timeout) {
        if (!Number.isInteger(timeout) || timeout < 0 || timeout > 0xffffffff) {
          throw new RangeError("timeout must be an integer between 0 and 4294967295");
        }
        timer = setTimeout(() => {
          deadlineError = new DOMException("The operation was aborted due to timeout", "TimeoutError");
          cancel(deadlineError);
          closeInBackground("Failed to close a timed out LangChain stream");
        }, timeout);
        timer.unref?.();
      }
      iterator = create(controller.signal);
    }
    return iterator;
  };
  const wrapped: AsyncGenerator<T> = {
    async next(...args) {
      try {
        signal?.throwIfAborted();
        if (deadlineError != null) throw deadlineError;
        if (completed) return { done: true, value: undefined };
        controller.signal.throwIfAborted();
        const result = await getIterator().next(...args);
        controller.signal.throwIfAborted();
        if (result.done) await finish(!scope.cancelled);
        else {
          // Some providers call handleLLMNewToken only after yielding. Observe
          // the first delivered chunk too, including a consumer's immediate break.
          for (const firstChunk of scope.firstChunks) firstChunk();
          scope.firstChunks.clear();
        }
        return result;
      } catch (error) {
        await finish(false);
        throw error;
      }
    },
    async return(value) {
      cancel(signal?.reason ?? new Error("Stream closed before completion"));
      try {
        return iterator == null ? { done: true, value } : await iterator.return(value);
      } finally {
        await finish(false);
      }
    },
    async throw(error) {
      cancel(error);
      try {
        if (iterator == null) throw error;
        return await iterator.throw(error);
      } finally {
        await finish(false);
      }
    },
    [Symbol.asyncIterator]() { return this; },
    async [Symbol.asyncDispose]() { await wrapped.return(undefined); },
  };
  const closeInBackground = (warning: string) => {
    // Start now to interrupt pending I/O, not when the callback queue reaches us.
    // Core's outer iterator can reject on abort before our return() settles.
    // Track this cleanup in its native drain API so awaitAllCallbacks() before
    // forceFlush()/shutdown also waits for every cancelled ancestor span.
    const closing = wrapped.return(undefined).catch(() => diag.warn(warning));
    void consumeCallback(() => closing, false);
  };
  const onAbort = () => {
    closeInBackground("Failed to close an aborted LangChain stream");
  };
  return wrapped;
}
