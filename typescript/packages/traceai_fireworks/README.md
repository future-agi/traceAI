# @traceai/fireworks

OpenTelemetry instrumentation for Fireworks AI - fast and affordable inference for open-source models.

## Installation

```bash
npm install @traceai/fireworks
```

## Features

- Automatic tracing of Fireworks AI API calls through OpenAI-compatible interface
- Support for chat completions, text completions, and embeddings
- Streaming response support
- Tool/function calling support
- Token usage tracking
- Full OpenTelemetry semantic conventions compliance

## Usage

### Basic Setup

```typescript
import { NodeTracerProvider } from "@opentelemetry/sdk-trace-node";
import { SimpleSpanProcessor, ConsoleSpanExporter } from "@opentelemetry/sdk-trace-base";
import { FireworksInstrumentation } from "@traceai/fireworks";
import OpenAI from "openai";

// Set up the tracer provider
const provider = new NodeTracerProvider();
provider.addSpanProcessor(new SimpleSpanProcessor(new ConsoleSpanExporter()));
provider.register();

// Create and enable the instrumentation
const instrumentation = new FireworksInstrumentation();
instrumentation.setTracerProvider(provider);
instrumentation.enable();

// Manually instrument the OpenAI module
const openaiModule = await import("openai");
instrumentation.manuallyInstrument(openaiModule);

// Create the Fireworks client
const client = new OpenAI({
  baseURL: "https://api.fireworks.ai/inference/v1",
  apiKey: process.env.FIREWORKS_API_KEY,
});

// Make requests - they will be automatically traced
const response = await client.chat.completions.create({
  model: "accounts/fireworks/models/llama-v3p1-8b-instruct",
  messages: [
    { role: "system", content: "You are a helpful assistant." },
    { role: "user", content: "Hello!" },
  ],
});
```

### Streaming Responses

```typescript
const stream = await client.chat.completions.create({
  model: "accounts/fireworks/models/llama-v3p1-8b-instruct",
  messages: [{ role: "user", content: "Count to 5." }],
  stream: true,
});

for await (const chunk of stream) {
  process.stdout.write(chunk.choices[0]?.delta?.content || "");
}
```

### Text Completions

```typescript
const completion = await client.completions.create({
  model: "accounts/fireworks/models/llama-v3p1-8b-instruct",
  prompt: "The quick brown fox",
  max_tokens: 50,
});
```

### Embeddings

```typescript
const embedding = await client.embeddings.create({
  model: "nomic-ai/nomic-embed-text-v1.5",
  input: "Hello, world!",
});

console.log(`Embedding dimensions: ${embedding.data[0].embedding.length}`);
```

### Batch Embeddings

```typescript
const embeddings = await client.embeddings.create({
  model: "nomic-ai/nomic-embed-text-v1.5",
  input: ["Hello", "World", "How are you?"],
});

console.log(`Generated ${embeddings.data.length} embeddings`);
```

## Configuration Options

| Option | Type | Description |
|--------|------|-------------|
| `instrumentationConfig` | `InstrumentationConfig` | OpenTelemetry instrumentation config |
| `traceConfig` | `TraceConfigOptions` | TraceAI config (hideInputs, hideOutputs, etc.) |

## Captured Attributes

| Attribute | Description |
|-----------|-------------|
| `fi.span.kind` | "LLM" for chat/completions, "EMBEDDING" for embeddings |
| `llm.system` | "fireworks" |
| `llm.provider` | "fireworks" |
| `llm.model` | Model name |
| `llm.token_count.prompt` | Input token count |
| `llm.token_count.completion` | Output token count |
| `llm.token_count.total` | Total token count |
| `embedding.model` | Embedding model name |
| `embedding.embeddings.{n}.text` | Input text for embedding |
| `embedding.embeddings.{n}.vector` | Embedding vector (JSON string) |

## Popular Models

| Model | Description |
|-------|-------------|
| `accounts/fireworks/models/llama-v3p1-8b-instruct` | Llama 3.1 8B Instruct |
| `accounts/fireworks/models/llama-v3p1-70b-instruct` | Llama 3.1 70B Instruct |
| `accounts/fireworks/models/mixtral-8x7b-instruct` | Mixtral 8x7B Instruct |
| `nomic-ai/nomic-embed-text-v1.5` | Nomic embedding model |

## License

Apache-2.0
