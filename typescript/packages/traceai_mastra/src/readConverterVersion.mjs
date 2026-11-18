import { createRequire } from "node:module";

// Mastra's Node floor (22.13) supports this synchronous ESM helper from CJS too.
// Anchor resolution to the installed integration, or its bundle when inlined.
/** @returns {unknown} */
export function readConverterVersion() {
  const load = createRequire(import.meta.url ?? __filename);
  return load("@mastra/otel-exporter/package.json").version;
}
