// Use the SDK's lazy transformation, not Promise.then(): SDK helpers call
// _thenUnwrap themselves, and asResponse must never start parsing the body.
// Structural detection also works across SDK versions and duplicate installs.
interface SDKPromise<T> extends Promise<T> {
  _thenUnwrap<U>(transform: (data: T) => U): SDKPromise<U>;
  asResponse(): Promise<{ status: number }>;
  withResponse(): Promise<{ data: T }>;
}

function isSDKPromise<T>(promise: Promise<T>): promise is SDKPromise<T> {
  const candidate = promise as Partial<SDKPromise<T>>;
  return typeof candidate._thenUnwrap === "function" &&
    typeof candidate.asResponse === "function" &&
    typeof candidate.withResponse === "function";
}

export function observeAPIPromise<T>(
  promise: Promise<T>,
  onResult: (value: T) => T,
  onError: (error: unknown) => void,
  onRawResponse: (response: { status: number }) => void,
): Promise<T> {
  const reject = (error: unknown): never => {
    onError(error);
    throw error;
  };
  if (!isSDKPromise(promise)) {
    // Preserve compatibility with clients/test doubles returning plain promises.
    return promise.then(onResult).catch(reject);
  }

  const observed = promise._thenUnwrap(onResult);
  // SDK 4–7 keep the transport promise separate from lazy body parsing. Replace
  // it only on our derived APIPromise: a dropped request must still reject
  // unhandled, while every native consumer/helper handles this same rejection.
  // A detached asResponse().catch(...) would silently consume dropped errors.
  const transport = observed as SDKPromise<T> & { responsePromise?: Promise<unknown> };
  if (transport.responsePromise) {
    transport.responsePromise = transport.responsePromise.catch(reject);
  }

  function wrap<U>(target: SDKPromise<U>): SDKPromise<U> {
    return new Proxy(target, {
      get(target, property) {
        switch (property) {
          case "then":
            return (...args: Parameters<Promise<U>["then"]>) =>
              target.then(undefined, reject).then(...args);
          case "catch":
            return (...args: Parameters<Promise<U>["catch"]>) =>
              target.then(undefined, reject).catch(...args);
          case "finally":
            return (...args: Parameters<Promise<U>["finally"]>) =>
              target.then(undefined, reject).finally(...args);
          case "withResponse":
            // Call on the native receiver: its internal asResponse is not a
            // raw-only caller, so it must not finish the span before parsing.
            return () => target.withResponse().catch(reject);
          case "asResponse":
            return () => target.asResponse().then(response => {
              onRawResponse(response);
              return response;
            }, reject);
          case "_thenUnwrap":
            return (transform: (data: U) => unknown) =>
              wrap(target._thenUnwrap(transform));
          default:
            return Reflect.get(target, property, target);
        }
      },
    });
  }
  return wrap(observed);
}
