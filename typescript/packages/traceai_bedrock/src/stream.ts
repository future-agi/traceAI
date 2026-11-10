import { Context, context, diag } from "@opentelemetry/api";

export interface StreamAccumulator {
  add(event: unknown): void;
  finish(): void;
}

/** Attribute capture must not change SDK results or hide transport errors. */
export function capture(fn: () => void): void {
  try {
    fn();
  } catch {
    diag.debug("Unable to capture Bedrock attributes");
  }
}

/** Observe only what the caller pulls. Never tee, prefetch, or buffer SDK events. */
export function observeStream<T>(
  source: AsyncIterable<T>,
  spanContext: Context,
  accumulator: StreamAccumulator,
  finish: (...errors: [] | [unknown]) => void,
  onErrorEvent: (error: unknown) => void,
): AsyncIterableIterator<T> {
  let iterator: AsyncIterator<T> | undefined;
  let finished = false;
  const getIterator = () => (iterator ??= source[Symbol.asyncIterator]());
  const complete = (...errors: [] | [unknown]) => {
    if (finished) return;
    finished = true;
    capture(() => accumulator.finish());
    finish(...errors);
  };
  const observe = (result: IteratorResult<T>) => {
    if (result.done) complete();
    else {
      capture(() => {
        // AWS event streams may deliver modeled exceptions as events, not throws.
        if (result.value && typeof result.value === "object") {
          for (const [name, value] of Object.entries(result.value)) {
            if (name.endsWith("Exception") && value && typeof value === "object") {
              onErrorEvent({ name, ...value });
            }
          }
        }
        accumulator.add(result.value);
      });
    }
    return result;
  };
  return {
    [Symbol.asyncIterator]() { return this; },
    async next(...args: [] | [unknown]) {
      try {
        return observe(await context.with(spanContext, () => getIterator().next(...args)));
      } catch (error) {
        complete(error);
        throw error;
      }
    },
    async return(value?: unknown) {
      try {
        const result = await context.with(spanContext, () => {
          const iter = getIterator();
          return iter.return ? iter.return(value) : { done: true as const, value };
        });
        // A generator's finally block may yield during return(); preserve that lifecycle.
        return observe(result);
      } catch (error) {
        complete(error);
        throw error;
      }
    },
    async throw(error?: unknown) {
      try {
        const result = await context.with(spanContext, () => {
          const iter = getIterator();
          if (iter.throw) return iter.throw(error);
          throw error;
        });
        return observe(result);
      } catch (thrown) {
        complete(thrown);
        throw thrown;
      }
    },
  };
}
