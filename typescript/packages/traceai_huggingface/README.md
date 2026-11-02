# @traceai/huggingface

Trace text generation, chat, embeddings, summarization, translation, and question answering from the [Hugging Face Inference SDK](https://huggingface.co/docs/huggingface.js/inference/README) with OpenTelemetry.

## Install

The instrumentor supports Node.js 18+ and `@huggingface/inference` 2.0.0+. Methods are available only when your SDK version supports them. The following example uses the current `InferenceClient` API and OpenTelemetry SDK 2.x:

```bash
npm install @traceai/huggingface @huggingface/inference@^4.13.32 \
  @opentelemetry/sdk-trace-node@^2 @opentelemetry/sdk-trace-base@^2
```

## Trace a request

Set `HF_TOKEN` to your Hugging Face token and `HF_MODEL` to a text-generation model available to your account. Configure the tracer provider before creating the wrapped client:

```typescript
import { NodeTracerProvider } from "@opentelemetry/sdk-trace-node";
import { SimpleSpanProcessor, ConsoleSpanExporter } from "@opentelemetry/sdk-trace-base";
import { HuggingFaceInstrumentation } from "@traceai/huggingface";
import * as huggingface from "@huggingface/inference";

const provider = new NodeTracerProvider({
  spanProcessors: [new SimpleSpanProcessor(new ConsoleSpanExporter())],
});
provider.register();

const instrumentation = new HuggingFaceInstrumentation();
instrumentation.setTracerProvider(provider);
const { InferenceClient } = instrumentation.manuallyInstrument(huggingface);
const client = new InferenceClient(process.env.HF_TOKEN);

try {
  const response = await client.textGeneration({
    model: process.env.HF_MODEL!,
    inputs: "The quick brown fox",
    parameters: { max_new_tokens: 30 },
  });
  console.log(response.generated_text);
} finally {
  instrumentation.disable();
  await provider.shutdown();
}
```

Use the module **returned by `manuallyInstrument()`**. ESM namespaces and some older CommonJS SDK exports are read-only; a previously imported constructor cannot be replaced in place. The instrumentor returns a typed module wrapper instead of modifying SDK task methods. Mutable CommonJS exports are also patched in place for compatibility.

With CommonJS, you can alternatively enable instrumentation before the first `require("@huggingface/inference")` to use OpenTelemetry's automatic require hook. Do not combine multiple Hugging Face instrumentor instances on the same client.

### Wrap an existing client

If another part of your application creates the client, use its returned wrapper:

```typescript
const client = instrumentation.instrumentClient(existingClient);
```

`HfInference`, `InferenceClient`, their endpoint constructor aliases, and clients returned by `client.endpoint(url)` are supported. Per-request options such as custom fetch functions and cancellation signals are passed through unchanged. Calling `disable()` stops tracing for already-wrapped clients; `enable()` resumes it.

## Supported tasks

| SDK method | Span kind |
| --- | --- |
| `textGeneration`, `textGenerationStream` | LLM |
| `chatCompletion`, `chatCompletionStream` | LLM |
| `featureExtraction` | EMBEDDING |
| `summarization`, `translation`, `questionAnswering` | LLM |

The same task functions on the returned module are instrumented. Other SDK methods pass through without a task span.

## Stream responses

Both streaming methods return async iterators immediately, just like the SDK:

```typescript
const stream = client.chatCompletionStream({
  model: process.env.HF_CHAT_MODEL!,
  messages: [{ role: "user", content: "Count to three." }],
  max_tokens: 32,
});

for await (const chunk of stream) {
  process.stdout.write(chunk.choices[0]?.delta?.content ?? "");
}
```

The span ends when iteration finishes, fails, or exits early with `break`. A stream you never iterate does not start a span. Chat token counts are recorded when the SDK response includes usage; they are not estimated.

## Privacy

Inputs and outputs are captured by default. Set privacy controls when creating the instrumentor:

```typescript
const instrumentation = new HuggingFaceInstrumentation({
  traceConfig: {
    hideInputs: true,
    hideOutputs: true,
  },
});
```

These controls apply to exported input/output content, text prompts, embedding input text, and embedding output vectors. Transport options and access tokens are not included in request attributes. Your application still sends its original input to Hugging Face.

## Trace attributes

Spans include `fi.span_kind`, `gen_ai.provider.name`, the request model when supplied, and task inputs/outputs unless hidden. Chat spans include `gen_ai.input.messages`, `gen_ai.output.messages`, response metadata and `gen_ai.usage.*` counts when returned by the SDK. Embedding spans include the embedding model, input texts, and vectors unless hidden.

## Troubleshooting

- **Requests succeed but no spans appear:** use the returned module or client wrapper, set the tracer provider, and check that instrumentation is enabled.
- **ESM imports or previously imported clients are untraced:** create the client from the returned module, or pass the existing client to `instrumentClient()`.
- **A streaming span is missing:** consume the iterator or close it after starting iteration. Flush or shut down the tracer provider before exiting the process.
- **A model rejects a task:** choose a model and inference provider that support that task. Instrumentation does not change model routing or account access.

## License

Apache 2.0
