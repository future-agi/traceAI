export type GenericFunction = (...args: any[]) => any;
export type SafeFunction<T extends GenericFunction> = (...args: Parameters<T>) => ReturnType<T> | null;
