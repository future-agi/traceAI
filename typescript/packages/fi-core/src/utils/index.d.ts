import { GenericFunction, SafeFunction } from "./types";
export * from "./typeUtils";
/**
 * Wraps a function with a try-catch block to catch and log any errors.
 * @param fn - A function to wrap with a try-catch block.
 * @returns A function that returns null if an error is thrown.
 */
export declare function withSafety<T extends GenericFunction>({ fn, onError, }: {
    fn: T;
    onError?: (error: unknown) => void;
}): SafeFunction<T>;
export declare const safelyJSONStringify: SafeFunction<{
    (value: any, replacer?: (this: any, key: string, value: any) => any, space?: string | number): string;
    (value: any, replacer?: (number | string)[] | null, space?: string | number): string;
}>;
export declare const safelyJSONParse: SafeFunction<(text: string, reviver?: (this: any, key: string, value: any) => any) => any>;
