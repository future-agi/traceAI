# @traceai/mistral

Trace native Mistral SDK calls in Future AGI, including model inputs and outputs, tool calls, token usage, errors, and streaming responses.

## Install

```bash
npm install @traceai/mistral @traceai/fi-core @mistralai/mistralai
```

Set `FI_API_KEY` and `FI_SECRET_KEY` to your Future AGI credentials, and `MISTRAL_API_KEY` to your own Mistral API key.

## Trace a client

Use explicit client instrumentation with either ESM or CommonJS. It returns the same client and preserves your transport, endpoint, request options, and cancellation signals.

```typescript
import { register, ProjectType } from "@traceai/fi-core";
import { MistralInstrumentation } from "@traceai/mistral";
import { Mistral } from "@mistralai/mistralai";

const provider = register({
  projectName: "mistral-app",
  projectType: ProjectType.OBSERVE,
});
const instrumentation = new MistralInstrumentation();
instrumentation.setTracerProvider(provider);

const client = instrumentation.instrumentClient(
  new Mistral({ apiKey: process.env.MISTRAL_API_KEY }),
);

try {
  const response = await client.chat.complete({
    model: "mistral-small-latest",
    messages: [{ role: "user", content: "Suggest a name for a neighborhood bakery." }],
  });
  console.log(response.choices[0]?.message.content);
} finally {
  instrumentation.disable();
  await provider.shutdown();
}
```

Open the **mistral-app** project in Future AGI to inspect the request's span.

If you already have an OpenTelemetry tracer provider, pass it to `setTracerProvider` instead of creating another one. Choose this instrumentation or the SDK's native telemetry, not both, to avoid duplicate model spans.

## Instrument all native clients

For SDKs with a named `Mistral` export and resource clients, register the imported module instead. This covers clients and resource objects created before or after registration, without modifying the ESM namespace.

```typescript
import * as mistralSDK from "@mistralai/mistralai";

instrumentation.manuallyInstrument(mistralSDK);
const client = new mistralSDK.Mistral({ apiKey: process.env.MISTRAL_API_KEY });
```

CommonJS applications can also register the instrumentation before requiring the SDK through OpenTelemetry's `registerInstrumentations`. For ESM, explicit `instrumentClient` or `manuallyInstrument` registration avoids depending on a loader hook.

Legacy default-export SDKs, including `0.1.3` and `1.0.0`, define methods on each client. Use `instrumentClient(client)` with those versions and keep calling their original `chat`, `chatStream`, and `embeddings` APIs.

`disable()` restores instrumented methods. `enable()` reattaches them. Registering the same client or module repeatedly does not add duplicate spans.

## Stream a response

```typescript
const stream = await client.chat.stream({
  model: "mistral-small-latest",
  messages: [{ role: "user", content: "Write a short welcome message for the bakery." }],
});

for await (const event of stream) {
  const content = event.data.choices[0]?.delta.content;
  if (typeof content === "string") process.stdout.write(content);
}
```

The span ends when consumption completes, fails, or is cancelled—not when the stream is first returned. Consume or cancel streams before shutting down the tracer provider. Web stream reader, pipe, and tee methods remain available when supplied by your SDK version.

Stream output capture uses a bounded 65,536-character budget for event JSON and assembled content. Longer output is marked with `mistral.output.truncated`; final token usage is still recorded.

## Control captured content

```typescript
const instrumentation = new MistralInstrumentation({
  traceConfig: {
    hideInputs: true,
    hideOutputs: true,
    hideEmbeddingVectors: true,
  },
});
instrumentation.setTracerProvider(provider);
```

These settings omit message content, tool payloads, and embedding data while preserving operational metadata and token usage. Content-hiding settings also hide exception details by default. The corresponding `FI_HIDE_*` environment variables are supported through `@traceai/fi-core`.

## Supported operations

When present in your SDK version:

- `chat.complete`, `chat.stream`, `chat.parse`, and `chat.parseStream`
- `fim.complete` and `fim.stream`
- `agents.complete` and `agents.stream`
- `embeddings.create`
- `beta.conversations.start`, `append`, and `restart`, plus their streaming variants

Tool definitions, tool-call arguments, and tool results are captured as part of the model request or response. This package does not execute tools or create spans for your application's tool functions. Administrative APIs and the SDK's separate Responses, audio, and realtime clients are not instrumented.

## License

Apache-2.0
