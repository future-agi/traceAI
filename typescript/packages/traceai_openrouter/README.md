# OpenRouter TypeScript tracing

Trace the official `@openrouter/sdk` 1.5.33+ inference calls through its supported custom HTTP client. Install `@traceai/openrouter`, `@openrouter/sdk`, and a configured OpenTelemetry SDK exporter. Configure Future AGI's OTLP HTTP endpoint `https://api.futureagi.com/tracer/v1/traces` with `FI_API_KEY` and `FI_SECRET_KEY` in your exporter; never embed secrets in trace attributes.

```ts
import { OpenRouter } from "@openrouter/sdk";
import { createOpenRouterTracingClient } from "@traceai/openrouter";

const openrouter = new OpenRouter({
  apiKey: process.env.OPENROUTER_API_KEY,
  httpClient: createOpenRouterTracingClient(),
});
const result = await openrouter.chat.send({
  chatRequest: { model: "openai/gpt-4o-mini", messages: [{ role: "user", content: "Hello" }] },
});
```

Call `createOpenRouterTracingClient({ fetcher })` when using a custom transport. This adapter observes chat, embeddings, rerank, both Responses namespaces and requests made by `callModel`; it never captures prompts, completion content or raw provider errors. Streaming spans finish on stream completion or cancellation; metadata is best-effort. OpenAI-compatible client calls use `@traceai/openai` instead; OpenRouter Agent lifecycle hooks and Broadcast OTLP are separate integrations. Do not configure multiple LLM instrumentation layers for the same request.
