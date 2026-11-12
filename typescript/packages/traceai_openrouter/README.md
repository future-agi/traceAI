# OpenRouter TypeScript tracing

Trace the official `@openrouter/sdk` 1.5.33+ inference calls through its supported custom HTTP client. Install the adapter, the SDK and `@traceai/fi-core`, which sets up the exporter to Future AGI:

```sh
npm install @traceai/openrouter @openrouter/sdk @traceai/fi-core
```

Set `FI_API_KEY`, `FI_SECRET_KEY` and `OPENROUTER_API_KEY`, then:

```ts
import { register, ProjectType } from "@traceai/fi-core";
import { OpenRouter } from "@openrouter/sdk";
import { createOpenRouterTracingClient } from "@traceai/openrouter";

const tracerProvider = register({ projectName: "openrouter-demo", projectType: ProjectType.OBSERVE });
const openrouter = new OpenRouter({
  apiKey: process.env.OPENROUTER_API_KEY,
  httpClient: createOpenRouterTracingClient({ tracer: tracerProvider.getTracer("@traceai/openrouter") }),
});
const result = await openrouter.chat.send({
  chatRequest: { model: "openai/gpt-4o-mini", messages: [{ role: "user", content: "Hello" }] },
});
```

Call `createOpenRouterTracingClient({ fetcher })` when using a custom transport. This adapter observes chat, embeddings, rerank, both Responses namespaces and requests made by `callModel`; it never captures prompts, completion content or raw provider errors. Streaming spans finish on stream completion or cancellation; metadata is best-effort. OpenAI-compatible client calls use `@traceai/openai` instead; OpenRouter Agent lifecycle hooks and Broadcast OTLP are separate integrations. Do not configure multiple LLM instrumentation layers for the same request.
