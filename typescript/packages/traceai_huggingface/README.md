# @traceai/huggingface

OpenTelemetry instrumentation for the [HuggingFace Inference](https://huggingface.co/docs/huggingface.js/inference/README) TypeScript SDK.

## Installation

```bash
npm install @traceai/huggingface
# or
pnpm add @traceai/huggingface
```

## Requirements

- Node.js 18+
- `@huggingface/inference` SDK version >= 2.0.0

## Quick Start

```typescript
import { NodeTracerProvider } from "@opentelemetry/sdk-trace-node";
import { SimpleSpanProcessor, ConsoleSpanExporter } from "@opentelemetry/sdk-trace-base";
import { HuggingFaceInstrumentation } from "@traceai/huggingface";
import { HfInference } from "@huggingface/inference";

// Set up the tracer provider
const provider = new NodeTracerProvider();
provider.addSpanProcessor(new SimpleSpanProcessor(new ConsoleSpanExporter()));
provider.register();

// Create and enable the instrumentation
const instrumentation = new HuggingFaceInstrumentation();
instrumentation.setTracerProvider(provider);
instrumentation.enable();

// Import and patch the HuggingFace module
const hfModule = await import("@huggingface/inference");
instrumentation.manuallyInstrument(hfModule);

// Use HuggingFace as normal - all calls will be traced
const client = new HfInference(process.env.HF_TOKEN);

const response = await client.textGeneration({
  model: "gpt2",
  inputs: "The quick brown fox",
});
```

## Instrumented Methods

This instrumentation automatically traces the following HuggingFace Inference SDK methods:

### Text Generation
- `client.textGeneration()` - Text generation/completion

### Chat Completion
- `client.chatCompletion()` - Chat-style completions
- `client.chatCompletionStream()` - Streaming chat completions

### Embeddings
- `client.featureExtraction()` - Feature extraction (embeddings)

### NLP Tasks
- `client.summarization()` - Text summarization
- `client.translation()` - Language translation
- `client.questionAnswering()` - Extractive question answering

## Trace Attributes

The instrumentation captures the following semantic attributes:

### Common Attributes
- `fi.span_kind` - Type of span (LLM, EMBEDDING)
- `llm.system` - "huggingface"
- `llm.provider` - "huggingface"
- `llm.model_name` - Model name used
- `input.value` - Input content
- `input.mime_type` - Input content type
- `output.value` - Output content
- `output.mime_type` - Output content type
- `raw.input` - Raw request JSON
- `raw.output` - Raw response JSON

### Text Generation Attributes
- `llm.prompts.{index}` - Input prompts
- `llm.invocation_parameters` - Model parameters (temperature, max_new_tokens, etc.)

### Chat Completion Attributes
- `llm.input_messages.{index}.role` - Message role
- `llm.input_messages.{index}.content` - Message content
- `llm.output_messages.{index}.role` - Response message role
- `llm.output_messages.{index}.content` - Response message content

### Token Usage Attributes
- `llm.token_count.prompt` - Input token count
- `llm.token_count.completion` - Output token count
- `llm.token_count.total` - Total token count

### Embedding Attributes
- `embedding.model_name` - Embedding model name
- `embedding.embeddings.{index}.text` - Input text
- `embedding.embeddings.{index}.vector` - Embedding vector

## Configuration

### Instrumentation Options

```typescript
const instrumentation = new HuggingFaceInstrumentation({
  instrumentationConfig: {
    enabled: true, // Enable/disable instrumentation
  },
  traceConfig: {
    hideInputs: false,  // Hide input content in traces
    hideOutputs: false, // Hide output content in traces
  },
});
```

## Streaming Support

The instrumentation fully supports streaming responses with `chatCompletionStream`:

```typescript
const stream = client.chatCompletionStream({
  model: "meta-llama/Llama-2-7b-chat-hf",
  messages: [{ role: "user", content: "Hello!" }],
  max_tokens: 100,
});

for await (const chunk of stream) {
  // Process chunks as they arrive
  console.log(chunk.choices[0]?.delta?.content);
}
// Span is ended with full content captured
```

## Supported Models

HuggingFace hosts thousands of models. Some popular ones for inference include:

**Text Generation:**
- `gpt2`
- `bigscience/bloom`
- `EleutherAI/gpt-neo-2.7B`

**Chat Models:**
- `meta-llama/Llama-2-7b-chat-hf`
- `meta-llama/Llama-2-13b-chat-hf`
- `HuggingFaceH4/zephyr-7b-beta`

**Embedding Models:**
- `sentence-transformers/all-MiniLM-L6-v2`
- `sentence-transformers/all-mpnet-base-v2`
- `BAAI/bge-base-en-v1.5`

**Summarization:**
- `facebook/bart-large-cnn`
- `google/pegasus-xsum`

**Translation:**
- `Helsinki-NLP/opus-mt-en-fr`
- `Helsinki-NLP/opus-mt-en-de`
- `t5-base`

**Question Answering:**
- `deepset/roberta-base-squad2`
- `distilbert-base-cased-distilled-squad`

See the [HuggingFace Model Hub](https://huggingface.co/models) for a complete list.

## Patched Classes

This instrumentation patches both:
- `HfInference` - The main inference client
- `InferenceClient` - Alternative inference client (if present)

## Examples

See the [examples](./examples) directory for complete working examples.

## License

Apache 2.0
