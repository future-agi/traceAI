# @traceai/portkey

Trace Portkey gateway calls in Future AGI, including model names, token usage, tool calls, streaming duration, and errors.

[Documentation](https://docs.futureagi.com/docs/integrations/traceai/portkey) · [Source](https://github.com/future-agi/traceAI/tree/dev/typescript/packages/traceai_portkey) · [Issues](https://github.com/future-agi/traceAI/issues)

## Install

Use Node.js 22 or later and `portkey-ai` 1.x, 2.x, or 3.x.

```sh
npm install @traceai/portkey @traceai/fi-core portkey-ai @opentelemetry/api@^1.9.0 @opentelemetry/context-async-hooks@^2.0.1
```

Set `FI_API_KEY` and `FI_SECRET_KEY` for Future AGI. Set `PORTKEY_API_KEY`, `PORTKEY_VIRTUAL_KEY`, and `PORTKEY_MODEL` for your Portkey gateway route. Use your own provider credentials configured in Portkey; never embed keys in source.

## Trace a request

Save this as `app.mjs`. The CommonJS core entry works from both CommonJS and ESM applications.

```javascript
import { createRequire } from "node:module";
const require = createRequire(import.meta.url);
const { register, ProjectType } = require("@traceai/fi-core");
const { PortkeyInstrumentation } = require("@traceai/portkey");
const { Portkey } = require("portkey-ai");
const { context } = require("@opentelemetry/api");
const { AsyncLocalStorageContextManager } = require("@opentelemetry/context-async-hooks");

// Standalone applications only: reuse an existing OTel context manager if present.
context.setGlobalContextManager(new AsyncLocalStorageContextManager().enable());
const provider = register({
  projectName: "portkey-app",
  projectType: ProjectType.OBSERVE,
});
const instrumentation = new PortkeyInstrumentation({
  traceConfig: { hideInputs: true, hideOutputs: true },
});
instrumentation.setTracerProvider(provider);

const client = new Portkey({
  apiKey: process.env.PORTKEY_API_KEY,
  virtualKey: process.env.PORTKEY_VIRTUAL_KEY,
});
instrumentation.manuallyInstrument(client);

try {
  const response = await client.chat.completions.create({
    model: process.env.PORTKEY_MODEL,
    messages: [{ role: "user", content: "Explain a rainbow in one sentence." }],
  });
  console.log(response.choices[0].message.content);
} finally {
  await provider.shutdown();
}
```

Run `node app.mjs`, then open **portkey-app** in Future AGI **Observe**. The call appears as a **Portkey Chat Completions** span. Keep `shutdown()` at process exit in long-running services.

If you already have a tracer provider, pass it to `setTracerProvider()` instead of calling `register()` again.

## Supported operations

- `chat.completions.create()`: messages, tool calls, model, usage, and streaming.
- `completions.create()`: text completions, usage, and streaming.
- `embeddings.create()`: model, input, token usage, and vector count. Embedding vectors are not exported.
- `responses.create()`, when available in your SDK: Responses output, function calls, usage, and streaming with `stream: true`.

The provider attribute identifies the **Portkey gateway**, not the downstream provider selected by a routing or fallback configuration. Model attributes use the returned model when available. Tool calls are recorded as model output; this package does not execute tools or create tool-execution spans.

Audio, images, prompt-template APIs, `responses.parse()`, and the event-emitter `responses.stream()` helper are outside this package's scope. Use `responses.create({ stream: true, ... })` for traced Responses streams. Calls made directly with an OpenAI client are outside this package's scope; instrument that client separately rather than wrapping it in an extra Portkey span.

## Streaming and SDK helpers

Consume streams to complete their spans. Breaking out of `for await` closes the span and forwards iterator cleanup to the SDK. If you obtain a stream but will not iterate it, explicitly close its iterator with `await stream[Symbol.asyncIterator]().return()`.

Responses `withResponse()`, `asResponse()`, and stream conversion helpers remain available. `asResponse()` leaves the HTTP body unconsumed and records request timing/status only, without parsed output or token usage.

## Automatic registration

Enable the instrumentor **before loading `portkey-ai`** to capture subsequently constructed clients automatically. For SDK modules already loaded, pass the module to `manuallyInstrument()` before constructing clients through its exports. For an imported constructor or an existing client, use `manuallyInstrument(client)` as shown above. This also works in ESM applications without relying on loader hooks.

Register one Portkey instrumentor per process. Repeated instrumentation of the same resource does not duplicate spans. `disable()` restores SDK methods; `enable()` reapplies them.

## Privacy

The example hides prompts, responses, tool arguments, and error details while retaining operation metadata and token usage. To capture content intentionally, set `hideInputs: false` and `hideOutputs: false`. Without these options, the package follows the traceAI core's environment settings and defaults.

Partial input/output hiding flags also suppress duplicate raw payloads and tool arguments. SDK credentials and request headers are not exported. Configure any other instrumentation in your application separately.

## License

Apache-2.0.
