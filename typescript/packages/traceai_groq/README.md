# @traceai/groq

OpenTelemetry instrumentation for [Groq](https://groq.com/) - the fastest inference engine for open-source LLMs.

## Features

- Automatic tracing of Groq Chat Completions
- Support for Groq Chat Completions models available to your account
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

For a local OTLP collector, install the SDK and OpenTelemetry packages used below:

```bash
npm install groq-sdk @opentelemetry/sdk-trace-node@^2 @opentelemetry/sdk-trace-base@^2 @opentelemetry/exporter-trace-otlp-proto@^0.201.1
```

To send traces to Future AGI instead, use [With TraceAI Core](#with-traceai-core).

```typescript
import { GroqInstrumentation } from "@traceai/groq";
import { NodeTracerProvider } from "@opentelemetry/sdk-trace-node";
import { SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { OTLPTraceExporter } from "@opentelemetry/exporter-trace-otlp-proto";
import Groq from "groq-sdk";

// Initialize OpenTelemetry 2.x with your OTLP collector.
const exporter = new OTLPTraceExporter({
  url: "http://localhost:4318/v1/traces",
});
const provider = new NodeTracerProvider({
  spanProcessors: [new SimpleSpanProcessor(exporter)],
});
provider.register();

// Initialize Groq instrumentation.
const groqInstrumentation = new GroqInstrumentation();
groqInstrumentation.setTracerProvider(provider);
groqInstrumentation.manuallyInstrument({ Groq });

// Use Groq as normal - all calls are automatically traced
const groq = new Groq({ apiKey: process.env.GROQ_API_KEY });

const chatCompletion = await groq.chat.completions.create({
  model: "llama-3.1-8b-instant",
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

```bash
npm install @traceai/fi-core groq-sdk
```

Set `FI_API_KEY`, `FI_SECRET_KEY`, and `GROQ_API_KEY` in your application's environment.

```typescript
import { register, ProjectType } from "@traceai/fi-core";
import { GroqInstrumentation } from "@traceai/groq";
import Groq from "groq-sdk";

// 1. Register TraceAI Core TracerProvider
const tracerProvider = register({
  projectName: "my-groq-app",
  projectType: ProjectType.OBSERVE,
  sessionName: "groq-session-" + Date.now(),
});

// 2. Attach instrumentation to this provider and SDK instance.
const instrumentation = new GroqInstrumentation();
instrumentation.setTracerProvider(tracerProvider);
instrumentation.manuallyInstrument({ Groq });

// 3. Create the client.
const client = new Groq({ apiKey: process.env.GROQ_API_KEY });

// 4. Use normally - all calls are traced
const response = await client.chat.completions.create({
  model: "llama-3.1-8b-instant",
  messages: [{ role: "user", content: "Hello!" }],
});

// 5. Shutdown when done
await tracerProvider.shutdown();
```

## Models

The instrumentation traces Chat Completions independently of the model ID. The examples below use `llama-3.1-8b-instant` and `llama-3.3-70b-versatile`. Check [Groq's model list](https://console.groq.com/docs/models) for the models currently available to your account before running an example.

## Real-World Use Cases

### 1. Low-Latency Chatbot

```typescript
import Groq from "groq-sdk";

const groq = new Groq({ apiKey: process.env.GROQ_API_KEY });

async function chat(userMessage: string, conversationHistory: any[]) {
  conversationHistory.push({ role: "user", content: userMessage });

  const response = await groq.chat.completions.create({
    model: "llama-3.1-8b-instant", // Choose a model suited to your task
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

### 2. Code generation

```typescript
async function generateCode(prompt: string, language: string) {
  const response = await groq.chat.completions.create({
    model: "llama-3.1-8b-instant",
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
    model: "llama-3.3-70b-versatile",
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
    model: "llama-3.1-8b-instant",
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

Streaming is consumed only as your application reads it; tracing does not run a second reader in the background. A stream's span finishes when you finish reading or cancel it. Breaking out of `for await` preserves the SDK's cancellation behavior. Token counts are recorded when Groq supplies usage, usually in the final chunk, so a cancelled stream may not have token counts.

SDK helpers remain available: `.withResponse()` gives you the parsed result and response headers, and streams retain `.tee()`, `.toReadableStream()`, and `.controller`. Use `.withResponse()` when you want both the headers and a traced response body. Calling only `.asResponse()` leaves the body untouched and finishes a metadata-only span; it does not record generated text or usage.

### 5. Document Summarization

```typescript
async function summarizeDocument(document: string, maxWords: number = 100) {
  const response = await groq.chat.completions.create({
    model: "llama-3.3-70b-versatile", // Choose a model suited to your task
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
    model: "llama-3.1-8b-instant",
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
| `fi.span.kind` | `LLM` |
| `gen_ai.provider.name` | `groq` |
| `gen_ai.request.model` | Model requested by the application |
| `gen_ai.response.model` | Model reported by Groq, when provided |
| `gen_ai.input.messages` | Input messages with role and content |
| `gen_ai.output.messages` | Output messages, including tool calls in the message JSON |
| `gen_ai.request.parameters` | Requested model parameters |
| `gen_ai.usage.input_tokens` | Input tokens, when provided |
| `gen_ai.usage.output_tokens` | Output tokens, when provided |
| `gen_ai.usage.total_tokens` | Total tokens, when provided |
| `gen_ai.tool.definitions` | Tool definitions, when provided |

## Send traces to Future AGI

Use the maintained `@traceai/fi-core` setup in [With TraceAI Core](#with-traceai-core). Set `FI_API_KEY` and `FI_SECRET_KEY` in your application's environment. The core package configures the Future AGI exporter and project resource attributes; you do not need a custom exporter or a second tracer provider.

## Running Examples

The `examples/` directory contains real-world examples:

```bash
cd examples
cp .env.example .env
# Edit .env with your API keys

pnpm install
pnpm run example
```

## License

Apache-2.0

## Related Packages

- [@traceai/openai](../traceai_openai) - OpenAI instrumentation
- [@traceai/anthropic](../traceai_anthropic) - Anthropic instrumentation
- [@traceai/fi-core](../fi-core) - Core tracing utilities
