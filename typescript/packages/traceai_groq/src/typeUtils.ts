/**
 * Utility function that uses the type system to check if a switch statement is exhaustive.
 * If the switch statement is not exhaustive, there will be a type error caught in typescript
 */
export function assertUnreachable(_: never): never {
  throw new Error("Unreachable");
}

export function isString(value: unknown): value is string {
  return typeof value === "string";
}
