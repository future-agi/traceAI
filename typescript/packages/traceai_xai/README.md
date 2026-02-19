# @traceai/xai

OpenTelemetry instrumentation for xAI (Grok) - AI models from xAI.

## Installation

```bash
npm install @traceai/xai
```

## Features

- Automatic tracing of xAI API calls through OpenAI-compatible interface
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

// Create the xAI client
const client = new OpenAI({
  baseURL: "https://api.x.ai/v1",
  apiKey: process.env.XAI_API_KEY,
});

// Make requests - they will be automatically traced
const response = await client.chat.completions.create({
  model: "grok-beta",
  messages: [
    { role: "system", content: "You are Grok, a witty AI assistant." },
    { role: "user", content: "Hello!" },
  ],
});
```

### Streaming Responses

```typescript
const stream = await client.chat.completions.create({
  model: "grok-beta",
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
  model: "grok-beta",
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
  model: "grok-embed",
  input: "Hello, world!",
});

console.log(`Embedding dimensions: ${embedding.data[0].embedding.length}`);
```

### Multi-Turn Conversations

```typescript
const response = await client.chat.completions.create({
  model: "grok-beta",
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
| `fi.span.kind` | "LLM" for chat, "EMBEDDING" for embeddings |
| `llm.system` | "xai" |
| `llm.provider` | "xai" |
| `llm.model` | Model name (grok-beta, etc.) |
| `llm.token_count.prompt` | Input token count |
| `llm.token_count.completion` | Output token count |
| `llm.token_count.total` | Total token count |
| `llm.input_messages.{n}.role` | Message role |
| `llm.input_messages.{n}.content` | Message content |
| `llm.output_messages.{n}.role` | Response role |
| `llm.output_messages.{n}.content` | Response content |
| `llm.output_messages.{n}.tool_calls.{m}.*` | Tool call details |

## Available Models

| Model | Description |
|-------|-------------|
| `grok-beta` | Grok beta model |
| `grok-embed` | Embedding model (availability varies) |

## About Grok

Grok is developed by xAI, founded by Elon Musk. It's designed to be witty, helpful, and have real-time knowledge through X (Twitter) integration.

## License

Apache-2.0
