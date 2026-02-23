# TypeScript Instrumentations

Framework-specific instrumentation packages for TypeScript.

## Available Packages

| Package | Framework | Version Support |
|---------|-----------|-----------------|
| `@traceai/openai` | OpenAI | ^4.0.0 |
| `@traceai/anthropic` | Anthropic | >=0.20.0 <1 |
| `@traceai/langchain` | LangChain | ^0.1.0, ^0.2.0, ^0.3.0 |
| `@traceai/llamaindex` | LlamaIndex | - |
| `@traceai/bedrock` | AWS Bedrock | - |
| `@traceai/vercel` | Vercel AI SDK | - |
| `@traceai/mastra` | Mastra | - |
| `@traceai/mcp` | Model Context Protocol | - |

## Installation

```bash
# Install core and your frameworks
npm install @traceai/fi-core @traceai/openai @opentelemetry/instrumentation

# Or multiple frameworks
npm install @traceai/fi-core @traceai/openai @traceai/anthropic @traceai/langchain
```

## Basic Usage Pattern

All instrumentations follow the same pattern:

```typescript
import { register, ProjectType } from "@traceai/fi-core";
import { XInstrumentation } from "@traceai/x";
import { registerInstrumentations } from "@opentelemetry/instrumentation";

// 1. Register provider FIRST
const provider = register({
    projectName: "my_app",
    projectType: ProjectType.OBSERVE,
});

// 2. Register instrumentations BEFORE importing SDK
registerInstrumentations({
    tracerProvider: provider,
    instrumentations: [new XInstrumentation()],
});

// 3. NOW import and use the SDK
import X from "x";
const client = new X();
```

**Important**: Register instrumentations BEFORE creating framework clients!

## @traceai/openai

```bash
npm install @traceai/openai openai
```

```typescript
import { OpenAIInstrumentation } from "@traceai/openai";

const instrumentation = new OpenAIInstrumentation({
    traceConfig: {
        hideInputs: false,
        hideOutputs: false,
    }
});

registerInstrumentations({
    tracerProvider: provider,
    instrumentations: [instrumentation],
});

// Now use OpenAI
import OpenAI from "openai";
const openai = new OpenAI();

const response = await openai.chat.completions.create({
    model: "gpt-4",
    messages: [{ role: "user", content: "Hello!" }],
});
```

### Supported Operations

- `chat.completions.create()` - Chat completions
- `completions.create()` - Legacy completions
- `embeddings.create()` - Embeddings
- `responses.create()` - Responses API
- Streaming for all above

### Helper Functions

```typescript
import { isPatched } from "@traceai/openai";

// Check if OpenAI is instrumented
if (isPatched()) {
    console.log("OpenAI is being traced");
}
```

## @traceai/anthropic

```bash
npm install @traceai/anthropic @anthropic-ai/sdk
```

```typescript
import { AnthropicInstrumentation } from "@traceai/anthropic";

registerInstrumentations({
    tracerProvider: provider,
    instrumentations: [new AnthropicInstrumentation()],
});

import Anthropic from "@anthropic-ai/sdk";
const anthropic = new Anthropic();

const response = await anthropic.messages.create({
    model: "claude-3-opus-20240229",
    max_tokens: 1024,
    messages: [{ role: "user", content: "Hello!" }],
});
```

### Supported Operations

- `messages.create()` - Messages API
- `messages.stream()` - Streaming messages
- Tool use and function calling

## @traceai/langchain

```bash
npm install @traceai/langchain @langchain/core @langchain/openai
```

```typescript
import { LangChainInstrumentation } from "@traceai/langchain";

const instrumentation = new LangChainInstrumentation({
    tracerProvider: provider,
});

registerInstrumentations({
    tracerProvider: provider,
    instrumentations: [instrumentation],
});

import { ChatOpenAI } from "@langchain/openai";
import { ChatPromptTemplate } from "@langchain/core/prompts";

const model = new ChatOpenAI({ model: "gpt-4" });
const prompt = ChatPromptTemplate.fromTemplate("Tell me about {topic}");
const chain = prompt.pipe(model);

const response = await chain.invoke({ topic: "AI" });
```

### Manual Instrumentation

For some LangChain versions, use manual instrumentation:

```typescript
import * as langchainCore from "@langchain/core";

instrumentation.manuallyInstrument(langchainCore);
```

## @traceai/bedrock

```bash
npm install @traceai/bedrock @aws-sdk/client-bedrock-runtime
```

```typescript
import { BedrockInstrumentation } from "@traceai/bedrock";

registerInstrumentations({
    tracerProvider: provider,
    instrumentations: [new BedrockInstrumentation()],
});

import { BedrockRuntimeClient, InvokeModelCommand } from "@aws-sdk/client-bedrock-runtime";

const client = new BedrockRuntimeClient({ region: "us-east-1" });
const response = await client.send(new InvokeModelCommand({
    modelId: "anthropic.claude-v2",
    body: JSON.stringify({
        prompt: "Hello!",
        max_tokens_to_sample: 100,
    }),
}));
```

## @traceai/vercel

```bash
npm install @traceai/vercel ai
```

```typescript
import { VercelAIInstrumentation } from "@traceai/vercel";

registerInstrumentations({
    tracerProvider: provider,
    instrumentations: [new VercelAIInstrumentation()],
});

import { generateText } from "ai";
import { openai } from "@ai-sdk/openai";

const { text } = await generateText({
    model: openai("gpt-4"),
    prompt: "Hello!",
});
```

## Multiple Frameworks

Instrument multiple frameworks together:

```typescript
import { register, ProjectType } from "@traceai/fi-core";
import { OpenAIInstrumentation } from "@traceai/openai";
import { AnthropicInstrumentation } from "@traceai/anthropic";
import { LangChainInstrumentation } from "@traceai/langchain";
import { registerInstrumentations } from "@opentelemetry/instrumentation";

const provider = register({
    projectName: "multi_llm_app",
    projectType: ProjectType.OBSERVE,
});

registerInstrumentations({
    tracerProvider: provider,
    instrumentations: [
        new OpenAIInstrumentation(),
        new AnthropicInstrumentation(),
        new LangChainInstrumentation(),
    ],
});
```

## TraceConfig Options

All instrumentations support TraceConfig:

```typescript
interface TraceConfigOptions {
    hideInputs?: boolean;         // Hide all input data
    hideOutputs?: boolean;        // Hide all output data
    hideInputMessages?: boolean;  // Hide input messages
    hideOutputMessages?: boolean; // Hide output messages
    hideInputImages?: boolean;    // Hide images
    hideInputText?: boolean;      // Hide text content
    hideOutputText?: boolean;     // Hide output text
    hideEmbeddingVectors?: boolean; // Hide vectors
    base64ImageMaxLength?: number; // Max image size
}
```

Example:

```typescript
const instrumentation = new OpenAIInstrumentation({
    traceConfig: {
        hideInputs: true,
        hideOutputs: true,
    }
});
```

## Captured Attributes

### LLM Spans

| Attribute | Description |
|-----------|-------------|
| `llm.model_name` | Model identifier |
| `llm.provider` | Provider name |
| `llm.input_messages` | Input messages |
| `llm.output_messages` | Output messages |
| `llm.token_count.prompt` | Input tokens |
| `llm.token_count.completion` | Output tokens |
| `llm.invocation_parameters` | Model parameters |
| `llm.tools` | Available tools |

### Common Attributes

| Attribute | Description |
|-----------|-------------|
| `fi.span_kind` | Span type (LLM, AGENT, etc.) |
| `input.value` | Raw input |
| `output.value` | Raw output |

## Checking Instrumentation Status

```typescript
import { isPatched as isOpenAIPatched } from "@traceai/openai";
import { isPatched as isAnthropicPatched } from "@traceai/anthropic";

console.log("OpenAI patched:", isOpenAIPatched());
console.log("Anthropic patched:", isAnthropicPatched());
```

## Disabling Instrumentation

```typescript
const instrumentation = new OpenAIInstrumentation({
    instrumentationConfig: {
        enabled: false, // Disable
    }
});
```

## Related

- [fi-core Reference](fi-core.md)
- [TypeScript Quickstart](../getting-started/quickstart-typescript.md)
- [TraceConfig](../configuration/trace-config.md)
