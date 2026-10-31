# @traceai/fi-instrumentation-vertexai

OpenTelemetry instrumentation for Google Cloud Vertex AI SDK. Part of traceAI, Future AGI's OpenTelemetry instrumentation libraries.

[Documentation](https://docs.futureagi.com/docs/integrations/traceai/vertexai) · [Source](https://github.com/future-agi/traceAI/tree/dev/typescript/packages/traceai_vertexai) · [Issues](https://github.com/future-agi/traceAI/issues)

## Install

```sh
npm install @traceai/fi-instrumentation-vertexai @traceai/fi-core @opentelemetry/instrumentation @opentelemetry/api@^1.9.0 @opentelemetry/context-async-hooks@^2.0.1
npm install @google-cloud/vertexai@"^1.0.0"
```

The SDK ranges above match this instrumentor's module-version filters. Install
only the SDKs your application uses. For Future AGI export, set `FI_API_KEY` and
`FI_SECRET_KEY` in your environment; keep provider credentials outside source code.

## Package entry points

This source revision fixes the package entry points. Both CommonJS `require()`
and native Node ESM `import` use the same CommonJS implementation; this avoids
broken, extensionless ESM build imports. The corrected packages still need a
patch release. A previously published `0.1.0` tarball is not changed by this README.

## Quick start

Load this setup **before** loading the SDK you want to instrument. This CommonJS
example registers tracing; it does not make a provider request.

```javascript
const { context } = require("@opentelemetry/api");
const { AsyncLocalStorageContextManager } = require("@opentelemetry/context-async-hooks");
const { register, ProjectType } = require("@traceai/fi-core");
// Standalone process only: if your OTel SDK already installs a context manager,
// reuse it instead of registering another one.
context.setGlobalContextManager(new AsyncLocalStorageContextManager().enable());
const { registerInstrumentations } = require("@opentelemetry/instrumentation");
const { VertexAIInstrumentation } = require("@traceai/fi-instrumentation-vertexai");
const tracerProvider = register({
  projectName: "vertexai-app",
  projectType: ProjectType.OBSERVE,
});
registerInstrumentations({
  tracerProvider, instrumentations: [new VertexAIInstrumentation()],
});
```

`register()` installs a tracer provider, not an async context manager. The
explicit context manager above keeps nested asynchronous calls in one trace.
Run your application's SDK calls after this setup. Call
`await tracerProvider.shutdown()` when the application finishes to flush pending
spans. For ESM, follow the documentation for your OpenTelemetry loader and SDK
instead of assuming import order alone enables automatic patching.

## Compatibility and privacy

This instruments the SDK hooks implemented by this package, not every API offered
by the provider. The install ranges above are intentionally limited to the module versions this
instrumentor patches; its peer declarations may be broader.
Traces can contain prompts, responses, and tool data; configure capture and
redaction before sending production traffic.

## License

Apache-2.0.
