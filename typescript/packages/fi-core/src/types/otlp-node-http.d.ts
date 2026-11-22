// The package's "node-http" subpath is not resolvable by TypeScript's node10 module
// resolution; only the two functions used by otlp-header-isolation.ts are declared.
declare module "@opentelemetry/otlp-exporter-base/node-http" {
  export function convertLegacyHttpOptions(config: any, signalIdentifier: string, signalResourcePath: string, requiredHeaders: Record<string, string>): any;
  export function createOtlpHttpExportDelegate(options: any, serializer: any): any;
}
