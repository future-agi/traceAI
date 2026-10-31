# @traceai/deepseek

Trace DeepSeek Chat Completions made through the OpenAI JavaScript SDK, including streamed replies, reasoning content, tool calls and cache token counts.

## Install

```bash
npm install @traceai/deepseek openai
```

Use your application's existing OpenTelemetry provider and exporter. For the standalone console example below, also install:

```bash
npm install @opentelemetry/sdk-trace-node@^1.30.1 @opentelemetry/sdk-trace-base@^1.30.1
```

Set `DEEPSEEK_API_KEY` to your DeepSeek API key. The integration traces Chat Completions sent to `api.deepseek.com`; other hosts, including proxies and lookalike domain names, are left untouched. This package does not instrument the Responses API or DeepSeek Harness.

Register only one OpenAI-compatible instrumentor for a given OpenAI SDK module. Do not register both `DeepSeekInstrumentation` and `OpenAIInstrumentation` for that module.

## Trace a request

```typescript
import { NodeTracerProvider } from "@opentelemetry/sdk-trace-node";
import { SimpleSpanProcessor, ConsoleSpanExporter } from "@opentelemetry/sdk-trace-base";
import { DeepSeekInstrumentation } from "@traceai/deepseek";
import * as openai from "openai";

const provider = new NodeTracerProvider({
  spanProcessors: [new SimpleSpanProcessor(new ConsoleSpanExporter())],
});
provider.register();

const instrumentation = new DeepSeekInstrumentation({
  traceConfig: { hideInputs: true, hideOutputs: true },
});
instrumentation.setTracerProvider(provider);
// Explicit registration works with ESM and with an SDK imported before setup.
instrumentation.manuallyInstrument(openai);

const client = new openai.OpenAI({
  baseURL: "https://api.deepseek.com/v1",
  apiKey: process.env.DEEPSEEK_API_KEY,
});

try {
  const response = await client.chat.completions.create({
    model: "deepseek-chat",
    messages: [{ role: "user", content: "Explain prompt caching in one sentence." }],
  });
  console.log(response.choices[0]?.message.content);
} finally {
  instrumentation.disable();
  await provider.shutdown();
}
```

The console exporter prints a `DeepSeek Chat Completions` span. In an application already configured to export to Future AGI, pass its provider to `setTracerProvider` instead of creating another one. Open the request's trace to see its model, status and token counts.

The example hides request and response content in telemetry. It does not change the response returned to your application or redact your application's own logs. Remove the hiding options only when you want to capture message content, tool schemas, tool arguments and reasoning.

## Stream a reply

Use the same client and instrumentation. Consume the stream before shutting down the provider:

```typescript
const stream = await client.chat.completions.create({
  model: "deepseek-reasoner",
  messages: [{ role: "user", content: "What is 15 times 27?" }],
  stream: true,
});

for await (const chunk of stream) {
  const content = chunk.choices[0]?.delta?.content;
  if (content) process.stdout.write(content);
}
```

The span ends when you finish consuming the stream, break out of iteration, or cancel with `stream.controller.abort()`. The original SDK stream and its available helpers are preserved; instrumentation does not consume a second copy in the background. Cancel streams you decide not to consume.

The SDK's `withResponse()` and `asResponse()` helpers remain available. `asResponse()` leaves the raw response body to you, so a raw-only request records request metadata and status rather than parsed output or token usage.

## Control captured content

Pass standard `TraceConfigOptions` through `traceConfig`:

- `hideInputs`, `hideInputMessages`, `hideInputText` or `hideInputImages`: omit request content and its raw-body, tool-schema and invocation-parameter copies.
- `hideOutputs`, `hideOutputMessages` or `hideOutputText`: omit response content, reasoning, tool arguments and raw response copies, including streamed content.
- Input/output, text and message hiding also hide error details by default, because provider errors can quote your messages. Set `hideErrorDetails: true` explicitly when using only `hideInputImages`.

These options control telemetry, not the values your application receives. The corresponding `FI_HIDE_*` environment settings are also supported.

## Span attributes

| Attribute | Value |
| --- | --- |
| `fi.span.kind` | `LLM` |
| `gen_ai.provider.name` | `deepseek` |
| `gen_ai.request.model` | Model name |
| `gen_ai.response.model` | Model returned by the provider |
| `gen_ai.response.id` | Completion ID |
| `gen_ai.usage.input_tokens` | Input token count, when returned |
| `gen_ai.usage.output_tokens` | Output token count, when returned |
| `gen_ai.usage.total_tokens` | Total token count, when returned |
| `deepseek.reasoning_content` | Reasoning content, when returned and content capture is enabled |
| `deepseek.prompt_cache_hit_tokens` | Cached prompt tokens, when returned |
| `deepseek.prompt_cache_miss_tokens` | Uncached prompt tokens, when returned |

## Troubleshoot missing spans

- **ESM or an SDK imported before setup:** call `manuallyInstrument(openai)` with the same module used to construct the client.
- **A different API host:** this instrumentor only selects `api.deepseek.com`. Use an integration for your actual provider or gateway.
- **A stream remains open:** consume it or cancel its controller before flushing the provider.
- **An already-instrumented warning:** remove the overlapping OpenAI-compatible instrumentor rather than stacking both.

## License

Apache-2.0
