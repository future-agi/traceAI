# @traceai/fi-instrumentation-google-adk

OpenTelemetry instrumentation for Google AI Development Kit (ADK). Part of traceAI, Future AGI's OpenTelemetry instrumentation libraries.

[Documentation](https://docs.futureagi.com/docs/integrations/traceai/google_adk) · [Source](https://github.com/future-agi/traceAI/tree/dev/typescript/packages/traceai_google_adk) · [Issues](https://github.com/future-agi/traceAI/issues)

## Install

```sh
npm install @traceai/fi-instrumentation-google-adk @traceai/fi-core @opentelemetry/instrumentation
npm install @google/adk@"^1.0.0"
```

The SDK ranges above match this instrumentor's module-version filters. Install
only the SDKs your application uses. For Future AGI export, set `FI_API_KEY` and
`FI_SECRET_KEY` in your environment; keep provider credentials outside source code.

## Current packaging limitation

This source revision builds successfully, but its package entry points still
refer to `dist/index.js` and `dist/esm/index.js`; the compiler emits
`dist/src/index.js` and `dist/esm/src/index.js` instead. Importing the package by
name therefore fails until those entry points are corrected. The setup below
shows the initialization API, but is **not a working install recipe for this
revision**. Registry metadata changes alone do not fix this packaging issue.

## Quick start

Load this setup **before** loading the SDK you want to instrument. This CommonJS
example registers tracing; it does not make a provider request.

```javascript
const { register, ProjectType } = require("@traceai/fi-core");
const { registerInstrumentations } = require("@opentelemetry/instrumentation");
const { GoogleADKInstrumentation } = require("@traceai/fi-instrumentation-google-adk");
const tracerProvider = register({
  projectName: "google-adk-app",
  projectType: ProjectType.OBSERVE,
});
registerInstrumentations({
  tracerProvider, instrumentations: [new GoogleADKInstrumentation()],
});
```

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
