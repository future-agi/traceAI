# @traceai/cohere

OpenTelemetry instrumentation for the [Cohere](https://cohere.com/) TypeScript/JavaScript SDK.

## Installation

```bash
npm install @traceai/cohere
# or
pnpm add @traceai/cohere
# or
yarn add @traceai/cohere
```

## Features

- Automatic instrumentation of Cohere API calls
- Supports chat, embed, and rerank APIs
- Captures request parameters, model information, and responses
- Configurable input/output hiding for privacy
- Compatible with OpenTelemetry ecosystem

## Usage

### Basic Setup

```typescript
import { CohereInstrumentation } from "@traceai/cohere";
import { NodeTracerProvider } from "@opentelemetry/sdk-trace-node";
import { registerInstrumentations } from "@opentelemetry/instrumentation";

// Set up the tracer provider
const provider = new NodeTracerProvider();
provider.register();

// Register the Cohere instrumentation
registerInstrumentations({
  instrumentations: [new CohereInstrumentation()],
});

// Now use the Cohere SDK as normal
import { CohereClient } from "cohere-ai";

const cohere = new CohereClient({
  token: process.env.COHERE_API_KEY,
});

// Chat example
const chatResponse = await cohere.chat({
  message: "Hello, how are you?",
  model: "command-r-08-2024",
});

// Embed example
const embedResponse = await cohere.embed({
  texts: ["hello", "goodbye"],
  model: "embed-english-v3.0",
  inputType: "search_document",
});

// Rerank example
const rerankResponse = await cohere.rerank({
  query: "What is the capital of France?",
  documents: [
    "Paris is the capital of France.",
    "Berlin is the capital of Germany.",
    "London is the capital of the United Kingdom.",
  ],
  model: "rerank-english-v3.0",
});
```

### Manual Instrumentation

If automatic instrumentation doesn't work (e.g., with bundlers), you can manually instrument:

```typescript
import { CohereInstrumentation } from "@traceai/cohere";
import * as CohereModule from "cohere-ai";

const instrumentation = new CohereInstrumentation();
instrumentation.manuallyInstrument(CohereModule);
```

### Configuration Options

```typescript
const instrumentation = new CohereInstrumentation({
  // OpenTelemetry instrumentation config
  instrumentationConfig: {
    enabled: true,
  },
  // TraceAI-specific config
  traceConfig: {
    hideInputs: false, // Set to true to hide input content in traces
    hideOutputs: false, // Set to true to hide output content in traces
  },
});
```

### With TraceAI Core

```typescript
import { register, ProjectType } from "@traceai/fi-core";
import { CohereInstrumentation } from "@traceai/cohere";
import { registerInstrumentations } from "@opentelemetry/instrumentation";

// 1. Register TraceAI Core TracerProvider
const tracerProvider = register({
  projectName: "my-cohere-app",
  projectType: ProjectType.OBSERVE,
  sessionName: "cohere-session-" + Date.now(),
});

// 2. Register instrumentation BEFORE importing Cohere SDK
registerInstrumentations({
  tracerProvider: tracerProvider,
  instrumentations: [new CohereInstrumentation()],
});

// 3. NOW import and use Cohere
const { CohereClient } = await import("cohere-ai");
const client = new CohereClient({ token: process.env.COHERE_API_KEY });

// 4. Use normally - all calls are traced
const response = await client.chat({
  message: "Hello!",
  model: "command-r-08-2024",
});

// 5. Shutdown when done
await tracerProvider.shutdown();
```

## Real-World Use Cases

### 1. Conversational AI with Chat History

```typescript
const { CohereClient } = await import("cohere-ai");
const client = new CohereClient({ token: process.env.COHERE_API_KEY });

async function chat(message: string, history: Array<{ role: string; message: string }>) {
  const response = await client.chat({
    message,
    model: "command-r-08-2024",
    chatHistory: history,
    preamble: "You are a helpful customer support agent for an e-commerce platform.",
  });

  // Add to history
  history.push({ role: "USER", message });
  history.push({ role: "CHATBOT", message: response.text });

  return response.text;
}

// Usage
const history: Array<{ role: string; message: string }> = [];
const answer1 = await chat("I have a problem with my order", history);
const answer2 = await chat("Order ID is 12345", history);
```

### 2. Semantic Search with Embeddings

```typescript
async function semanticSearch(query: string, documents: string[]) {
  // Get query embedding (use 'search_query' for queries)
  const queryEmbedding = await client.embed({
    texts: [query],
    model: "embed-english-v3.0",
    inputType: "search_query",
  });

  // Get document embeddings (use 'search_document' for documents)
  const docEmbeddings = await client.embed({
    texts: documents,
    model: "embed-english-v3.0",
    inputType: "search_document",
  });

  // Calculate cosine similarities
  const queryVector = queryEmbedding.embeddings[0] as number[];
  const similarities = (docEmbeddings.embeddings as number[][]).map((docVector, i) => ({
    document: documents[i],
    score: cosineSimilarity(queryVector, docVector),
  }));

  return similarities.sort((a, b) => b.score - a.score);
}
```

### 3. Document Reranking for RAG

```typescript
async function rerankDocuments(query: string, documents: string[]) {
  const response = await client.rerank({
    query,
    documents,
    model: "rerank-english-v3.0",
    topN: 5,
    returnDocuments: true,
  });

  return response.results.map((result) => ({
    document: result.document?.text,
    score: result.relevanceScore,
    originalIndex: result.index,
  }));
}

// Usage in RAG pipeline
const retrievedDocs = await vectorSearch(userQuery, 20); // Get top 20 from vector DB
const rerankedDocs = await rerankDocuments(userQuery, retrievedDocs);
const topDocs = rerankedDocs.slice(0, 3); // Use top 3 for context
```

### 4. Tool-Augmented Chatbot

```typescript
async function chatWithTools(message: string) {
  const response = await client.chat({
    message,
    model: "command-r-08-2024",
    tools: [
      {
        name: "search_database",
        description: "Search the product database",
        parameterDefinitions: {
          query: { type: "str", description: "Search query", required: true },
          category: { type: "str", description: "Product category", required: false },
        },
      },
      {
        name: "get_order_status",
        description: "Get status of a customer order",
        parameterDefinitions: {
          orderId: { type: "str", description: "Order ID", required: true },
        },
      },
    ],
  });

  if (response.toolCalls && response.toolCalls.length > 0) {
    // Execute tool calls and continue conversation
    for (const toolCall of response.toolCalls) {
      console.log(`Tool: ${toolCall.name}, Args:`, toolCall.parameters);
    }
  }

  return response;
}
```

### 5. Multilingual Embeddings

```typescript
async function multilingualSearch(query: string, documents: string[]) {
  // Use multilingual model for cross-language search
  const queryEmbedding = await client.embed({
    texts: [query],
    model: "embed-multilingual-v3.0",
    inputType: "search_query",
  });

  const docEmbeddings = await client.embed({
    texts: documents,
    model: "embed-multilingual-v3.0",
    inputType: "search_document",
  });

  // Works across languages!
  // Query in English can match documents in French, German, Spanish, etc.
  return calculateSimilarities(queryEmbedding, docEmbeddings);
}
```

### 6. Streaming Chat Responses

```typescript
async function streamChat(message: string) {
  const stream = await client.chatStream({
    message,
    model: "command-r-08-2024",
  });

  let fullText = "";
  for await (const event of stream) {
    if (event.eventType === "text-generation") {
      process.stdout.write(event.text);
      fullText += event.text;
    } else if (event.eventType === "stream-end") {
      console.log("\n--- Stream complete ---");
      console.log("Token usage:", event.response?.meta?.tokens);
    }
  }

  return fullText;
}
```

## Traced Operations

### Chat API

The instrumentation captures:

- Model name
- Input messages and chat history
- Preamble and connectors
- Temperature and other generation parameters
- Response text and metadata
- Token usage (prompt and completion tokens)

### Embed API

The instrumentation captures:

- Model name
- Input texts
- Input type
- Truncation settings
- Output embeddings (unless hidden)
- Token usage

### Rerank API

The instrumentation captures:

- Model name
- Query text
- Documents to rerank
- Top N parameter
- Reranked results with relevance scores

## Semantic Conventions

This instrumentation follows OpenTelemetry semantic conventions and FutureAGI conventions for LLM observability:

- Span names follow the pattern: `cohere.{operation}` (e.g., `cohere.chat`, `cohere.embed`, `cohere.rerank`)
- Uses `LLM` span kind for chat operations
- Uses `EMBEDDING` span kind for embed operations
- Uses `RERANKER` span kind for rerank operations

## Supported Models

### Chat Models

- command
- command-r
- command-r-08-2024
- command-light
- command-nightly

### Embedding Models

- embed-english-v3.0
- embed-multilingual-v3.0
- embed-english-light-v3.0
- embed-multilingual-light-v3.0

### Rerank Models

- rerank-english-v3.0
- rerank-multilingual-v3.0
- rerank-english-v2.0
- rerank-multilingual-v2.0

## Requirements

- Node.js >= 18
- cohere-ai SDK

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
COHERE_API_KEY=your_key pnpm test -- --testPathPattern=e2e
```

## Integration with TraceAI Platform

```typescript
import { CohereInstrumentation } from "@traceai/cohere";
import { NodeTracerProvider } from "@opentelemetry/sdk-trace-node";
import { SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { OTLPTraceExporter } from "@opentelemetry/exporter-trace-otlp-proto";
import { Resource } from "@opentelemetry/resources";

const provider = new NodeTracerProvider({
  resource: new Resource({
    "service.name": "my-cohere-app",
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

const instrumentation = new CohereInstrumentation();
instrumentation.manuallyInstrument(require("cohere-ai"));
```

## License

Apache-2.0

## Related Packages

- [@traceai/openai](../traceai_openai) - OpenAI instrumentation
- [@traceai/anthropic](../traceai_anthropic) - Anthropic instrumentation
- [@traceai/groq](../traceai_groq) - Groq instrumentation
- [@traceai/mistral](../traceai_mistral) - Mistral instrumentation
- [@traceai/google-genai](../traceai_google_genai) - Google GenAI instrumentation
- [@traceai/fi-core](../fi-core) - Core tracing utilities
