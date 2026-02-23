# @traceai/vllm

OpenTelemetry instrumentation for vLLM - high-throughput and memory-efficient inference engine for LLMs.

## Installation

```bash
npm install @traceai/vllm
```

## Features

- Automatic tracing of vLLM API calls through OpenAI-compatible interface
- Support for chat completions and text completions
- Streaming response support
- Tool/function calling support
- Configurable URL pattern matching for multi-endpoint setups
- Token usage tracking
- Full OpenTelemetry semantic conventions compliance

## Usage

### Basic Setup

```typescript
import { NodeTracerProvider } from "@opentelemetry/sdk-trace-node";
import { SimpleSpanProcessor, ConsoleSpanExporter } from "@opentelemetry/sdk-trace-base";
import { VLLMInstrumentation } from "@traceai/vllm";
import OpenAI from "openai";

// Set up the tracer provider
const provider = new NodeTracerProvider();
provider.addSpanProcessor(new SimpleSpanProcessor(new ConsoleSpanExporter()));
provider.register();

// Create and enable the instrumentation
const instrumentation = new VLLMInstrumentation({
  // Optional: Only trace requests to specific vLLM servers
  baseUrlPattern: "localhost:8000",
});
instrumentation.setTracerProvider(provider);
instrumentation.enable();

// Manually instrument the OpenAI module
const openaiModule = await import("openai");
instrumentation.manuallyInstrument(openaiModule);

// Create the vLLM client
const client = new OpenAI({
  baseURL: "http://localhost:8000/v1",
  apiKey: "not-needed", // vLLM doesn't require API key
});

// Make requests - they will be automatically traced
const response = await client.chat.completions.create({
  model: "meta-llama/Llama-2-7b-chat-hf",
  messages: [
    { role: "system", content: "You are a helpful assistant." },
    { role: "user", content: "Hello!" },
  ],
});
```

### URL Pattern Matching

When running multiple OpenAI-compatible APIs, use `baseUrlPattern` to only trace vLLM requests:

```typescript
// String pattern
const instrumentation = new VLLMInstrumentation({
  baseUrlPattern: "localhost:8000",
});

// RegExp pattern for multiple servers
const instrumentation = new VLLMInstrumentation({
  baseUrlPattern: /vllm\.internal:\d+/,
});
```

### Streaming Responses

```typescript
const stream = await client.chat.completions.create({
  model: "meta-llama/Llama-2-7b-chat-hf",
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
  model: "meta-llama/Llama-2-7b-hf",
  prompt: "The quick brown fox",
  max_tokens: 50,
});
```

## Configuration Options

| Option | Type | Description |
|--------|------|-------------|
| `instrumentationConfig` | `InstrumentationConfig` | OpenTelemetry instrumentation config |
| `traceConfig` | `TraceConfigOptions` | TraceAI config (hideInputs, hideOutputs, etc.) |
| `baseUrlPattern` | `string \| RegExp` | URL pattern to identify vLLM requests |

## Captured Attributes

| Attribute | Description |
|-----------|-------------|
| `fi.span.kind` | Always "LLM" |
| `llm.system` | "vllm" |
| `llm.provider` | "vllm" |
| `llm.model` | Model name |
| `llm.token_count.prompt` | Input token count |
| `llm.token_count.completion` | Output token count |
| `llm.token_count.total` | Total token count |
| `llm.input_messages.{n}.role` | Message role |
| `llm.input_messages.{n}.content` | Message content |
| `llm.output_messages.{n}.role` | Response role |
| `llm.output_messages.{n}.content` | Response content |

## Running vLLM Server

```bash
# Using Docker
docker run --runtime nvidia --gpus all \
  -v ~/.cache/huggingface:/root/.cache/huggingface \
  -p 8000:8000 \
  vllm/vllm-openai:latest \
  --model meta-llama/Llama-2-7b-chat-hf

# Or using Python
pip install vllm
python -m vllm.entrypoints.openai.api_server \
  --model meta-llama/Llama-2-7b-chat-hf \
  --port 8000
```

## License

Apache-2.0
