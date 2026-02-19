export type SafeFunction = (...args: unknown[]) => unknown;

export function isFunction(value: unknown): value is SafeFunction {
  return typeof value === "function";
}
