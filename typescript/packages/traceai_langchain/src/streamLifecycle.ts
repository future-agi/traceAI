import { diag } from "@opentelemetry/api";

type CancelRun = () => Promise<void>;
type StreamScope = { runs: Set<CancelRun>; cancelled: boolean };
const streams = new Map<string, StreamScope>();

export function registerStreamRun(runId: string, cancel: CancelRun): () => void {
  const scope = streams.get(runId);
  scope?.runs.add(cancel);
  return () => { scope?.runs.delete(cancel); };
}

export function isStreamCancelled(runId: string): boolean {
  return streams.get(runId)?.cancelled === true;
}

/**
 * An iterator facade, rather than an async generator: return() must abort a
 * pending provider read immediately, not queue behind its next(). Otherwise a
 * consumer break can hang forever on an SSE connection waiting for more tokens.
 * Only the matching run is finalized; unrelated streams and callbacks are left alone.
 */
export function wrapModelStream<T>(
  create: (signal: AbortSignal) => AsyncGenerator<T>,
  runId: string,
  signal?: AbortSignal,
): AsyncGenerator<T> {
  const controller = new AbortController();
  const scope: StreamScope = { runs: new Set(), cancelled: false };
  streams.set(runId, scope);
  const iterator = create(controller.signal);
  let completion: Promise<void> | undefined;
  let completed = false;

  const finish = (success: boolean) => {
    // next(), return(), and the abort listener may all finish the same iterator.
    return completion ??= (async () => {
      signal?.removeEventListener("abort", onAbort);
      completed = true;
      try {
        if (!success) await Promise.all([...scope.runs].map((cancel) => cancel()));
      } catch {
        // Instrumentation cleanup must not replace an application's error.
        diag.warn("Failed to finalize a LangChain stream");
      } finally {
        if (streams.get(runId) === scope) streams.delete(runId);
        scope.runs.clear();
      }
    })();
  };
  const cancel = (reason: unknown) => {
    if (completed) return;
    scope.cancelled = true;
    controller.abort(reason);
  };
  const wrapped: AsyncGenerator<T> = {
    async next(...args) {
      try {
        signal?.throwIfAborted();
        const result = await iterator.next(...args);
        signal?.throwIfAborted();
        if (result.done) await finish(!scope.cancelled);
        return result;
      } catch (error) {
        await finish(false);
        throw error;
      }
    },
    async return(value) {
      const reason = signal?.reason ?? new Error("Stream closed before completion");
      cancel(reason);
      try {
        return await iterator.return(value);
      } finally {
        await finish(false);
      }
    },
    async throw(error) {
      cancel(error);
      try {
        return await iterator.throw(error);
      } finally {
        await finish(false);
      }
    },
    [Symbol.asyncIterator]() { return this; },
    async [Symbol.asyncDispose]() { await wrapped.return(undefined); },
  };
  const onAbort = () => {
    // Recent cores race next() against the signal but do not return the generator
    // when it is paused at a yield. Explicitly close both the source and its span.
    void wrapped.return(undefined).catch(() => diag.warn("Failed to close an aborted LangChain stream"));
  };
  signal?.addEventListener("abort", onAbort, { once: true });
  if (signal?.aborted) onAbort();
  return wrapped;
}
