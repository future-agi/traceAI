import { context, Context } from "@opentelemetry/api";

/** Observe lazily, preserving the SDK stream's methods, receiver and cancellation. */
export function observeStream<T>(
  source: AsyncIterable<T>,
  spanContext: Context,
  onValue: (value: T) => void,
  onEnd: (error?: unknown) => void,
): AsyncIterable<T> {
  let ended = false;
  const finish = (error?: unknown) => {
    if (!ended) { ended = true; onEnd(error); }
  };
  const observe = async (call: () => PromiseLike<IteratorResult<T>>): Promise<IteratorResult<T>> => {
    try {
      const result = await context.with(spanContext, call);
      if (result.done) finish();
      else onValue(result.value);
      return result;
    } catch (error) { finish(error); throw error; }
  };
  const cancel = async (call: () => unknown) => {
    try { const result = await context.with(spanContext, call); finish(); return result; }
    catch (error) { finish(error); throw error; }
  };
  const wrapIterator = (iterator: AsyncIterator<T>): AsyncIterableIterator<T> => ({
    next: (...args) => observe(() => iterator.next(...args)),
    return: value => observe(() => iterator.return ? iterator.return(value) : Promise.resolve({ done: true, value })),
    throw: error => observe(() => iterator.throw ? iterator.throw(error) : Promise.reject(error)),
    [Symbol.asyncIterator]() { return this; },
  });

  // Web ReadableStream pipe/tee methods consume internally, bypassing getReader
  // and the async iterator. Use a lazy, zero-prefetch bridge only for these APIs.
  let bridge: ReadableStream<T> | undefined;
  let bridgeIterator: AsyncIterator<T> | undefined;
  const pipedStream = () => {
    if (!bridge) {
      // A failed pipe with preventCancel must release this iterator's reader lock
      // without cancelling the caller's original ReadableStream.
      const iterator = (source as ReadableStream<T>).values({ preventCancel: true });
      bridgeIterator = iterator;
      bridge = new ReadableStream<T>({
        async pull(controller) {
          try {
            const result = await observe(() => iterator.next());
            if (result.done) controller.close();
            else controller.enqueue(result.value);
          } catch (error) { controller.error(error); }
        },
        async cancel(reason) {
          await cancel(() => iterator.return?.(reason));
          await (source as ReadableStream<T>).cancel(reason);
        },
      }, { highWaterMark: 0 });
    }
    return bridge;
  };

  return new Proxy(source, {
    get(target, key) {
      const value = Reflect.get(target, key, target);
      if ((key === Symbol.asyncIterator || key === "values") && typeof value === "function") {
        return (...args: unknown[]) => wrapIterator(Reflect.apply(value, target, args));
      }
      if (key === "next" && typeof value === "function") {
        return (...args: unknown[]) => observe(() => Reflect.apply(value, target, args));
      }
      if ((key === "return" || key === "throw") && typeof value === "function") {
        return (...args: unknown[]) => observe(() => Reflect.apply(value, target, args));
      }
      if (key === "getReader" && typeof value === "function") {
        return (...args: unknown[]) => {
          const reader = Reflect.apply(value, target, args) as object;
          return new Proxy(reader, {
            get(readerTarget, readerKey) {
              const member = Reflect.get(readerTarget, readerKey, readerTarget);
              if (readerKey === "read") return (...readArgs: unknown[]) => observe(() => Reflect.apply(member, readerTarget, readArgs));
              if (readerKey === "cancel") return (...cancelArgs: unknown[]) => cancel(() => Reflect.apply(member, readerTarget, cancelArgs));
              return typeof member === "function" ? member.bind(readerTarget) : member;
            },
          });
        };
      }
      if (key === "cancel" && typeof value === "function") {
        return (...args: unknown[]) => cancel(() => Reflect.apply(value, target, args));
      }
      if (["pipeTo", "pipeThrough", "tee"].includes(String(key)) && typeof value === "function") {
        return (...args: unknown[]) => {
          const stream = pipedStream();
          const preventCancel = (args[1] as { preventCancel?: boolean } | undefined)?.preventCancel;
          const releaseOnFailure = (pipe: Promise<void>) => !preventCancel ? pipe : pipe.catch(async error => {
            // The bridge's pipeTo does not cancel on this path, so release its
            // reader explicitly; the original stream remains available to resume.
            try { await bridgeIterator?.return?.(); }
            finally { bridge = undefined; bridgeIterator = undefined; }
            throw error;
          });
          if (key === "pipeThrough" && preventCancel) {
            // pipeThrough starts an internal pipeTo but hides its promise. Keep
            // that promise to release the reader when its output is cancelled.
            const pair = args[0] as { writable: WritableStream<T>; readable: ReadableStream<unknown> };
            const pipe = releaseOnFailure(stream.pipeTo(pair.writable, args[1] as Parameters<ReadableStream<T>["pipeTo"]>[1]));
            void pipe.catch(() => {}); // pipeThrough does not expose pipe failures.
            return pair.readable;
          }
          const result = Reflect.apply(Reflect.get(stream, key), stream, args);
          return key === "pipeTo" ? releaseOnFailure(result as Promise<void>) : result;
        };
      }
      // Native ReadableStream accessors/methods require their original receiver.
      return typeof value === "function" ? value.bind(target) : value;
    },
  });
}
