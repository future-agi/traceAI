# @traceai/groq

OpenTelemetry instrumentation for [Groq](https://groq.com/) - the fastest inference engine for open-source LLMs.

## Features

- Automatic tracing of Groq Chat Completions
- Support for all Groq models (Mixtral, LLaMA 3, Gemma, etc.)
- Tool/function calling tracing
- Streaming response support
- Token usage tracking
- Error handling and exception recording

## Installation

```bash
npm install @traceai/groq
# or
pnpm add @traceai/groq
# or
yarn add @traceai/groq
```

## Quick Start

```typescript
import { GroqInstrumentation } from "@traceai/groq";
import { NodeTracerProvider } from "@opentelemetry/sdk-trace-node";
import { SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { OTLPTraceExporter } from "@opentelemetry/exporter-trace-otlp-proto";
import Groq from "groq-sdk";

// Initialize OpenTelemetry
const provider = new NodeTracerProvider();
const exporter = new OTLPTraceExporter({
  url: "http://localhost:4318/v1/traces",
});
provider.addSpanProcessor(new SimpleSpanProcessor(exporter));
provider.register();

// Initialize Groq instrumentation
const groqInstrumentation = new GroqInstrumentation();
groqInstrumentation.manuallyInstrument(require("groq-sdk"));

// Use Groq as normal - all calls are automatically traced
const groq = new Groq({ apiKey: process.env.GROQ_API_KEY });

const chatCompletion = await groq.chat.completions.create({
  model: "mixtral-8x7b-32768",
  messages: [
    { role: "user", content: "Explain quantum computing in simple terms" }
  ],
});

console.log(chatCompletion.choices[0].message.content);
```

## Configuration

### Basic Configuration

```typescript
const instrumentation = new GroqInstrumentation({
  instrumentationConfig: {
    enabled: true,
  },
});
```

### With Trace Configuration

```typescript
const instrumentation = new GroqInstrumentation({
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
import { GroqInstrumentation } from "@traceai/groq";
import { registerInstrumentations } from "@opentelemetry/instrumentation";

// 1. Register TraceAI Core TracerProvider
const tracerProvider = register({
  projectName: "my-groq-app",
  projectType: ProjectType.OBSERVE,
  sessionName: "groq-session-" + Date.now(),
});

// 2. Register instrumentation BEFORE importing Groq SDK
registerInstrumentations({
  tracerProvider: tracerProvider,
  instrumentations: [new GroqInstrumentation()],
});

// 3. NOW import and use Groq
const Groq = (await import("groq-sdk")).default;
const client = new Groq({ apiKey: process.env.GROQ_API_KEY });

// 4. Use normally - all calls are traced
const response = await client.chat.completions.create({
  model: "llama-3.1-8b-instant",
  messages: [{ role: "user", content: "Hello!" }],
});

// 5. Shutdown when done
await tracerProvider.shutdown();
```

## Supported Models

Groq offers blazing-fast inference for various open-source models:

| Model | ID | Context Window |
|-------|-----|----------------|
| Mixtral 8x7B | `mixtral-8x7b-32768` | 32,768 tokens |
| LLaMA 3.1 70B | `llama-3.1-70b-versatile` | 131,072 tokens |
| LLaMA 3.1 8B | `llama-3.1-8b-instant` | 131,072 tokens |
| LLaMA 3 70B | `llama3-70b-8192` | 8,192 tokens |
| LLaMA 3 8B | `llama3-8b-8192` | 8,192 tokens |
| Gemma 2 9B | `gemma2-9b-it` | 8,192 tokens |
| LLaMA 3 Groq 70B (Tool Use) | `llama3-groq-70b-8192-tool-use-preview` | 8,192 tokens |

## Real-World Use Cases

### 1. Low-Latency Chatbot

```typescript
import Groq from "groq-sdk";

const groq = new Groq({ apiKey: process.env.GROQ_API_KEY });

async function chat(userMessage: string, conversationHistory: any[]) {
  conversationHistory.push({ role: "user", content: userMessage });

  const response = await groq.chat.completions.create({
    model: "llama-3.1-8b-instant", // Fastest model for real-time chat
    messages: conversationHistory,
    temperature: 0.7,
    max_tokens: 500,
  });

  const assistantMessage = response.choices[0].message.content;
  conversationHistory.push({ role: "assistant", content: assistantMessage });

  return assistantMessage;
}

// Usage
const history = [
  { role: "system", content: "You are a helpful customer support agent." }
];

const response = await chat("I need help with my order", history);
console.log(response);
```

### 2. Code Generation with Mixtral

```typescript
async function generateCode(prompt: string, language: string) {
  const response = await groq.chat.completions.create({
    model: "mixtral-8x7b-32768",
    messages: [
      {
        role: "system",
        content: `You are an expert ${language} programmer. Provide clean, well-documented code.`,
      },
      {
        role: "user",
        content: prompt,
      },
    ],
    temperature: 0.2, // Lower temperature for more precise code
    max_tokens: 2000,
  });

  return response.choices[0].message.content;
}

// Usage
const code = await generateCode(
  "Write a function to validate email addresses using regex",
  "TypeScript"
);
console.log(code);
```

### 3. Tool Calling for Real-Time Data

```typescript
async function assistantWithTools(userQuery: string) {
  const tools = [
    {
      type: "function",
      function: {
        name: "get_weather",
        description: "Get current weather for a location",
        parameters: {
          type: "object",
          properties: {
            location: {
              type: "string",
              description: "City name, e.g. 'San Francisco, CA'",
            },
            unit: {
              type: "string",
              enum: ["celsius", "fahrenheit"],
              description: "Temperature unit",
            },
          },
          required: ["location"],
        },
      },
    },
    {
      type: "function",
      function: {
        name: "search_web",
        description: "Search the web for current information",
        parameters: {
          type: "object",
          properties: {
            query: {
              type: "string",
              description: "Search query",
            },
          },
          required: ["query"],
        },
      },
    },
  ];

  const response = await groq.chat.completions.create({
    model: "llama3-groq-70b-8192-tool-use-preview",
    messages: [{ role: "user", content: userQuery }],
    tools,
    tool_choice: "auto",
  });

  const message = response.choices[0].message;

  if (message.tool_calls) {
    // Process tool calls
    for (const toolCall of message.tool_calls) {
      console.log(`Calling tool: ${toolCall.function.name}`);
      console.log(`Arguments: ${toolCall.function.arguments}`);
      // Execute the tool and continue the conversation
    }
  }

  return message;
}

// Usage
const result = await assistantWithTools("What's the weather in Tokyo?");
```

### 4. Streaming Responses

```typescript
async function streamChat(prompt: string) {
  const stream = await groq.chat.completions.create({
    model: "mixtral-8x7b-32768",
    messages: [{ role: "user", content: prompt }],
    stream: true,
  });

  let fullResponse = "";

  for await (const chunk of stream) {
    const content = chunk.choices[0]?.delta?.content || "";
    process.stdout.write(content);
    fullResponse += content;
  }

  console.log("\n--- Stream complete ---");
  return fullResponse;
}

// Usage
await streamChat("Write a short poem about artificial intelligence");
```

### 5. Document Summarization

```typescript
async function summarizeDocument(document: string, maxWords: number = 100) {
  const response = await groq.chat.completions.create({
    model: "llama-3.1-70b-versatile", // Best for complex summarization
    messages: [
      {
        role: "system",
        content: `You are a document summarization expert. Provide concise summaries in ${maxWords} words or less.`,
      },
      {
        role: "user",
        content: `Summarize the following document:\n\n${document}`,
      },
    ],
    temperature: 0.3,
    max_tokens: 500,
  });

  return response.choices[0].message.content;
}
```

### 6. JSON Structured Output

```typescript
async function extractStructuredData(text: string) {
  const response = await groq.chat.completions.create({
    model: "mixtral-8x7b-32768",
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

// Usage
const data = await extractStructuredData(
  "John Smith is a 35-year-old software engineer living in Seattle."
);
console.log(data);
// Output: { name: "John Smith", age: 35, occupation: "software engineer", location: "Seattle" }
```

## Traced Attributes

The instrumentation captures the following attributes:

| Attribute | Description |
|-----------|-------------|
| `llm.system` | Always "groq" |
| `llm.provider` | Always "groq" |
| `llm.model_name` | The model used (e.g., "mixtral-8x7b-32768") |
| `llm.input_messages` | Input messages with role and content |
| `llm.output_messages` | Output messages with role and content |
| `llm.invocation_parameters` | Model parameters (temperature, max_tokens, etc.) |
| `llm.token_count.prompt` | Number of input tokens |
| `llm.token_count.completion` | Number of output tokens |
| `llm.token_count.total` | Total tokens used |
| `llm.tools` | Tool definitions if provided |
| `message.tool_calls` | Tool calls made by the model |

## Integration with TraceAI Platform

```typescript
import { GroqInstrumentation } from "@traceai/groq";
import { NodeTracerProvider } from "@opentelemetry/sdk-trace-node";
import { SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { OTLPTraceExporter } from "@opentelemetry/exporter-trace-otlp-proto";
import { Resource } from "@opentelemetry/resources";

// Configure for TraceAI platform
const provider = new NodeTracerProvider({
  resource: new Resource({
    "service.name": "my-groq-app",
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

// Initialize instrumentation
const instrumentation = new GroqInstrumentation();
instrumentation.manuallyInstrument(require("groq-sdk"));
```

## Why Groq?

Groq's LPU (Language Processing Unit) inference engine provides:

- **Ultra-low latency**: Up to 10x faster than GPU-based solutions
- **Consistent performance**: Predictable response times
- **Cost-effective**: Pay per token with no idle costs
- **Open-source models**: Access to Mixtral, LLaMA, Gemma, and more

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
GROQ_API_KEY=your_key pnpm test -- --testPathPattern=e2e
```

## License

Apache-2.0

## Related Packages

- [@traceai/openai](../traceai_openai) - OpenAI instrumentation
- [@traceai/anthropic](../traceai_anthropic) - Anthropic instrumentation
- [@traceai/fi-core](../fi-core) - Core tracing utilities
