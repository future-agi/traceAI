# @traceai/ollama

OpenTelemetry instrumentation for the [Ollama](https://ollama.ai/) TypeScript/JavaScript SDK.

Ollama enables running large language models locally, providing privacy-first AI capabilities with support for Llama, Mistral, Phi, and many other open-source models.

## Installation

```bash
npm install @traceai/ollama
# or
pnpm add @traceai/ollama
# or
yarn add @traceai/ollama
```

## Features

- Automatic instrumentation of Ollama API calls
- Supports chat, generate, and embed APIs
- Captures request parameters, model information, and responses
- Full streaming support with token tracking
- Configurable input/output hiding for privacy
- Compatible with OpenTelemetry ecosystem

## Usage

### Basic Setup

```typescript
import { OllamaInstrumentation } from "@traceai/ollama";
import { NodeTracerProvider } from "@opentelemetry/sdk-trace-node";
import { registerInstrumentations } from "@opentelemetry/instrumentation";

// Set up the tracer provider
const provider = new NodeTracerProvider();
provider.register();

// Register the Ollama instrumentation
registerInstrumentations({
  instrumentations: [new OllamaInstrumentation()],
});

// Now use the Ollama SDK as normal
import { Ollama } from "ollama";

const ollama = new Ollama({ host: "http://localhost:11434" });

// Chat example
const chatResponse = await ollama.chat({
  model: "llama3.2",
  messages: [{ role: "user", content: "Hello, how are you?" }],
});

// Generate example
const generateResponse = await ollama.generate({
  model: "llama3.2",
  prompt: "The capital of France is",
});

// Embed example
const embedResponse = await ollama.embed({
  model: "nomic-embed-text",
  input: ["Hello", "World"],
});
```

### Manual Instrumentation

If automatic instrumentation doesn't work (e.g., with bundlers), you can manually instrument:

```typescript
import { OllamaInstrumentation } from "@traceai/ollama";
import * as OllamaModule from "ollama";

const instrumentation = new OllamaInstrumentation();
instrumentation.manuallyInstrument(OllamaModule);
```

### Configuration Options

```typescript
const instrumentation = new OllamaInstrumentation({
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
import { OllamaInstrumentation } from "@traceai/ollama";
import { registerInstrumentations } from "@opentelemetry/instrumentation";

// 1. Register TraceAI Core TracerProvider
const tracerProvider = register({
  projectName: "my-ollama-app",
  projectType: ProjectType.OBSERVE,
  sessionName: "ollama-session-" + Date.now(),
});

// 2. Register instrumentation BEFORE importing Ollama SDK
registerInstrumentations({
  tracerProvider: tracerProvider,
  instrumentations: [new OllamaInstrumentation()],
});

// 3. NOW import and use Ollama
const { Ollama } = await import("ollama");
const client = new Ollama();

// 4. Use normally - all calls are traced
const response = await client.chat({
  model: "llama3.2",
  messages: [{ role: "user", content: "Hello!" }],
});

// 5. Shutdown when done
await tracerProvider.shutdown();
```

## Real-World Use Cases

### 1. Local Chatbot with History

```typescript
import { Ollama } from "ollama";

const ollama = new Ollama();

interface Message {
  role: "system" | "user" | "assistant";
  content: string;
}

async function chat(message: string, history: Message[]) {
  const response = await ollama.chat({
    model: "llama3.2",
    messages: [
      { role: "system", content: "You are a helpful coding assistant." },
      ...history,
      { role: "user", content: message },
    ],
  });

  // Add to history
  history.push({ role: "user", content: message });
  history.push({ role: "assistant", content: response.message.content });

  return response.message.content;
}

// Usage
const history: Message[] = [];
const answer1 = await chat("How do I read a file in Python?", history);
const answer2 = await chat("What about async file reading?", history);
```

### 2. Streaming Chat Responses

```typescript
async function streamChat(message: string) {
  const stream = await ollama.chat({
    model: "llama3.2",
    messages: [{ role: "user", content: message }],
    stream: true,
  });

  let fullText = "";
  for await (const chunk of stream) {
    process.stdout.write(chunk.message.content);
    fullText += chunk.message.content;
  }
  console.log("\n--- Stream complete ---");

  return fullText;
}
```

### 3. Local Embeddings for Semantic Search

```typescript
async function semanticSearch(query: string, documents: string[]) {
  // Get query embedding
  const queryResult = await ollama.embed({
    model: "nomic-embed-text",
    input: query,
  });
  const queryVector = queryResult.embeddings[0];

  // Get document embeddings
  const docResult = await ollama.embed({
    model: "nomic-embed-text",
    input: documents,
  });

  // Calculate cosine similarities
  const similarities = docResult.embeddings.map((docVector, i) => ({
    document: documents[i],
    score: cosineSimilarity(queryVector, docVector),
  }));

  return similarities.sort((a, b) => b.score - a.score);
}

function cosineSimilarity(a: number[], b: number[]): number {
  const dotProduct = a.reduce((sum, val, i) => sum + val * b[i], 0);
  const magnitudeA = Math.sqrt(a.reduce((sum, val) => sum + val * val, 0));
  const magnitudeB = Math.sqrt(b.reduce((sum, val) => sum + val * val, 0));
  return dotProduct / (magnitudeA * magnitudeB);
}
```

### 4. Text Completion with System Prompt

```typescript
async function generateWithContext(prompt: string, systemPrompt: string) {
  const response = await ollama.generate({
    model: "llama3.2",
    prompt: prompt,
    system: systemPrompt,
    options: {
      temperature: 0.7,
      num_predict: 200,
    },
  });

  return response.response;
}

// Usage
const story = await generateWithContext(
  "Write a short story about a robot",
  "You are a creative fiction writer. Write engaging, vivid stories."
);
```

### 5. JSON Mode for Structured Output

```typescript
async function extractStructuredData(text: string) {
  const response = await ollama.chat({
    model: "llama3.2",
    messages: [
      {
        role: "user",
        content: `Extract the following information from the text and return as JSON:
        - names (array of strings)
        - dates (array of strings)
        - locations (array of strings)

        Text: ${text}`,
      },
    ],
    format: "json",
  });

  return JSON.parse(response.message.content);
}
```

### 6. Code Generation with CodeLlama

```typescript
async function generateCode(description: string, language: string) {
  const response = await ollama.chat({
    model: "codellama",
    messages: [
      {
        role: "system",
        content: `You are an expert ${language} programmer. Write clean, well-documented code.`,
      },
      {
        role: "user",
        content: `Write a ${language} function that: ${description}`,
      },
    ],
    options: {
      temperature: 0.2, // Lower temperature for more deterministic code
    },
  });

  return response.message.content;
}
```

### 7. Multi-Model Comparison

```typescript
async function compareModels(prompt: string, models: string[]) {
  const results = await Promise.all(
    models.map(async (model) => {
      const start = Date.now();
      const response = await ollama.chat({
        model,
        messages: [{ role: "user", content: prompt }],
      });
      const duration = Date.now() - start;

      return {
        model,
        response: response.message.content,
        duration,
        promptTokens: response.prompt_eval_count,
        completionTokens: response.eval_count,
      };
    })
  );

  return results;
}

// Usage
const comparison = await compareModels("Explain quantum computing in one sentence", [
  "llama3.2",
  "mistral",
  "phi3",
]);
```

### 8. RAG with Local Embeddings

```typescript
interface Document {
  id: string;
  text: string;
  embedding?: number[];
}

class LocalRAG {
  private documents: Document[] = [];
  private ollama: Ollama;

  constructor() {
    this.ollama = new Ollama();
  }

  async addDocuments(docs: { id: string; text: string }[]) {
    const embedResult = await this.ollama.embed({
      model: "nomic-embed-text",
      input: docs.map((d) => d.text),
    });

    this.documents.push(
      ...docs.map((doc, i) => ({
        ...doc,
        embedding: embedResult.embeddings[i],
      }))
    );
  }

  async query(question: string, topK = 3) {
    // Get question embedding
    const queryResult = await this.ollama.embed({
      model: "nomic-embed-text",
      input: question,
    });
    const queryVector = queryResult.embeddings[0];

    // Find similar documents
    const similarities = this.documents
      .map((doc) => ({
        doc,
        score: this.cosineSimilarity(queryVector, doc.embedding!),
      }))
      .sort((a, b) => b.score - a.score)
      .slice(0, topK);

    // Generate answer with context
    const context = similarities.map((s) => s.doc.text).join("\n\n");
    const response = await this.ollama.chat({
      model: "llama3.2",
      messages: [
        {
          role: "system",
          content: "Answer questions based on the provided context. If the answer isn't in the context, say so.",
        },
        {
          role: "user",
          content: `Context:\n${context}\n\nQuestion: ${question}`,
        },
      ],
    });

    return {
      answer: response.message.content,
      sources: similarities.map((s) => ({ id: s.doc.id, score: s.score })),
    };
  }

  private cosineSimilarity(a: number[], b: number[]): number {
    const dotProduct = a.reduce((sum, val, i) => sum + val * b[i], 0);
    const magnitudeA = Math.sqrt(a.reduce((sum, val) => sum + val * val, 0));
    const magnitudeB = Math.sqrt(b.reduce((sum, val) => sum + val * val, 0));
    return dotProduct / (magnitudeA * magnitudeB);
  }
}
```

## Traced Operations

### Chat API

The instrumentation captures:

- Model name
- Input messages (system, user, assistant roles)
- Generation options (temperature, top_p, top_k, etc.)
- Response content
- Token usage (prompt_eval_count, eval_count)
- Streaming chunks

### Generate API

The instrumentation captures:

- Model name
- Prompt text
- System prompt (if provided)
- Generation options
- Response text
- Token usage
- Streaming chunks

### Embed API

The instrumentation captures:

- Model name
- Input texts (single or batch)
- Output embeddings
- Token usage

## Semantic Conventions

This instrumentation follows OpenTelemetry semantic conventions and FutureAGI conventions for LLM observability:

- Span names follow the pattern: `Ollama {operation}` (e.g., `Ollama Chat`, `Ollama Generate`, `Ollama Embed`)
- Uses `LLM` span kind for chat and generate operations
- Uses `EMBEDDING` span kind for embed operations

## Supported Models

Ollama supports a wide variety of open-source models. Some popular ones include:

### Chat/Completion Models

- llama2, llama3, llama3.1, llama3.2
- mistral, mixtral
- codellama
- phi, phi3
- gemma, gemma2
- qwen, qwen2
- deepseek-coder
- neural-chat
- starling-lm
- vicuna
- orca-mini
- dolphin-mistral

### Embedding Models

- nomic-embed-text
- mxbai-embed-large
- all-minilm

## Requirements

- Node.js >= 18
- ollama SDK (npm package)
- Ollama running locally or accessible via network

## Prerequisites

1. **Install Ollama**: Download from [ollama.ai](https://ollama.ai/)

2. **Start Ollama server**:
   ```bash
   ollama serve
   ```

3. **Pull required models**:
   ```bash
   ollama pull llama3.2
   ollama pull nomic-embed-text
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

Run E2E tests (requires Ollama running locally):

```bash
# Ensure Ollama is running
ollama serve

# Pull test models
ollama pull llama3.2
ollama pull nomic-embed-text

# Run tests
OLLAMA_HOST=http://localhost:11434 pnpm test -- --testPathPattern=e2e
```

## Integration with TraceAI Platform

```typescript
import { OllamaInstrumentation } from "@traceai/ollama";
import { NodeTracerProvider } from "@opentelemetry/sdk-trace-node";
import { SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { OTLPTraceExporter } from "@opentelemetry/exporter-trace-otlp-proto";
import { Resource } from "@opentelemetry/resources";

const provider = new NodeTracerProvider({
  resource: new Resource({
    "service.name": "my-ollama-app",
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

const instrumentation = new OllamaInstrumentation();
instrumentation.manuallyInstrument(require("ollama"));
```

## Privacy & Local-First

One of the main benefits of Ollama is privacy - all model inference happens locally. The TraceAI instrumentation respects this by:

- Only capturing telemetry data locally by default
- Providing `hideInputs` and `hideOutputs` options to mask sensitive data
- Not sending any data to external services unless explicitly configured

## License

Apache-2.0

## Related Packages

- [@traceai/openai](../traceai_openai) - OpenAI instrumentation
- [@traceai/anthropic](../traceai_anthropic) - Anthropic instrumentation
- [@traceai/groq](../traceai_groq) - Groq instrumentation
- [@traceai/mistral](../traceai_mistral) - Mistral instrumentation
- [@traceai/cohere](../traceai_cohere) - Cohere instrumentation
- [@traceai/google-genai](../traceai_google_genai) - Google GenAI instrumentation
- [@traceai/fi-core](../fi-core) - Core tracing utilities
