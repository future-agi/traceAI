# @traceai/cohere

Trace Cohere chat, streaming chat, embeddings and reranking in Future AGI with the maintained Cohere SDK instrumentor.

## Install

```bash
npm install @traceai/cohere @traceai/fi-core cohere-ai
```

Use Node.js 18 or later and `cohere-ai` 7.x or 8.x. Use `cohere-ai` 8.3.0 for the v2 examples below. The 7.0.0 SDK only exposes the older v1 operations; streaming requires a version with `chatStream`.

## Configure tracing

Set `FI_API_KEY`, `FI_SECRET_KEY` and `COHERE_API_KEY` in your environment. Get your Future AGI credentials from your organization's settings.

Initialize tracing **before constructing any Cohere clients**. Explicit module instrumentation works with both CommonJS and native ESM, without an import hook or copied tracing helpers.

```typescript
import { register, ProjectType } from "@traceai/fi-core";
import { CohereInstrumentation } from "@traceai/cohere";
import * as Cohere from "cohere-ai";

const provider = register({
  projectName: "cohere-app",
  projectType: ProjectType.OBSERVE,
});

const instrumentation = new CohereInstrumentation({
  traceConfig: {
    hideInputs: true,
    hideOutputs: true,
    hideEmbeddingVectors: true,
  },
});
instrumentation.setTracerProvider(provider);
instrumentation.manuallyInstrument(Cohere);

const client = new Cohere.CohereClientV2({
  token: process.env.COHERE_API_KEY,
});

try {
  const response = await client.chat({
    model: "command-r-08-2024",
    messages: [{ role: "user", content: "Explain semantic search in one sentence." }],
  });
  console.log(response.message);
} finally {
  instrumentation.disable();
  await provider.shutdown();
}
```

Open the **cohere-app** project in Future AGI to inspect the `Cohere Chat` span, model and token usage. This example hides request/response content, embedding vectors and error details. To inspect content in traces, explicitly turn off the corresponding hiding options only when it is safe for your application.

If you already have an OpenTelemetry tracer provider, pass it to `setTracerProvider` instead of creating a second provider. For CommonJS auto-instrumentation, register `CohereInstrumentation` with OpenTelemetry before requiring `cohere-ai`; do not also install another Cohere instrumentor.

## Stream a response

With the same initialized client, replace the chat call with:

```typescript
const stream = await client.chatStream({
  model: "command-r-08-2024",
  messages: [{ role: "user", content: "Explain semantic search in one sentence." }],
});

for await (const event of stream) {
  if (event.type === "content-start" || event.type === "content-delta") {
    process.stdout.write(event.delta?.message?.content?.text ?? "");
  }
}
```

The span stays open while you consume the stream. It finishes at the terminal event, iterator completion, failure, early return or controller abort. Breaking or returning from the iterator, aborting its controller, or receiving `USER_CANCEL` sets `cohere.stream.cancelled` and leaves the span status unset. A stream that ends without a terminal event is an error, not a cancellation.

For both chat and streaming chat, `ERROR`, `ERROR_TOXIC`, `ERROR_LIMIT` and `TIMEOUT` finish reasons mark the span as an error. A nonstreaming `USER_CANCEL` response sets `cohere.request.cancelled` with an unset status. Partial generated output is retained when content capture is enabled; token counts are available only if Cohere sends usage. These telemetry statuses do not change SDK results or turn resolved responses into exceptions.

Consume the stream or explicitly close/abort it rather than abandoning it. SDK stream controllers, events and `withRawResponse()` remain available.

## Embed and rerank

```typescript
await client.embed({
  model: "embed-english-v3.0",
  texts: ["Paris is the capital of France."],
  inputType: "search_document",
  embeddingTypes: ["float"],
});

await client.rerank({
  model: "rerank-v3.5",
  query: "What is the capital of France?",
  documents: ["Paris is the capital of France.", "Berlin is the capital of Germany."],
  topN: 1,
});
```

Embeddings use the `EMBEDDING` span kind; reranking uses `RERANKER`, including result indices and relevance scores. Chat uses `LLM`. The instrumentor reads the SDK's camelCase properties, including `chatHistory`, `toolCalls`, `inputTokens` and `relevanceScore`, and retains support for v1 payloads.

## Usage, provenance and streaming timing

The following numeric attributes are captured independently of input/output hiding:

| Attribute | Meaning |
| --- | --- |
| `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`, `gen_ai.usage.total_tokens` | Actual tokens when supplied. For older responses without `tokens`, input/output billed units remain the fallback. |
| `gen_ai.usage.cache_read_tokens` | Cohere's supplied `cachedTokens` (cached prompt tokens); not added to the total again. |
| `cohere.usage.billed_units.input_tokens`, `cohere.usage.billed_units.output_tokens` | Separately supplied billable tokens, which can differ from actual tokens. |
| `cohere.usage.billed_units.search_units`, `cohere.usage.billed_units.classifications` | Supplied billable operation units, not token counts. |
| `cohere.usage.billed_units.images`, `cohere.usage.billed_units.image_tokens` | Supplied billable image counts and image tokens, respectively. |
| `cohere.stream.time_to_first_token_ms` | Monotonic elapsed milliseconds from SDK request invocation until the first consumed nonempty generated text, reasoning, tool plan, or tool name/argument fragment. |

Missing usage is not synthesized. First-token timing includes request/headers latency and any delay before the consumer reads the first content event; it is not server-side inference timing. Metadata-only, empty, or cancelled-before-content streams have no timing value. A recorded value survives later cancellation or failure and is not replaced by subsequent chunks.

`gen_ai.response.id` retains the provider generation/response ID, including IDs delivered at stream start, even when content is hidden or the stream later fails or is cancelled.

With output capture enabled, v2 streamed citations are accumulated by index in `gen_ai.output.messages` and `raw.output`, including partial streams. Input hiding retains citation offsets, generated citation text and source IDs/types but removes source document/tool bodies. Output hiding omits citations along with the rest of the generated content. These projections never change the SDK events or responses delivered to your application.

## Existing v1 applications

Keep using `CohereClient`, `CohereInstrumentation` and `manuallyInstrument` with the same setup. Calls through `CohereClient.v2` and `CohereClientV2` are also traced when available in your SDK. Disable/enable applies to subsequent calls, including already-bound v2 methods; in-flight calls finish their spans normally.

This integration covers `chat`, `chatStream`, `embed` and `rerank`, not every endpoint on the Cohere SDK. It does not infer a default model when no model was supplied.

## Troubleshooting

- **No v2 spans:** initialize instrumentation before constructing the client. V2 binds its methods in the constructor; recreate clients constructed before setup.
- **Duplicate spans:** use one Cohere instrumentor per application and remove overlapping instrumentation.
- **A stream span never finishes:** consume the stream, break/return from its iterator, or abort its controller before shutting down the provider.
- **Missing content or vectors:** check your `traceConfig` and `FI_HIDE_*` environment settings. Input/output hiding also applies to Cohere-specific fields and raw payloads; vector hiding excludes the raw embedding response. Connector access tokens are always omitted from telemetry, regardless of content settings; requests to Cohere retain them.

## Development

From the TypeScript workspace, build shared packages and the instrumentor before running its offline suite:

```bash
pnpm --filter '@traceai/cohere...' --workspace-concurrency=1 run build
pnpm --filter '@traceai/cohere' exec jest --runInBand
```

## License

Apache-2.0
