# @traceai/google-genai

OpenTelemetry instrumentation for [Google Generative AI](https://ai.google.dev/) (Gemini).

## Features

- Automatic tracing of Gemini generateContent calls
- Embeddings API tracing (embedContent, batchEmbedContents)
- Support for all Gemini models
- Function calling tracing
- Token usage tracking
- Multimodal content support
- Error handling and exception recording

## Installation

```bash
npm install @traceai/google-genai
# or
pnpm add @traceai/google-genai
# or
yarn add @traceai/google-genai
```

## Quick Start

```typescript
import { GoogleGenAIInstrumentation } from "@traceai/google-genai";
import { NodeTracerProvider } from "@opentelemetry/sdk-trace-node";
import { SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { OTLPTraceExporter } from "@opentelemetry/exporter-trace-otlp-proto";
import { GoogleGenerativeAI } from "@google/generative-ai";

// Initialize OpenTelemetry
const provider = new NodeTracerProvider();
const exporter = new OTLPTraceExporter({
  url: "http://localhost:4318/v1/traces",
});
provider.addSpanProcessor(new SimpleSpanProcessor(exporter));
provider.register();

// Initialize Google GenAI instrumentation
const genAIInstrumentation = new GoogleGenAIInstrumentation();
genAIInstrumentation.manuallyInstrument(require("@google/generative-ai"));

// Use Google GenAI as normal - all calls are automatically traced
const genAI = new GoogleGenerativeAI(process.env.GOOGLE_API_KEY!);
const model = genAI.getGenerativeModel({ model: "gemini-1.5-pro" });

const result = await model.generateContent("Explain quantum computing");
console.log(result.response.text());
```

## Configuration

### Basic Configuration

```typescript
const instrumentation = new GoogleGenAIInstrumentation({
  instrumentationConfig: {
    enabled: true,
  },
});
```

### With Trace Configuration

```typescript
const instrumentation = new GoogleGenAIInstrumentation({
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
import { GoogleGenAIInstrumentation } from "@traceai/google-genai";
import { registerInstrumentations } from "@opentelemetry/instrumentation";

// 1. Register TraceAI Core TracerProvider
const tracerProvider = register({
  projectName: "my-gemini-app",
  projectType: ProjectType.OBSERVE,
  sessionName: "gemini-session-" + Date.now(),
});

// 2. Register instrumentation BEFORE importing Google GenAI SDK
registerInstrumentations({
  tracerProvider: tracerProvider,
  instrumentations: [new GoogleGenAIInstrumentation()],
});

// 3. NOW import and use Google GenAI
const { GoogleGenerativeAI } = await import("@google/generative-ai");
const genAI = new GoogleGenerativeAI(process.env.GOOGLE_API_KEY!);
const model = genAI.getGenerativeModel({ model: "gemini-1.5-flash" });

// 4. Use normally - all calls are traced
const result = await model.generateContent("Hello!");

// 5. Shutdown when done
await tracerProvider.shutdown();
```

## Supported Models

### Generative Models

| Model | ID | Description |
|-------|-----|-------------|
| Gemini 1.5 Pro | `gemini-1.5-pro` | Most capable, 1M context |
| Gemini 1.5 Flash | `gemini-1.5-flash` | Fast and versatile |
| Gemini 1.5 Flash-8B | `gemini-1.5-flash-8b` | Small and fast |
| Gemini 1.0 Pro | `gemini-1.0-pro` | Previous generation |
| Gemini Pro Vision | `gemini-pro-vision` | Multimodal (legacy) |

### Embedding Models

| Model | ID | Dimensions |
|-------|-----|------------|
| Embedding 001 | `embedding-001` | 768 |
| Text Embedding 004 | `text-embedding-004` | 768 |

## Real-World Use Cases

### 1. Basic Text Generation

```typescript
const genAI = new GoogleGenerativeAI(process.env.GOOGLE_API_KEY!);
const model = genAI.getGenerativeModel({ model: "gemini-1.5-pro" });

async function generateText(prompt: string) {
  const result = await model.generateContent(prompt);
  return result.response.text();
}

const answer = await generateText("What is machine learning?");
console.log(answer);
```

### 2. Multi-Turn Conversation

```typescript
const genAI = new GoogleGenerativeAI(process.env.GOOGLE_API_KEY!);
const model = genAI.getGenerativeModel({ model: "gemini-1.5-flash" });

async function chat(history: { role: string; parts: { text: string }[] }[], userMessage: string) {
  history.push({ role: "user", parts: [{ text: userMessage }] });

  const result = await model.generateContent({
    contents: history,
  });

  const response = result.response.text();
  history.push({ role: "model", parts: [{ text: response }] });

  return response;
}

// Usage
const history: { role: string; parts: { text: string }[] }[] = [];
const answer1 = await chat(history, "Hello! What's the capital of France?");
const answer2 = await chat(history, "What's the population there?");
```

### 3. System Instructions

```typescript
const genAI = new GoogleGenerativeAI(process.env.GOOGLE_API_KEY!);
const model = genAI.getGenerativeModel({
  model: "gemini-1.5-pro",
  systemInstruction: "You are a helpful coding assistant. Always provide code examples.",
});

const result = await model.generateContent(
  "How do I read a file in Python?"
);
console.log(result.response.text());
```

### 4. Function Calling

```typescript
const genAI = new GoogleGenerativeAI(process.env.GOOGLE_API_KEY!);

const tools = [
  {
    functionDeclarations: [
      {
        name: "get_weather",
        description: "Get the current weather for a location",
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
    ],
  },
];

const model = genAI.getGenerativeModel({
  model: "gemini-1.5-pro",
  tools,
});

const result = await model.generateContent(
  "What's the weather like in Tokyo?"
);

const functionCall = result.response.candidates?.[0]?.content.parts.find(
  (part) => part.functionCall
);
if (functionCall?.functionCall) {
  console.log("Function:", functionCall.functionCall.name);
  console.log("Arguments:", functionCall.functionCall.args);
}
```

### 5. Embeddings for Semantic Search

```typescript
const genAI = new GoogleGenerativeAI(process.env.GOOGLE_API_KEY!);
const embeddingModel = genAI.getGenerativeModel({ model: "text-embedding-004" });

async function getEmbedding(text: string) {
  const result = await embeddingModel.embedContent(text);
  return result.embedding.values;
}

async function semanticSearch(query: string, documents: string[]) {
  const queryEmbedding = await getEmbedding(query);

  const docEmbeddings = await Promise.all(
    documents.map((doc) => getEmbedding(doc))
  );

  // Calculate cosine similarities
  const similarities = docEmbeddings.map((docEmb, i) => ({
    document: documents[i],
    score: cosineSimilarity(queryEmbedding, docEmb),
  }));

  return similarities.sort((a, b) => b.score - a.score);
}

function cosineSimilarity(a: number[], b: number[]): number {
  const dot = a.reduce((sum, val, i) => sum + val * b[i], 0);
  const magA = Math.sqrt(a.reduce((sum, val) => sum + val * val, 0));
  const magB = Math.sqrt(b.reduce((sum, val) => sum + val * val, 0));
  return dot / (magA * magB);
}
```

### 6. Batch Embeddings

```typescript
const genAI = new GoogleGenerativeAI(process.env.GOOGLE_API_KEY!);
const embeddingModel = genAI.getGenerativeModel({ model: "text-embedding-004" });

async function batchEmbed(texts: string[]) {
  const result = await embeddingModel.batchEmbedContents({
    requests: texts.map((text) => ({
      content: { parts: [{ text }] },
    })),
  });

  return result.embeddings.map((e) => e.values);
}

// Usage
const embeddings = await batchEmbed([
  "First document text",
  "Second document text",
  "Third document text",
]);
```

### 7. Generation Configuration

```typescript
const genAI = new GoogleGenerativeAI(process.env.GOOGLE_API_KEY!);
const model = genAI.getGenerativeModel({
  model: "gemini-1.5-pro",
  generationConfig: {
    temperature: 0.7,
    topP: 0.9,
    topK: 40,
    maxOutputTokens: 2000,
    stopSequences: ["END"],
  },
});

const result = await model.generateContent("Write a creative story");
```

### 8. Safety Settings

```typescript
import { HarmCategory, HarmBlockThreshold } from "@google/generative-ai";

const genAI = new GoogleGenerativeAI(process.env.GOOGLE_API_KEY!);
const model = genAI.getGenerativeModel({
  model: "gemini-1.5-pro",
  safetySettings: [
    {
      category: HarmCategory.HARM_CATEGORY_HARASSMENT,
      threshold: HarmBlockThreshold.BLOCK_MEDIUM_AND_ABOVE,
    },
    {
      category: HarmCategory.HARM_CATEGORY_HATE_SPEECH,
      threshold: HarmBlockThreshold.BLOCK_MEDIUM_AND_ABOVE,
    },
  ],
});
```

## Traced Attributes

The instrumentation captures the following attributes:

| Attribute | Description |
|-----------|-------------|
| `llm.system` | Always "google_generative_ai" |
| `llm.provider` | Always "google_generative_ai" |
| `llm.model_name` | The model used |
| `llm.input_messages` | Input messages with role and content |
| `llm.output_messages` | Output messages with role and content |
| `llm.invocation_parameters` | Generation config |
| `llm.token_count.prompt` | Number of input tokens |
| `llm.token_count.completion` | Number of output tokens |
| `llm.token_count.total` | Total tokens used |
| `llm.tools` | Tool/function definitions |
| `embedding.model_name` | Embedding model used |
| `embedding.embeddings` | Embedding text and vectors |

## Integration with TraceAI Platform

```typescript
import { GoogleGenAIInstrumentation } from "@traceai/google-genai";
import { NodeTracerProvider } from "@opentelemetry/sdk-trace-node";
import { SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { OTLPTraceExporter } from "@opentelemetry/exporter-trace-otlp-proto";
import { Resource } from "@opentelemetry/resources";

const provider = new NodeTracerProvider({
  resource: new Resource({
    "service.name": "my-gemini-app",
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

const instrumentation = new GoogleGenAIInstrumentation();
instrumentation.manuallyInstrument(require("@google/generative-ai"));
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
GOOGLE_API_KEY=your_key pnpm test -- --testPathPattern=e2e
```

## License

Apache-2.0

## Related Packages

- [@traceai/openai](../traceai_openai) - OpenAI instrumentation
- [@traceai/anthropic](../traceai_anthropic) - Anthropic instrumentation
- [@traceai/groq](../traceai_groq) - Groq instrumentation
- [@traceai/mistral](../traceai_mistral) - Mistral instrumentation
- [@traceai/cohere](../traceai_cohere) - Cohere instrumentation
- [@traceai/fi-core](../fi-core) - Core tracing utilities
