/**
 * Utility function that uses the type system to check if a switch statement is exhaustive.
 */
export function assertUnreachable(_: never): never {
  throw new Error("Unreachable");
}

export function isString(value: unknown): value is string {
  return typeof value === "string";
}
