import { isAttributeValue } from "@opentelemetry/core";
/**
 * Type guard to determine whether or not a value is an array of strings.
 * @param value
 * @returns true if the value is an array of strings, false otherwise.
 */
export function isStringArray(value) {
    return Array.isArray(value) && value.every((v) => typeof v === "string");
}
/**
 * Type guard to determine whether or not a value is an object.
 * @param value
 * @returns true if the value is an object, false otherwise.
 */
function isObject(value) {
    return typeof value === "object" && value !== null && !Array.isArray(value);
}
/**
 * Type guard to determine whether or not a value is an object with string keys.
 * @param value
 * @returns true if the value is an object with string keys, false otherwise.
 */
export function isObjectWithStringKeys(value) {
    return (isObject(value) &&
        Object.keys(value).every((key) => typeof key === "string"));
}
/**
 * Type guard to determine whether or not a value is an object with string keys and attribute values.
 * @param value
 * @returns true if the value is an object with string keys and attribute values, false otherwise.
 */
export function isAttributes(value) {
    return (isObject(value) &&
        Object.entries(value).every(([key, value]) => isAttributeValue(value) && typeof key === "string"));
}
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
export function assertUnreachable(value) {
    throw new Error(`Unreachable code reached with value: ${value}`);
}
//# sourceMappingURL=data:application/json;base64,eyJ2ZXJzaW9uIjozLCJmaWxlIjoidHlwZVV0aWxzLmpzIiwic291cmNlUm9vdCI6IiIsInNvdXJjZXMiOlsidHlwZVV0aWxzLnRzIl0sIm5hbWVzIjpbXSwibWFwcGluZ3MiOiJBQUNBLE9BQU8sRUFBRSxnQkFBZ0IsRUFBRSxNQUFNLHFCQUFxQixDQUFDO0FBRXZEOzs7O0dBSUc7QUFDSCxNQUFNLFVBQVUsYUFBYSxDQUFDLEtBQWM7SUFDMUMsT0FBTyxLQUFLLENBQUMsT0FBTyxDQUFDLEtBQUssQ0FBQyxJQUFJLEtBQUssQ0FBQyxLQUFLLENBQUMsQ0FBQyxDQUFDLEVBQUUsRUFBRSxDQUFDLE9BQU8sQ0FBQyxLQUFLLFFBQVEsQ0FBQyxDQUFDO0FBQzNFLENBQUM7QUFFRDs7OztHQUlHO0FBQ0gsU0FBUyxRQUFRLENBQ2YsS0FBYztJQUVkLE9BQU8sT0FBTyxLQUFLLEtBQUssUUFBUSxJQUFJLEtBQUssS0FBSyxJQUFJLElBQUksQ0FBQyxLQUFLLENBQUMsT0FBTyxDQUFDLEtBQUssQ0FBQyxDQUFDO0FBQzlFLENBQUM7QUFFRDs7OztHQUlHO0FBQ0gsTUFBTSxVQUFVLHNCQUFzQixDQUNwQyxLQUFjO0lBRWQsT0FBTyxDQUNMLFFBQVEsQ0FBQyxLQUFLLENBQUM7UUFDZixNQUFNLENBQUMsSUFBSSxDQUFDLEtBQUssQ0FBQyxDQUFDLEtBQUssQ0FBQyxDQUFDLEdBQUcsRUFBRSxFQUFFLENBQUMsT0FBTyxHQUFHLEtBQUssUUFBUSxDQUFDLENBQzNELENBQUM7QUFDSixDQUFDO0FBRUQ7Ozs7R0FJRztBQUNILE1BQU0sVUFBVSxZQUFZLENBQUMsS0FBYztJQUN6QyxPQUFPLENBQ0wsUUFBUSxDQUFDLEtBQUssQ0FBQztRQUNmLE1BQU0sQ0FBQyxPQUFPLENBQUMsS0FBSyxDQUFDLENBQUMsS0FBSyxDQUN6QixDQUFDLENBQUMsR0FBRyxFQUFFLEtBQUssQ0FBQyxFQUFFLEVBQUUsQ0FBQyxnQkFBZ0IsQ0FBQyxLQUFLLENBQUMsSUFBSSxPQUFPLEdBQUcsS0FBSyxRQUFRLENBQ3JFLENBQ0YsQ0FBQztBQUNKLENBQUM7QUFFRDs7Ozs7Ozs7Ozs7Ozs7Ozs7R0FpQkc7QUFDSCxNQUFNLFVBQVUsaUJBQWlCLENBQUMsS0FBWTtJQUM1QyxNQUFNLElBQUksS0FBSyxDQUFDLHdDQUF3QyxLQUFLLEVBQUUsQ0FBQyxDQUFDO0FBQ25FLENBQUMiLCJzb3VyY2VzQ29udGVudCI6WyJpbXBvcnQgeyBBdHRyaWJ1dGVzIH0gZnJvbSBcIkBvcGVudGVsZW1ldHJ5L2FwaVwiO1xuaW1wb3J0IHsgaXNBdHRyaWJ1dGVWYWx1ZSB9IGZyb20gXCJAb3BlbnRlbGVtZXRyeS9jb3JlXCI7XG5cbi8qKlxuICogVHlwZSBndWFyZCB0byBkZXRlcm1pbmUgd2hldGhlciBvciBub3QgYSB2YWx1ZSBpcyBhbiBhcnJheSBvZiBzdHJpbmdzLlxuICogQHBhcmFtIHZhbHVlXG4gKiBAcmV0dXJucyB0cnVlIGlmIHRoZSB2YWx1ZSBpcyBhbiBhcnJheSBvZiBzdHJpbmdzLCBmYWxzZSBvdGhlcndpc2UuXG4gKi9cbmV4cG9ydCBmdW5jdGlvbiBpc1N0cmluZ0FycmF5KHZhbHVlOiB1bmtub3duKTogdmFsdWUgaXMgc3RyaW5nW10ge1xuICByZXR1cm4gQXJyYXkuaXNBcnJheSh2YWx1ZSkgJiYgdmFsdWUuZXZlcnkoKHYpID0+IHR5cGVvZiB2ID09PSBcInN0cmluZ1wiKTtcbn1cblxuLyoqXG4gKiBUeXBlIGd1YXJkIHRvIGRldGVybWluZSB3aGV0aGVyIG9yIG5vdCBhIHZhbHVlIGlzIGFuIG9iamVjdC5cbiAqIEBwYXJhbSB2YWx1ZVxuICogQHJldHVybnMgdHJ1ZSBpZiB0aGUgdmFsdWUgaXMgYW4gb2JqZWN0LCBmYWxzZSBvdGhlcndpc2UuXG4gKi9cbmZ1bmN0aW9uIGlzT2JqZWN0KFxuICB2YWx1ZTogdW5rbm93bixcbik6IHZhbHVlIGlzIFJlY29yZDxzdHJpbmcgfCBudW1iZXIgfCBzeW1ib2wsIHVua25vd24+IHtcbiAgcmV0dXJuIHR5cGVvZiB2YWx1ZSA9PT0gXCJvYmplY3RcIiAmJiB2YWx1ZSAhPT0gbnVsbCAmJiAhQXJyYXkuaXNBcnJheSh2YWx1ZSk7XG59XG5cbi8qKlxuICogVHlwZSBndWFyZCB0byBkZXRlcm1pbmUgd2hldGhlciBvciBub3QgYSB2YWx1ZSBpcyBhbiBvYmplY3Qgd2l0aCBzdHJpbmcga2V5cy5cbiAqIEBwYXJhbSB2YWx1ZVxuICogQHJldHVybnMgdHJ1ZSBpZiB0aGUgdmFsdWUgaXMgYW4gb2JqZWN0IHdpdGggc3RyaW5nIGtleXMsIGZhbHNlIG90aGVyd2lzZS5cbiAqL1xuZXhwb3J0IGZ1bmN0aW9uIGlzT2JqZWN0V2l0aFN0cmluZ0tleXMoXG4gIHZhbHVlOiB1bmtub3duLFxuKTogdmFsdWUgaXMgUmVjb3JkPHN0cmluZywgdW5rbm93bj4ge1xuICByZXR1cm4gKFxuICAgIGlzT2JqZWN0KHZhbHVlKSAmJlxuICAgIE9iamVjdC5rZXlzKHZhbHVlKS5ldmVyeSgoa2V5KSA9PiB0eXBlb2Yga2V5ID09PSBcInN0cmluZ1wiKVxuICApO1xufVxuXG4vKipcbiAqIFR5cGUgZ3VhcmQgdG8gZGV0ZXJtaW5lIHdoZXRoZXIgb3Igbm90IGEgdmFsdWUgaXMgYW4gb2JqZWN0IHdpdGggc3RyaW5nIGtleXMgYW5kIGF0dHJpYnV0ZSB2YWx1ZXMuXG4gKiBAcGFyYW0gdmFsdWVcbiAqIEByZXR1cm5zIHRydWUgaWYgdGhlIHZhbHVlIGlzIGFuIG9iamVjdCB3aXRoIHN0cmluZyBrZXlzIGFuZCBhdHRyaWJ1dGUgdmFsdWVzLCBmYWxzZSBvdGhlcndpc2UuXG4gKi9cbmV4cG9ydCBmdW5jdGlvbiBpc0F0dHJpYnV0ZXModmFsdWU6IHVua25vd24pOiB2YWx1ZSBpcyBBdHRyaWJ1dGVzIHtcbiAgcmV0dXJuIChcbiAgICBpc09iamVjdCh2YWx1ZSkgJiZcbiAgICBPYmplY3QuZW50cmllcyh2YWx1ZSkuZXZlcnkoXG4gICAgICAoW2tleSwgdmFsdWVdKSA9PiBpc0F0dHJpYnV0ZVZhbHVlKHZhbHVlKSAmJiB0eXBlb2Yga2V5ID09PSBcInN0cmluZ1wiLFxuICAgIClcbiAgKTtcbn1cblxuLyoqXG4gKiBBIHR5cGUgY2hlY2sgZnVuY3Rpb24gdG8gZW5zdXJlIHRoYXQgYSBzd2l0Y2ggb3Igc2V0IG9mIGNvbmRpdGlvbmFscyBpcyBleGhhdXN0aXZlLlxuICogVHlwc2NyaXB0IHdpbGwgdGhyb3cgYW4gZXJyb3IgaWYgdGhlIHN3aXRjaCBvciBjb25kaXRpb25hbHMgYXJlIG5vdCBleGhhdXN0aXZlLlxuICogQGV4YW1wbGVcbiAqICBgYGB0eXBlc2NyaXB0XG4gKiB0eXBlIE15VHlwZSA9IFwiYVwiIHwgXCJiXCI7XG4gKiBmdW5jdGlvbiBteUZ1bmN0aW9uKHZhbHVlOiBNeVR5cGUpIHtcbiAqICAgc3dpdGNoICh2YWx1ZSkge1xuICogICAgIGNhc2UgXCJhXCI6XG4gKiAgICAgIHJldHVybiBcIkFcIjtcbiAqICAgIGNhc2UgXCJiXCI6XG4gKiAgICAgIHJldHVybiBcIkJcIjtcbiAqICAgIGRlZmF1bHQ6XG4gKiAgICAgIGFzc2VydFVucmVhY2hhYmxlKHZhbHVlKTtcbiAqICAgfVxuICogfVxuICogYGBgXG4gKi9cbmV4cG9ydCBmdW5jdGlvbiBhc3NlcnRVbnJlYWNoYWJsZSh2YWx1ZTogbmV2ZXIpOiBuZXZlciB7XG4gIHRocm93IG5ldyBFcnJvcihgVW5yZWFjaGFibGUgY29kZSByZWFjaGVkIHdpdGggdmFsdWU6ICR7dmFsdWV9YCk7XG59Il19