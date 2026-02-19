# @traceai/cerebras

OpenTelemetry instrumentation for Cerebras - ultra-fast inference on Wafer-Scale Engine hardware.

## Installation

```bash
npm install @traceai/cerebras
```

## Features

- Automatic tracing of Cerebras Cloud SDK calls
- Streaming response support
- Cerebras-specific time metrics (queue time, prompt time, completion time)
- Token usage tracking
- Full OpenTelemetry semantic conventions compliance

## Usage

### Basic Setup

```typescript
import { NodeTracerProvider } from "@opentelemetry/sdk-trace-node";
import { SimpleSpanProcessor, ConsoleSpanExporter } from "@opentelemetry/sdk-trace-base";
import { CerebrasInstrumentation } from "@traceai/cerebras";
import Cerebras from "@cerebras/cerebras_cloud_sdk";

// Set up the tracer provider
const provider = new NodeTracerProvider();
provider.addSpanProcessor(new SimpleSpanProcessor(new ConsoleSpanExporter()));
provider.register();

// Create and enable the instrumentation
const instrumentation = new CerebrasInstrumentation();
instrumentation.setTracerProvider(provider);
instrumentation.enable();

// Manually instrument the Cerebras module
const cerebrasModule = await import("@cerebras/cerebras_cloud_sdk");
instrumentation.manuallyInstrument(cerebrasModule);

// Create the Cerebras client
const client = new Cerebras({
  apiKey: process.env.CEREBRAS_API_KEY,
});

// Make requests - they will be automatically traced
const response = await client.chat.completions.create({
  model: "llama3.1-8b",
  messages: [
    { role: "system", content: "You are a helpful assistant." },
    { role: "user", content: "Hello!" },
  ],
});
```

### Performance Metrics

Cerebras provides detailed timing information for analyzing inference performance:

```typescript
const response = await client.chat.completions.create({
  model: "llama3.1-70b",
  messages: [{ role: "user", content: "Explain quantum computing." }],
});

// Time info is captured in span attributes:
// cerebras.queue_time: 0.001 (seconds)
// cerebras.prompt_time: 0.015 (seconds)
// cerebras.completion_time: 0.045 (seconds)
// cerebras.total_time: 0.061 (seconds)

if (response.time_info) {
  const tokensPerSecond = response.usage.completion_tokens / response.time_info.completion_time;
  console.log(`Throughput: ${tokensPerSecond.toFixed(0)} tokens/second`);
}
```

### Streaming Responses

```typescript
const stream = await client.chat.completions.create({
  model: "llama3.1-8b",
  messages: [{ role: "user", content: "Count to 5." }],
  stream: true,
});

for await (const chunk of stream) {
  process.stdout.write(chunk.choices[0]?.delta?.content || "");
}
```

### High-Throughput Inference

Cerebras is optimized for extremely fast inference:

```typescript
const startTime = Date.now();

const response = await client.chat.completions.create({
  model: "llama3.1-70b",
  messages: [
    { role: "user", content: "Write a haiku about AI." },
  ],
  max_tokens: 50,
});

const latency = Date.now() - startTime;
console.log(`End-to-end latency: ${latency}ms`);
console.log(`Server time: ${response.time_info?.total_time}s`);
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
| `llm.system` | "cerebras" |
| `llm.provider` | "cerebras" |
| `llm.model` | Model name (llama3.1-8b, llama3.1-70b) |
| `llm.token_count.prompt` | Input token count |
| `llm.token_count.completion` | Output token count |
| `llm.token_count.total` | Total token count |
| `cerebras.queue_time` | Time spent in queue (seconds) |
| `cerebras.prompt_time` | Time to process prompt (seconds) |
| `cerebras.completion_time` | Time to generate completion (seconds) |
| `cerebras.total_time` | Total server-side time (seconds) |

## Available Models

| Model | Description |
|-------|-------------|
| `llama3.1-8b` | Llama 3.1 8B - fast general-purpose |
| `llama3.1-70b` | Llama 3.1 70B - high capability |

## Why Cerebras?

Cerebras Cloud runs on Wafer-Scale Engine (WSE) hardware, providing:
- Ultra-low latency inference
- High throughput (thousands of tokens/second)
- Consistent performance under load
- No GPU scheduling overhead

## License

Apache-2.0
