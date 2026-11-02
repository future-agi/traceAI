// Shared only with the compatibility instrumentor: never install both routes.
export let nativeTracingEnabled = false;
export function enableNativeTracing(): void { nativeTracingEnabled = true; }
