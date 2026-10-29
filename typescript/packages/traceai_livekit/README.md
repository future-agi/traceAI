# @traceai/fi-instrumentation-livekit

OpenTelemetry instrumentation for LiveKit real-time communication SDK. Part of traceAI, Future AGI's OpenTelemetry instrumentation libraries.

[Documentation](https://docs.futureagi.com/docs/integrations/traceai/livekit) · [Source](https://github.com/future-agi/traceAI/tree/dev/typescript/packages/traceai_livekit) · [Issues](https://github.com/future-agi/traceAI/issues)

## Install

```sh
npm install @traceai/fi-instrumentation-livekit @traceai/fi-core @opentelemetry/instrumentation
npm install livekit-client@"^2.0.0" @livekit/rtc-node@"^0.5.0"
```

The SDK ranges above match this instrumentor's module-version filters. Install
only the SDKs your application uses. For Future AGI export, set `FI_API_KEY` and
`FI_SECRET_KEY` in your environment; keep provider credentials outside source code.

## Quick start

Load this setup **before** loading the SDK you want to instrument. This CommonJS
example registers tracing; it does not make a provider request.

```javascript
const { register, ProjectType } = require("@traceai/fi-core");
const { registerInstrumentations } = require("@opentelemetry/instrumentation");
const { LiveKitInstrumentation } = require("@traceai/fi-instrumentation-livekit");
const tracerProvider = register({
  projectName: "livekit-app",
  projectType: ProjectType.OBSERVE,
});
registerInstrumentations({
  tracerProvider, instrumentations: [new LiveKitInstrumentation()],
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
