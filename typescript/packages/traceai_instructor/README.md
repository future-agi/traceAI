# @traceai/instructor

OpenTelemetry instrumentation for Instructor. Part of traceAI, Future AGI's OpenTelemetry instrumentation libraries.

[Documentation](https://docs.futureagi.com/docs/integrations/traceai/instructor) · [Source](https://github.com/future-agi/traceAI/tree/dev/typescript/packages/traceai_instructor) · [Issues](https://github.com/future-agi/traceAI/issues)

## Install

```sh
npm install @traceai/instructor @traceai/fi-core @opentelemetry/instrumentation
npm install @instructor-ai/instructor@"^1.0.0"
```

The SDK ranges above match this instrumentor's module-version filters. Install
only the SDKs your application uses. For Future AGI export, set `FI_API_KEY` and
`FI_SECRET_KEY` in your environment; keep provider credentials outside source code.

## Current packaging limitation

The CommonJS setup below was checked against this source revision. Its declared
ESM/esnext entry paths do not match the emitted `dist/esm/src` and
`dist/esnext/src` layout, so ESM consumers need a packaging correction before
using those entry points. Registry metadata changes alone do not fix this issue.

## Quick start

Load this setup **before** loading the SDK you want to instrument. This CommonJS
example registers tracing; it does not make a provider request.

```javascript
const { register, ProjectType } = require("@traceai/fi-core");
const { registerInstrumentations } = require("@opentelemetry/instrumentation");
const { InstructorInstrumentation } = require("@traceai/instructor");
const tracerProvider = register({
  projectName: "instructor-app",
  projectType: ProjectType.OBSERVE,
});
registerInstrumentations({
  tracerProvider, instrumentations: [new InstructorInstrumentation()],
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
