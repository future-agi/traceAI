# @traceai/deepseek

OpenTelemetry instrumentation for DeepSeek - AI models with advanced reasoning capabilities.

## Installation

```bash
npm install @traceai/deepseek
```

## Features

- Automatic tracing of DeepSeek API calls through OpenAI-compatible interface
- Support for chat completions with streaming
- DeepSeek R1 reasoning content capture
- Prompt cache hit/miss metrics
- Tool/function calling support
- Full OpenTelemetry semantic conventions compliance

## Usage

### Basic Setup

```typescript
import { NodeTracerProvider } from "@opentelemetry/sdk-trace-node";
import { SimpleSpanProcessor, ConsoleSpanExporter } from "@opentelemetry/sdk-trace-base";
import { DeepSeekInstrumentation } from "@traceai/deepseek";
import OpenAI from "openai";

// Set up the tracer provider
const provider = new NodeTracerProvider();
provider.addSpanProcessor(new SimpleSpanProcessor(new ConsoleSpanExporter()));
provider.register();

// Create and enable the instrumentation
const instrumentation = new DeepSeekInstrumentation();
instrumentation.setTracerProvider(provider);
instrumentation.enable();

// Manually instrument the OpenAI module
const openaiModule = await import("openai");
instrumentation.manuallyInstrument(openaiModule);

// Create the DeepSeek client
const client = new OpenAI({
  baseURL: "https://api.deepseek.com/v1",
  apiKey: process.env.DEEPSEEK_API_KEY,
});

// Make requests - they will be automatically traced
const response = await client.chat.completions.create({
  model: "deepseek-chat",
  messages: [
    { role: "system", content: "You are a helpful assistant." },
    { role: "user", content: "Hello!" },
  ],
});
```

### DeepSeek R1 Reasoning Models

DeepSeek R1 models include reasoning content that shows the model's thought process:

```typescript
const response = await client.chat.completions.create({
  model: "deepseek-reasoner",
  messages: [
    { role: "user", content: "What is 15 * 27? Show your reasoning." },
  ],
});

// The reasoning content is captured in span attributes as:
// deepseek.reasoning_content: "15 * 27 = 15 * 20 + 15 * 7 = 300 + 105 = 405"
```

### Streaming Responses

```typescript
const stream = await client.chat.completions.create({
  model: "deepseek-chat",
  messages: [{ role: "user", content: "Count to 5." }],
  stream: true,
});

for await (const chunk of stream) {
  // For R1 models, reasoning_content streams separately
  if (chunk.choices[0]?.delta?.reasoning_content) {
    process.stdout.write(`[Thinking] ${chunk.choices[0].delta.reasoning_content}`);
  }
  if (chunk.choices[0]?.delta?.content) {
    process.stdout.write(chunk.choices[0].delta.content);
  }
}
```

### Tool Calling

```typescript
const response = await client.chat.completions.create({
  model: "deepseek-chat",
  messages: [
    { role: "user", content: "What's the weather in Paris?" },
  ],
  tools: [
    {
      type: "function",
      function: {
        name: "get_weather",
        description: "Get the current weather",
        parameters: {
          type: "object",
          properties: {
            location: { type: "string" },
          },
        },
      },
    },
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
| `fi.span.kind` | Always "LLM" |
| `llm.system` | "deepseek" |
| `llm.provider` | "deepseek" |
| `llm.model` | Model name (deepseek-chat, deepseek-reasoner, etc.) |
| `llm.token_count.prompt` | Input token count |
| `llm.token_count.completion` | Output token count |
| `llm.token_count.total` | Total token count |
| `deepseek.reasoning_content` | R1 model reasoning (when available) |
| `deepseek.prompt_cache_hit_tokens` | Cached tokens (when available) |
| `deepseek.prompt_cache_miss_tokens` | Non-cached tokens (when available) |

## Available Models

| Model | Description |
|-------|-------------|
| `deepseek-chat` | General chat model |
| `deepseek-reasoner` | Advanced reasoning model (R1) |
| `deepseek-coder` | Code-optimized model |

## License

Apache-2.0
