/**
 * Type guard to check if a value is a string
 */
export function isString(value: unknown): value is string {
  return typeof value === "string";
}

/**
 * Assert that a value is never (useful for exhaustive switch statements)
 */
export function assertUnreachable(value: never): never {
  throw new Error(`Unreachable: ${value}`);
}
