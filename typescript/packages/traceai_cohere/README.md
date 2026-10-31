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
  if (event.type === "content-delta") {
    process.stdout.write(event.delta?.message?.content?.text ?? "");
  }
}
```

The span stays open while you consume the stream. It finishes at the terminal event, iterator completion, failure, early return or controller abort. A partial stream is marked `cohere.stream.cancelled`; token counts are only available if Cohere sends usage. Consume the stream or explicitly close/abort it rather than abandoning it. SDK stream controllers, events and `withRawResponse()` remain available.

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

## Existing v1 applications

Keep using `CohereClient`, `CohereInstrumentation` and `manuallyInstrument` with the same setup. Calls through `CohereClient.v2` and `CohereClientV2` are also traced when available in your SDK. Disable/enable applies to subsequent calls, including already-bound v2 methods; in-flight calls finish their spans normally.

This integration covers `chat`, `chatStream`, `embed` and `rerank`, not every endpoint on the Cohere SDK. It does not infer a default model when no model was supplied.

## Troubleshooting

- **No v2 spans:** initialize instrumentation before constructing the client. V2 binds its methods in the constructor; recreate clients constructed before setup.
- **Duplicate spans:** use one Cohere instrumentor per application and remove overlapping instrumentation.
- **A stream span never finishes:** consume the stream, break/return from its iterator, or abort its controller before shutting down the provider.
- **Missing content or vectors:** check your `traceConfig` and `FI_HIDE_*` environment settings. Input/output hiding also applies to Cohere-specific fields and raw payloads; vector hiding excludes the raw embedding response.

## Development

From the TypeScript workspace, build shared packages and the instrumentor before running its offline suite:

```bash
pnpm --filter '@traceai/cohere...' --workspace-concurrency=1 run build
pnpm --filter '@traceai/cohere' exec jest --runInBand
```

## License

Apache-2.0
