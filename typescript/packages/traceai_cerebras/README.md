# @traceai/cerebras

Trace native Cerebras Cloud SDK chat and text completions with OpenTelemetry, including streaming responses, token usage, and server timing attributes.

## Install

```bash
npm install @traceai/cerebras @cerebras/cerebras_cloud_sdk
```

The instrumentor supports `@cerebras/cerebras_cloud_sdk` 1.x from 1.0.0. Text completions are traced when your SDK exposes `client.completions.create`.

## Configure tracing

Use your application's existing OpenTelemetry tracer provider. For a standalone example that prints spans to the console, install the OpenTelemetry 1.x provider packages used below:

```bash
npm install @opentelemetry/sdk-trace-node@^1.30.0 @opentelemetry/sdk-trace-base@^1.30.0
```

Set `CEREBRAS_API_KEY` and set `CEREBRAS_MODEL` to a model available in your Cerebras account.

```typescript
import { NodeTracerProvider } from "@opentelemetry/sdk-trace-node";
import { SimpleSpanProcessor, ConsoleSpanExporter } from "@opentelemetry/sdk-trace-base";
import { CerebrasInstrumentation } from "@traceai/cerebras";
import * as cerebrasModule from "@cerebras/cerebras_cloud_sdk";

const provider = new NodeTracerProvider({
  spanProcessors: [new SimpleSpanProcessor(new ConsoleSpanExporter())],
});
provider.register();

const instrumentation = new CerebrasInstrumentation();
instrumentation.setTracerProvider(provider);
instrumentation.manuallyInstrument(cerebrasModule);

const client = new cerebrasModule.Cerebras({
  apiKey: process.env.CEREBRAS_API_KEY,
});
const model = process.env.CEREBRAS_MODEL;
if (!model) throw new Error("Set CEREBRAS_MODEL to a model available in your account");

const response = await client.chat.completions.create({
  model,
  messages: [{ role: "user", content: "Explain wafer-scale computing in one sentence." }],
});
if ("choices" in response) {
  const choice = response.choices?.[0];
  if (choice && "message" in choice) {
    console.log(choice.message?.content ?? "");
  }
}
await provider.forceFlush();
```

Call `manuallyInstrument()` when the SDK is already imported, including ESM applications. CommonJS applications can instead initialize the instrumentor before requiring the SDK. Use one `CerebrasInstrumentation` instance for your application; calling `manuallyInstrument()` again does not duplicate spans. `disable()` restores the SDK methods, and `enable()` instruments them again.

For an application already exporting traces to Future AGI, pass its provider to `setTracerProvider()` rather than creating another provider. This package emits traces; timing values are span attributes, not an OpenTelemetry metrics stream.

## Stream a response

```typescript
const stream = await client.chat.completions.create({
  model,
  messages: [{ role: "user", content: "Count to five." }],
  stream: true,
});

for await (const chunk of stream) {
  if ("choices" in chunk) {
    const choice = chunk.choices?.[0];
    if (choice && "delta" in choice) {
      process.stdout.write(choice.delta?.content ?? "");
    }
  }
}
```

The span ends when the stream finishes, fails, or is cancelled. The SDK's `controller`, `tee()`, and `toReadableStream()` remain available. Consume the stream or cancel it with `stream.controller.abort()` to finish the span. Usage and timing attributes are recorded when the SDK response includes them.

## Use response helpers

The SDK's `APIPromise` helpers remain available:

```typescript
const { data, response: httpResponse } = await client.chat.completions.create({
  model,
  messages: [{ role: "user", content: "Hello." }],
}).withResponse();
```

`withResponse()` captures parsed response attributes. If you choose `asResponse()` instead, the instrumentor leaves the raw response body unread and ends the span without parsed output, usage, or server timing attributes. Use `withResponse()` when you need both the raw HTTP response and those attributes.

## Hide request and response content

Pass `traceConfig` when creating the instrumentor:

```typescript
const instrumentation = new CerebrasInstrumentation({
  traceConfig: { hideInputs: true, hideOutputs: true },
});
```

These options hide payload content while retaining model, provider, token usage, finish reasons, and timing attributes. You can also set `FI_HIDE_INPUTS`, `FI_HIDE_OUTPUTS`, and `FI_HIDE_ERROR_DETAILS` environment variables.

For this integration, `hideInputMessages`, `hideInputText`, and `hideInputImages` omit all input payload attributes and invocation parameters, which can contain tool schemas or stop text. `hideOutputMessages` and `hideOutputText` omit all output payload attributes and prevent stream content from being buffered for tracing. This also removes duplicate raw payloads rather than leaving content in another attribute. The corresponding `FI_HIDE_INPUT_MESSAGES`, `FI_HIDE_INPUT_TEXT`, `FI_HIDE_INPUT_IMAGES`, `FI_HIDE_OUTPUT_MESSAGES`, and `FI_HIDE_OUTPUT_TEXT` environment variables are supported.

## Captured attributes

| Attribute | Value |
|-----------|-------|
| `fi.span.kind` | `LLM` |
| `gen_ai.provider.name` | `cerebras` |
| `gen_ai.operation.name` | `chat` or `text_completion` |
| `gen_ai.request.model` | Requested model |
| `gen_ai.response.model` | Model returned by Cerebras |
| `gen_ai.usage.input_tokens` | Input token count, when returned |
| `gen_ai.usage.output_tokens` | Output token count, when returned |
| `gen_ai.usage.total_tokens` | Total token count, when returned |
| `cerebras.queue_time` | Queue time in seconds, when returned |
| `cerebras.prompt_time` | Prompt processing time in seconds, when returned |
| `cerebras.completion_time` | Completion generation time in seconds, when returned |
| `cerebras.total_time` | Total server time in seconds, when returned |

## License

Apache-2.0
