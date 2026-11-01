import { context, type Context } from "@opentelemetry/api";
import { record } from "./capture.js";

/** Older Interactions APIs return APIPromise, not a plain Promise. In particular,
 * asResponse() must not trigger JSON parsing or consume the caller's raw body.
 */
export function wrapAPIPromise(
  target: object,
  child: Context,
  resolve: (value: unknown) => unknown,
  reject: (error: unknown) => never,
  requestOnly: () => void,
): object {
  let consumed: Promise<unknown> | undefined;
  const consume = () =>
    (consumed ??= context.with(child, () =>
      Promise.resolve(target).then(resolve, reject),
    ));
  return new Proxy(target, {
    get(object, key) {
      if (key === "then" || key === "catch" || key === "finally") {
        // Accessing .then itself must not start consuming a lazy response.
        return (...args: unknown[]) => {
          const promise = consume();
          return Reflect.apply(Reflect.get(promise, key), promise, args);
        };
      }
      const method = Reflect.get(object, key, object);
      if (typeof method !== "function") return method;
      if (key === "withResponse" || key === "asResponse")
        return (...args: unknown[]) => {
          try {
            return Promise.resolve(
              context.with(child, () => Reflect.apply(method, object, args)),
            ).then((value) => {
              if (key === "asResponse") {
                requestOnly();
                return value;
              }
              const envelope = record(value);
              const data = resolve(envelope.data);
              // Non-stream data keeps identity. Streaming data alone gets the iterator observer.
              return data === envelope.data ? value : { ...envelope, data };
            }, reject);
          } catch (error) {
            return reject(error);
          }
        };
      return method.bind(object);
    },
  });
}
