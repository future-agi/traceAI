# @traceai/xai

OpenTelemetry instrumentation for xAI (Grok) - AI models from xAI.

## Installation

```bash
npm install @traceai/xai openai
```

The `openai` package is an optional peer dependency (minimum 4.0.0). The
instrumentation supports both the v4 resource layout and the latest v7 layout;
OpenAI SDK 7.47.0 requires Node.js 22+, while older compatible SDK versions
can still be used on Node.js 18+. Configure the OpenAI client with an xAI
`baseURL` (such as `https://api.x.ai/v1`); calls to non-xAI hosts are not traced
by this package. Embedding spans are recorded only when the endpoint supports
an embedding model.

## Features

- Automatic tracing of xAI API calls through the OpenAI-compatible interface (OpenAI JS SDK 4.x–7.x, including 7.47.0)
- Support for chat completions and embeddings
- Streaming response support
- Tool/function calling support
- Token usage tracking
- Full OpenTelemetry semantic conventions compliance

## Usage

### Basic Setup

```typescript
import { NodeTracerProvider } from "@opentelemetry/sdk-trace-node";
import { SimpleSpanProcessor, ConsoleSpanExporter } from "@opentelemetry/sdk-trace-base";
import { XAIInstrumentation } from "@traceai/xai";
import OpenAI from "openai";

// Set up the tracer provider
const provider = new NodeTracerProvider();
provider.addSpanProcessor(new SimpleSpanProcessor(new ConsoleSpanExporter()));
provider.register();

// Create and enable the instrumentation
const instrumentation = new XAIInstrumentation();
instrumentation.setTracerProvider(provider);
instrumentation.enable();

// Manually instrument the OpenAI module
const openaiModule = await import("openai");
instrumentation.manuallyInstrument(openaiModule);

// Supply an API key and a model ID available to your xAI account.
const apiKey = process.env.XAI_API_KEY;
const model = process.env.XAI_MODEL;
if (!apiKey || !model) {
  throw new Error("Set XAI_API_KEY and XAI_MODEL");
}
const client = new OpenAI({
  baseURL: "https://api.x.ai/v1",
  apiKey,
});

// Calls against the xAI base URL are traced.
const response = await client.chat.completions.create({
  model,
  messages: [
    { role: "system", content: "You are Grok, a witty AI assistant." },
    { role: "user", content: "Hello!" },
  ],
});
```

### Streaming Responses

```typescript
const stream = await client.chat.completions.create({
  model,
  messages: [{ role: "user", content: "Tell me a joke." }],
  stream: true,
});

for await (const chunk of stream) {
  process.stdout.write(chunk.choices[0]?.delta?.content || "");
}
```

### Tool Calling

```typescript
const response = await client.chat.completions.create({
  model,
  messages: [
    { role: "user", content: "What's the current time in Tokyo?" },
  ],
  tools: [
    {
      type: "function",
      function: {
        name: "get_current_time",
        description: "Get the current time in a specific timezone",
        parameters: {
          type: "object",
          properties: {
            timezone: {
              type: "string",
              description: "IANA timezone (e.g., 'Asia/Tokyo')"
            },
          },
          required: ["timezone"],
        },
      },
    },
  ],
  tool_choice: "auto",
});

// Handle tool calls
const toolCall = response.choices[0]?.message?.tool_calls?.[0];
if (toolCall) {
  console.log(`Tool: ${toolCall.function.name}`);
  console.log(`Args: ${toolCall.function.arguments}`);
}
```

### Embeddings

```typescript
const embedding = await client.embeddings.create({
  // Set this only if your xAI endpoint offers an embedding model.
  model: process.env.XAI_EMBEDDING_MODEL!,
  input: "Hello, world!",
});

console.log(`Embedding dimensions: ${embedding.data[0].embedding.length}`);
```

### Multi-Turn Conversations

```typescript
const response = await client.chat.completions.create({
  model,
  messages: [
    { role: "system", content: "You are Grok." },
    { role: "user", content: "My name is Alice." },
    { role: "assistant", content: "Nice to meet you, Alice!" },
    { role: "user", content: "What's my name?" },
  ],
});
```

## Configuration Options

| Option | Type | Description |
|--------|------|-------------|
| `instrumentationConfig` | `InstrumentationConfig` | OpenTelemetry instrumentation config |
| `traceConfig` | `TraceConfigOptions` | TraceAI config (hideInputs, hideOutputs, etc.) |

## Captured Attributes

| Attribute | Description |
|-----------|-------------|
| `fi.span.kind` | `LLM` for chat, `EMBEDDING` for embeddings |
| `gen_ai.provider.name` | `xai` |
| `gen_ai.request.model` | Requested chat model |
| `gen_ai.input.messages` | JSON-encoded chat messages |
| `gen_ai.output.messages` | JSON-encoded response message and tool calls |
| `gen_ai.usage.input_tokens` | Input token count (when returned) |
| `gen_ai.usage.output_tokens` | Output token count (when returned) |
| `gen_ai.usage.total_tokens` | Total token count (when returned) |

Use a model ID currently available to your xAI account. Available models and
embedding support depend on the endpoint and account configuration.

## License

Apache-2.0
