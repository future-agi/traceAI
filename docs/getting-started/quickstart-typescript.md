# TypeScript Quickstart

Get started with traceAI in your TypeScript AI application in under 5 minutes.

## Basic Setup

### 1. Install Dependencies

```bash
npm install @traceai/fi-core @traceai/openai openai @opentelemetry/instrumentation
```

### 2. Set Environment Variables

```bash
export FI_API_KEY="your-api-key"
export FI_SECRET_KEY="your-secret-key"
export OPENAI_API_KEY="your-openai-key"
```

### 3. Instrument Your Application

```typescript
import { register, ProjectType } from "@traceai/fi-core";
import { OpenAIInstrumentation } from "@traceai/openai";
import { registerInstrumentations } from "@opentelemetry/instrumentation";
import OpenAI from "openai";

// 1. Register tracer provider FIRST
const tracerProvider = register({
    projectName: "my_chatbot",
    projectType: ProjectType.OBSERVE,
});

// 2. Register instrumentations BEFORE creating clients
registerInstrumentations({
    tracerProvider,
    instrumentations: [new OpenAIInstrumentation()],
});

// 3. Now create and use OpenAI client
const openai = new OpenAI({
    apiKey: process.env.OPENAI_API_KEY,
});

async function main() {
    const response = await openai.chat.completions.create({
        model: "gpt-4",
        messages: [
            { role: "system", content: "You are a helpful assistant." },
            { role: "user", content: "What is the capital of France?" }
        ],
    });

    console.log(response.choices[0].message.content);

    // Important: Shutdown provider when done
    await tracerProvider.shutdown();
}

main();
```

**Important**: Register instrumentations BEFORE importing/creating framework clients!

## Multiple Frameworks

Instrument multiple frameworks together:

```typescript
import { register, ProjectType } from "@traceai/fi-core";
import { OpenAIInstrumentation } from "@traceai/openai";
import { AnthropicInstrumentation } from "@traceai/anthropic";
import { LangChainInstrumentation } from "@traceai/langchain";
import { registerInstrumentations } from "@opentelemetry/instrumentation";

const tracerProvider = register({
    projectName: "multi_llm_app",
    projectType: ProjectType.OBSERVE,
});

registerInstrumentations({
    tracerProvider,
    instrumentations: [
        new OpenAIInstrumentation(),
        new AnthropicInstrumentation(),
        new LangChainInstrumentation(),
    ],
});
```

## Streaming Support

Streaming responses are automatically traced:

```typescript
const stream = await openai.chat.completions.create({
    model: "gpt-4",
    messages: [{ role: "user", content: "Tell me a story." }],
    stream: true,
});

for await (const chunk of stream) {
    const content = chunk.choices[0]?.delta?.content;
    if (content) {
        process.stdout.write(content);
    }
}
```

## Running Experiments

Use `ProjectType.EXPERIMENT` to run AI evaluations:

```typescript
import {
    register,
    ProjectType,
    EvalTag,
    EvalTagType,
    EvalSpanKind,
    EvalName,
    ModelChoices
} from "@traceai/fi-core";

const evalTags = [
    new EvalTag({
        type: EvalTagType.OBSERVATION_SPAN,
        value: EvalSpanKind.LLM,
        eval_name: EvalName.TOXICITY,
        custom_eval_name: "toxicity_check",
        mapping: { output: "raw.output" },
        model: ModelChoices.TURING_SMALL
    }),
    new EvalTag({
        type: EvalTagType.OBSERVATION_SPAN,
        value: EvalSpanKind.LLM,
        eval_name: EvalName.CONTEXT_ADHERENCE,
        custom_eval_name: "adherence_check",
        mapping: {
            context: "raw.input",
            output: "raw.output"
        },
        model: ModelChoices.TURING_SMALL
    })
];

const tracerProvider = register({
    projectName: "my_experiment",
    projectType: ProjectType.EXPERIMENT,
    projectVersionName: "v1.0",
    evalTags: evalTags,
});
```

## Privacy Controls

Configure data redaction:

```typescript
import { OpenAIInstrumentation } from "@traceai/openai";

const instrumentation = new OpenAIInstrumentation({
    traceConfig: {
        hideInputs: true,
        hideOutputs: true,
        hideInputMessages: false,
        hideOutputMessages: false,
        hideEmbeddingVectors: true,
    }
});
```

## Configuration Options

Full configuration example:

```typescript
const tracerProvider = register({
    // Required
    projectName: "my_app",

    // Project type
    projectType: ProjectType.OBSERVE,  // or EXPERIMENT

    // Optional
    projectVersionName: "v1.0",        // Only for EXPERIMENT
    evalTags: [],                       // Only for EXPERIMENT
    sessionName: "session-123",         // Only for OBSERVE
    metadata: { team: "ml" },

    // Advanced
    batch: false,                       // Use batch processor
    verbose: true,                      // Debug logging
    transport: Transport.HTTP,          // or Transport.GRPC

    // Custom headers (override env vars)
    headers: {
        "x-api-key": "...",
        "x-secret-key": "..."
    }
});
```

## Express.js Example

```typescript
import express from "express";
import { register, ProjectType } from "@traceai/fi-core";
import { OpenAIInstrumentation } from "@traceai/openai";
import { registerInstrumentations } from "@opentelemetry/instrumentation";
import OpenAI from "openai";

// Setup tracing before anything else
const tracerProvider = register({
    projectName: "express_api",
    projectType: ProjectType.OBSERVE,
});

registerInstrumentations({
    tracerProvider,
    instrumentations: [new OpenAIInstrumentation()],
});

const openai = new OpenAI();
const app = express();

app.use(express.json());

app.post("/chat", async (req, res) => {
    const { message } = req.body;

    const response = await openai.chat.completions.create({
        model: "gpt-4",
        messages: [{ role: "user", content: message }],
    });

    res.json({ reply: response.choices[0].message.content });
});

app.listen(3000);
```

## Graceful Shutdown

Always shutdown the tracer provider:

```typescript
// In your main function
try {
    // Your app logic
} finally {
    await tracerProvider.shutdown();
}

// Or handle process signals
process.on("SIGINT", async () => {
    await tracerProvider.shutdown();
    process.exit(0);
});
```

## Next Steps

- [fi-core Reference](../typescript/fi-core.md) - Full API documentation
- [Evaluation Tags](../configuration/eval-tags.md) - AI evaluations
- [OpenAI Examples](../examples/typescript/basic-openai.md) - More examples
