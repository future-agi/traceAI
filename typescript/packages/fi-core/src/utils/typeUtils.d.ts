import { Attributes } from "@opentelemetry/api";
/**
 * Type guard to determine whether or not a value is an array of strings.
 * @param value
 * @returns true if the value is an array of strings, false otherwise.
 */
export declare function isStringArray(value: unknown): value is string[];
/**
 * Type guard to determine whether or not a value is an object with string keys.
 * @param value
 * @returns true if the value is an object with string keys, false otherwise.
 */
export declare function isObjectWithStringKeys(value: unknown): value is Record<string, unknown>;
/**
 * Type guard to determine whether or not a value is an object with string keys and attribute values.
 * @param value
 * @returns true if the value is an object with string keys and attribute values, false otherwise.
 */
export declare function isAttributes(value: unknown): value is Attributes;
/**
 * A type check function to ensure that a switch or set of conditionals is exhaustive.
 * Typscript will throw an error if the switch or conditionals are not exhaustive.
 * @example
 *  ```typescript
 * type MyType = "a" | "b";
 * function myFunction(value: MyType) {
 *   switch (value) {
 *     case "a":
 *      return "A";
 *    case "b":
 *      return "B";
 *    default:
 *      assertUnreachable(value);
 *   }
 * }
 * ```
 */
export declare function assertUnreachable(value: never): never;
