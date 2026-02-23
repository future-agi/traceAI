# @traceai/mistral

OpenTelemetry instrumentation for [Mistral AI](https://mistral.ai/) - leading open-weight AI models.

## Features

- Automatic tracing of Mistral Chat Completions
- Embeddings API tracing
- Support for all Mistral models
- Tool/function calling tracing
- Token usage tracking
- Error handling and exception recording

## Installation

```bash
npm install @traceai/mistral
# or
pnpm add @traceai/mistral
# or
yarn add @traceai/mistral
```

## Quick Start

```typescript
import { MistralInstrumentation } from "@traceai/mistral";
import { NodeTracerProvider } from "@opentelemetry/sdk-trace-node";
import { SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { OTLPTraceExporter } from "@opentelemetry/exporter-trace-otlp-proto";
import { Mistral } from "@mistralai/mistralai";

// Initialize OpenTelemetry
const provider = new NodeTracerProvider();
const exporter = new OTLPTraceExporter({
  url: "http://localhost:4318/v1/traces",
});
provider.addSpanProcessor(new SimpleSpanProcessor(exporter));
provider.register();

// Initialize Mistral instrumentation
const mistralInstrumentation = new MistralInstrumentation();
mistralInstrumentation.manuallyInstrument(require("@mistralai/mistralai"));

// Use Mistral as normal - all calls are automatically traced
const mistral = new Mistral({ apiKey: process.env.MISTRAL_API_KEY });

const chatResponse = await mistral.chat.complete({
  model: "mistral-large-latest",
  messages: [
    { role: "user", content: "Explain quantum computing in simple terms" }
  ],
});

console.log(chatResponse.choices[0].message.content);
```

## Configuration

### Basic Configuration

```typescript
const instrumentation = new MistralInstrumentation({
  instrumentationConfig: {
    enabled: true,
  },
});
```

### With Trace Configuration

```typescript
const instrumentation = new MistralInstrumentation({
  instrumentationConfig: {
    enabled: true,
  },
  traceConfig: {
    hideInputs: false,  // Set to true to hide sensitive input data
    hideOutputs: false, // Set to true to hide sensitive output data
  },
});
```

### With TraceAI Core

```typescript
import { register, ProjectType } from "@traceai/fi-core";
import { MistralInstrumentation } from "@traceai/mistral";
import { registerInstrumentations } from "@opentelemetry/instrumentation";

// 1. Register TraceAI Core TracerProvider
const tracerProvider = register({
  projectName: "my-mistral-app",
  projectType: ProjectType.OBSERVE,
  sessionName: "mistral-session-" + Date.now(),
});

// 2. Register instrumentation BEFORE importing Mistral SDK
registerInstrumentations({
  tracerProvider: tracerProvider,
  instrumentations: [new MistralInstrumentation()],
});

// 3. NOW import and use Mistral
const { Mistral } = await import("@mistralai/mistralai");
const client = new Mistral({ apiKey: process.env.MISTRAL_API_KEY });

// 4. Use normally - all calls are traced
const response = await client.chat.complete({
  model: "mistral-small-latest",
  messages: [{ role: "user", content: "Hello!" }],
});

// 5. Shutdown when done
await tracerProvider.shutdown();
```

## Supported Models

### Chat Models

| Model | ID | Description |
|-------|-----|-------------|
| Mistral Large | `mistral-large-latest` | Most capable model, flagship |
| Mistral Medium | `mistral-medium-latest` | Balanced performance |
| Mistral Small | `mistral-small-latest` | Fast and cost-effective |
| Open Mistral 7B | `open-mistral-7b` | Open-weight 7B model |
| Open Mixtral 8x7B | `open-mixtral-8x7b` | Open-weight MoE model |
| Open Mixtral 8x22B | `open-mixtral-8x22b` | Largest open model |
| Codestral | `codestral-latest` | Specialized for code |

### Embedding Models

| Model | ID | Dimensions |
|-------|-----|------------|
| Mistral Embed | `mistral-embed` | 1024 |

## Real-World Use Cases

### 1. AI Assistant with Function Calling

```typescript
async function aiAssistant(userQuery: string) {
  const tools = [
    {
      type: "function",
      function: {
        name: "get_current_weather",
        description: "Get the current weather in a location",
        parameters: {
          type: "object",
          properties: {
            location: {
              type: "string",
              description: "City name",
            },
            unit: {
              type: "string",
              enum: ["celsius", "fahrenheit"],
            },
          },
          required: ["location"],
        },
      },
    },
  ];

  const response = await mistral.chat.complete({
    model: "mistral-large-latest",
    messages: [{ role: "user", content: userQuery }],
    tools,
    tool_choice: "auto",
  });

  return response.choices[0].message;
}
```

### 2. Code Generation with Codestral

```typescript
async function generateCode(prompt: string) {
  const response = await mistral.chat.complete({
    model: "codestral-latest",
    messages: [
      {
        role: "system",
        content: "You are an expert programmer. Write clean, efficient code.",
      },
      { role: "user", content: prompt },
    ],
    temperature: 0.2,
    max_tokens: 2000,
  });

  return response.choices[0].message.content;
}

// Usage
const code = await generateCode("Write a TypeScript function to merge two sorted arrays");
```

### 3. Semantic Search with Embeddings

```typescript
async function semanticSearch(query: string, documents: string[]) {
  // Get query embedding
  const queryEmbedding = await mistral.embeddings.create({
    model: "mistral-embed",
    inputs: query,
  });

  // Get document embeddings
  const docEmbeddings = await mistral.embeddings.create({
    model: "mistral-embed",
    inputs: documents,
  });

  // Calculate cosine similarities
  const queryVector = queryEmbedding.data[0].embedding;
  const similarities = docEmbeddings.data.map((doc, index) => ({
    document: documents[index],
    similarity: cosineSimilarity(queryVector, doc.embedding),
  }));

  // Sort by similarity
  return similarities.sort((a, b) => b.similarity - a.similarity);
}

function cosineSimilarity(a: number[], b: number[]): number {
  const dotProduct = a.reduce((sum, val, i) => sum + val * b[i], 0);
  const magnitudeA = Math.sqrt(a.reduce((sum, val) => sum + val * val, 0));
  const magnitudeB = Math.sqrt(b.reduce((sum, val) => sum + val * val, 0));
  return dotProduct / (magnitudeA * magnitudeB);
}
```

### 4. Multi-Turn Conversation

```typescript
class ChatSession {
  private history: Array<{ role: string; content: string }> = [];

  constructor(systemPrompt?: string) {
    if (systemPrompt) {
      this.history.push({ role: "system", content: systemPrompt });
    }
  }

  async chat(userMessage: string): Promise<string> {
    this.history.push({ role: "user", content: userMessage });

    const response = await mistral.chat.complete({
      model: "mistral-large-latest",
      messages: this.history,
    });

    const assistantMessage = response.choices[0].message.content;
    this.history.push({ role: "assistant", content: assistantMessage || "" });

    return assistantMessage || "";
  }
}

// Usage
const session = new ChatSession("You are a helpful coding assistant.");
const answer1 = await session.chat("What is TypeScript?");
const answer2 = await session.chat("How does it compare to JavaScript?");
```

### 5. Safe Prompting

```typescript
async function safeChatCompletion(userMessage: string) {
  const response = await mistral.chat.complete({
    model: "mistral-large-latest",
    messages: [{ role: "user", content: userMessage }],
    safe_prompt: true, // Enable Mistral's safety guardrails
  });

  return response.choices[0].message.content;
}
```

### 6. JSON Mode

```typescript
async function extractStructuredData(text: string) {
  const response = await mistral.chat.complete({
    model: "mistral-large-latest",
    messages: [
      {
        role: "system",
        content: "Extract information as JSON. Only output valid JSON.",
      },
      {
        role: "user",
        content: `Extract person information from: "${text}"

Return JSON with fields: name, age, occupation, location`,
      },
    ],
    temperature: 0,
    response_format: { type: "json_object" },
  });

  return JSON.parse(response.choices[0].message.content || "{}");
}
```

## Traced Attributes

The instrumentation captures the following attributes:

| Attribute | Description |
|-----------|-------------|
| `llm.system` | Always "mistralai" |
| `llm.provider` | Always "mistralai" |
| `llm.model_name` | The model used |
| `llm.input_messages` | Input messages with role and content |
| `llm.output_messages` | Output messages with role and content |
| `llm.invocation_parameters` | Model parameters |
| `llm.token_count.prompt` | Number of input tokens |
| `llm.token_count.completion` | Number of output tokens |
| `llm.token_count.total` | Total tokens used |
| `llm.tools` | Tool definitions if provided |
| `embedding.model_name` | Embedding model used |
| `embedding.embeddings` | Embedding text and vectors |

## Integration with TraceAI Platform

```typescript
import { MistralInstrumentation } from "@traceai/mistral";
import { NodeTracerProvider } from "@opentelemetry/sdk-trace-node";
import { SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { OTLPTraceExporter } from "@opentelemetry/exporter-trace-otlp-proto";
import { Resource } from "@opentelemetry/resources";

const provider = new NodeTracerProvider({
  resource: new Resource({
    "service.name": "my-mistral-app",
    "deployment.environment": "production",
  }),
});

const exporter = new OTLPTraceExporter({
  url: "https://api.traceai.com/v1/traces",
  headers: {
    Authorization: `Bearer ${process.env.TRACEAI_API_KEY}`,
  },
});

provider.addSpanProcessor(new SimpleSpanProcessor(exporter));
provider.register();

const instrumentation = new MistralInstrumentation();
instrumentation.manuallyInstrument(require("@mistralai/mistralai"));
```

## Running Examples

The `examples/` directory contains real-world examples:

```bash
cd examples
cp .env.example .env
# Edit .env with your API keys

pnpm install
pnpm run example
```

## E2E Testing

Run E2E tests with real API keys:

```bash
MISTRAL_API_KEY=your_key pnpm test -- --testPathPattern=e2e
```

## License

Apache-2.0

## Related Packages

- [@traceai/openai](../traceai_openai) - OpenAI instrumentation
- [@traceai/groq](../traceai_groq) - Groq instrumentation
- [@traceai/anthropic](../traceai_anthropic) - Anthropic instrumentation
- [@traceai/cohere](../traceai_cohere) - Cohere instrumentation
- [@traceai/google-genai](../traceai_google_genai) - Google GenAI instrumentation
- [@traceai/fi-core](../fi-core) - Core tracing utilities
