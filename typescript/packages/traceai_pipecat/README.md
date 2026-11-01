# @traceai/fi-instrumentation-pipecat

Trace Pipecat **JavaScript client** connection events and operations with OpenTelemetry. For server conversation, STT, LLM, TTS and tool spans, use [Pipecat's native Python tracing integration](https://docs.futureagi.com/docs/integrations/traceai/pipecat).

## Install

```sh
npm install @traceai/fi-instrumentation-pipecat @pipecat-ai/client-js@^1.16.0
npm install @opentelemetry/api@^1.9.0 @opentelemetry/sdk-trace-web@1.30.1 @opentelemetry/sdk-trace-base@1.30.1 @opentelemetry/resources@1.30.1 @opentelemetry/exporter-trace-otlp-http@0.57.2
```

Supported clients are `PipecatClient` 1.16.x and later 1.x releases, and legacy `RTVIClient` 0.3.x. The npm package named `@pipecat-ai/client-js` has no published 0.2.0 version. Neither client exports a server `Pipeline` or `FrameProcessor`.

## Observe browser client events

Start with a working Pipecat client and transport. Use the `/client` entry point in browser bundles: it uses the public `on`/`off` event API, has no Node instrumentation dependencies, and leaves your existing callbacks intact.

Send telemetry to an **authenticated, same-origin OTLP gateway** operated by your backend. The gateway forwards traces to Future AGI and supplies `x-api-key` and `x-secret-key` from server-side `FI_API_KEY` and `FI_SECRET_KEY`. Keep those keys out of browser bundles, public environment variables and client requests. On the gateway, set the resource attributes `project_name` and `project_type=observe` for your destination project; do not let clients choose another tenant's destination. The Future AGI OTLP HTTP traces endpoint is `https://api.futureagi.com/tracer/v1/traces`.

```typescript
import type { PipecatClient } from "@pipecat-ai/client-js";
import { WebTracerProvider } from "@opentelemetry/sdk-trace-web";
import { BatchSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { Resource } from "@opentelemetry/resources";
import { OTLPTraceExporter } from "@opentelemetry/exporter-trace-otlp-http";
import { observePipecatClient } from "@traceai/fi-instrumentation-pipecat/client";

// Call before connecting your existing Pipecat client.
export function traceClient(client: PipecatClient, collectorUrl: string): () => Promise<void> {
  const provider = new WebTracerProvider({
    resource: new Resource({ "service.name": "pipecat-web" }),
    spanProcessors: [new BatchSpanProcessor(new OTLPTraceExporter({ url: collectorUrl }))],
  });
  const detach = observePipecatClient(client, { tracer: provider.getTracer("pipecat-web") });
  return async () => {
    detach();
    await provider.shutdown();
  };
}
```

Pass your existing client and gateway's absolute URL, for example `new URL("/otel/v1/traces", window.location.origin).href`, to `traceClient`. Connect and use the client as usual. After `await client.disconnect()`, call and await the returned cleanup function. This flushes queued spans and removes only this integration's listeners.

If you already have an OpenTelemetry provider, reuse it instead of creating another one: `observePipecatClient(client, { tracer: provider.getTracer("pipecat-web") })` returns a listener cleanup function. Keep provider shutdown in your existing application lifecycle.

In Future AGI, open your configured project and look for `Pipecat Client Event: connected`, `botReady` and `disconnected`. `transportStateChanged`, `error` and `messageError` also produce client event spans. Each span describes a notification, not a server operation's duration. An optional `parentContext` associates events with an application-owned span without modifying transport headers or bot messages.

## Existing Node instrumentation

`PipecatInstrumentation`, `manuallyInstrument()` and `isPatched()` remain available from the root entry point. Register the instrumentation with your existing Node tracer provider **before requiring** `@pipecat-ai/client-js`. For already loaded modules or ESM, pass the actual imported module to `manuallyInstrument()`.

```javascript
const { PipecatInstrumentation } = require("@traceai/fi-instrumentation-pipecat");
const instrumentation = new PipecatInstrumentation();
instrumentation.setTracerProvider(tracerProvider); // Your existing Node OTel provider.
const pipecat = require("@pipecat-ai/client-js");
instrumentation.manuallyInstrument(pipecat); // Idempotent with automatic instrumentation.
```

For Node 18 ESM applications, load client-js 1.16 through its CommonJS entry with `createRequire(import.meta.url)("@pipecat-ai/client-js")` (import `createRequire` from `node:module`). Its upstream `.js` ESM entry lacks `type:module`; use a bundler or a newer Node with syntax detection for native imports. Pass the loaded module to `manuallyInstrument()`.

This path traces `connect` and `disconnect`, legacy `action`/`sendMessage`, and current `sendText`/`sendClientMessage`/`sendClientRequest`. It preserves synchronous returns and throws. Connect spans end when the connection attempt completes, rather than waiting for disconnect. `disable()` restores patched methods, including manually instrumented modules. The client SDK still requires its normal browser/media environment; the Node instrumentor does not supply a transport or make browser-only SDKs server runtimes.

Choose public-event observation for browser lifecycle telemetry, or Node operation tracing for method timings. They describe different events; do not stack both to obtain server pipeline tracing.

## Privacy and scope

Both paths record fixed operation/event names, status codes and `pipecat.telemetry_source=client`. They do **not** record message text, transcripts, audio/video, action arguments/results, URLs, connection parameters or exception details. This also applies to legacy clients: earlier versions' payload capture is no longer used. `traceConfig` remains accepted for source compatibility, but does not enable content capture.

Client spans use `fi.span.kind=CHAIN`; sending a client message is not an LLM or tool invocation. Keep native Python tracing on the server for those spans. This package neither fabricates server spans nor injects credentials or trace context into your transport.

## Troubleshooting

- **No browser spans:** attach the observer before connecting, use the `/client` import, and check the gateway's HTTP response and forwarding configuration.
- **Spans remain queued:** await provider shutdown when disposing the application, or `forceFlush()` on your existing provider before checking the collector.
- **No STT/LLM/TTS spans:** enable [native server tracing](https://docs.futureagi.com/docs/integrations/traceai/pipecat); the JavaScript client cannot measure server execution.

## License

Apache-2.0.
