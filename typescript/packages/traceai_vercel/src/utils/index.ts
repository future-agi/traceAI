import { GenericFunction, SafeFunction } from "../jsonutils/types";
export * from "../jsonutils/typeUtils";

/**
 * Wraps a function with a try-catch block to catch and log any errors.
 * @param fn - A function to wrap with a try-catch block.
 * @returns A function that returns null if an error is thrown.
 */
function withSafety<T extends GenericFunction>({
  fn,
  onError,
}: {
  fn: T;
  onError?: (error: unknown) => void;
}): SafeFunction<T> {
  return (...args) => {
    try {
      return fn(...args);
    } catch (error) {
      if (onError) {
        onError(error);
      }
      return null;
    }
  };
}

const safelyJSONStringify = withSafety({ fn: JSON.stringify });

const safelyJSONParse = withSafety({ fn: JSON.parse });

export { withSafety, safelyJSONStringify, safelyJSONParse };
