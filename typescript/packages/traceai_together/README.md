# @traceai/together

OpenTelemetry instrumentation for the [Together AI](https://together.ai/) TypeScript SDK.

## Installation

```bash
npm install @traceai/together
# or
pnpm add @traceai/together
```

## Requirements

- Node.js 18+
- `together-ai` SDK version >= 0.6.0 and < 2 (0.5.x is a different SDK without `chat`, `completions` or `embeddings` resources)

## Quick Start

```typescript
import { register, ProjectType } from "@traceai/fi-core";
import { registerInstrumentations } from "@opentelemetry/instrumentation";
import { TogetherInstrumentation } from "@traceai/together";
import Together from "together-ai";

const tracerProvider = register({
  projectType: ProjectType.OBSERVE,
  projectName: "together_project",
});

const instrumentation = new TogetherInstrumentation();
registerInstrumentations({ instrumentations: [instrumentation], tracerProvider });
// Patch the imported SDK explicitly; this works for ESM and CommonJS alike.
instrumentation.manuallyInstrument(Together);

const client = new Together({ apiKey: process.env.TOGETHER_API_KEY });

try {
  const response = await client.chat.completions.create({
    model: "meta-llama/Llama-3-8b-chat-hf",
    messages: [{ role: "user", content: "Hello!" }],
  });
  console.log(response.choices[0]?.message?.content);
} finally {
  await tracerProvider.shutdown();
}
```

The instrumentation wraps the SDK's shared `Chat.Completions`, `Completions` and `Embeddings` resources, so clients created before or after `manuallyInstrument()` are traced. If the installed SDK does not expose those resources, it logs a warning and `isPatched()` stays `false`.

## Instrumented Methods

This instrumentation automatically traces the following Together AI SDK methods:

### Chat Completions
- `client.chat.completions.create()` - Chat completions with support for streaming

### Completions (Legacy)
- `client.completions.create()` - Text completions

### Embeddings
- `client.embeddings.create()` - Text embeddings

## Trace Attributes

The instrumentation captures the following semantic attributes:

### Common Attributes
- `fi.span_kind` - Type of span (LLM, EMBEDDING)
- `llm.system` - "together"
- `llm.provider` - "together"
- `llm.model_name` - Model name used
- `input.value` - Input content
- `input.mime_type` - Input content type
- `output.value` - Output content
- `output.mime_type` - Output content type
- `raw.input` - Raw request JSON
- `raw.output` - Raw response JSON

### Chat Completion Attributes
- `llm.input_messages.{index}.role` - Message role
- `llm.input_messages.{index}.content` - Message content
- `llm.output_messages.{index}.role` - Response message role
- `llm.output_messages.{index}.content` - Response message content
- `llm.invocation_parameters` - Model parameters (temperature, max_tokens, etc.)

### Tool Call Attributes
- `llm.tools.{index}.tool.json_schema` - Tool definitions
- `llm.output_messages.{index}.tool_calls.{index}.id` - Tool call ID
- `llm.output_messages.{index}.tool_calls.{index}.function.name` - Function name
- `llm.output_messages.{index}.tool_calls.{index}.function.arguments.json` - Function arguments

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
const instrumentation = new TogetherInstrumentation({
  instrumentationConfig: {
    enabled: true, // Enable/disable instrumentation
  },
  traceConfig: {
    hideInputs: false,  // Hide prompts, messages, tools, embedding text and non-numeric parameters
    hideOutputs: false, // Hide output content in traces
  },
});
```

## Streaming Support

The instrumentation fully supports streaming responses. When using streaming, the complete response is captured after the stream is consumed:

```typescript
const stream = await client.chat.completions.create({
  model: "meta-llama/Llama-3-8b-chat-hf",
  messages: [{ role: "user", content: "Hello!" }],
  stream: true,
});

for await (const chunk of stream) {
  // Process chunks as they arrive
  console.log(chunk.choices[0]?.delta?.content);
}
// The span ends when the stream finishes, fails or you stop iterating early
```

The span records the aggregated text, tool calls, finish reason and usage rather than every raw chunk. `stream.tee()`, `stream.controller` and `withResponse()` keep working.

## Supported Models

Together AI supports a wide variety of models. Some popular ones include:

**Chat Models:**
- `meta-llama/Llama-3-8b-chat-hf`
- `meta-llama/Llama-3-70b-chat-hf`
- `mistralai/Mixtral-8x7B-Instruct-v0.1`
- `togethercomputer/llama-2-70b-chat`

**Text Completion Models:**
- `togethercomputer/RedPajama-INCITE-7B-Base`
- `togethercomputer/GPT-JT-6B-v1`

**Embedding Models:**
- `togethercomputer/m2-bert-80M-8k-retrieval`
- `BAAI/bge-large-en-v1.5`

See the [Together AI models page](https://api.together.xyz/models) for a complete list.

## Examples

See the [examples](./examples) directory for complete working examples.

## License

Apache 2.0
