# Basic OpenAI Example (TypeScript)

A complete example of tracing OpenAI chat completions in TypeScript.

## Prerequisites

```bash
npm install @traceai/fi-core @traceai/openai openai @opentelemetry/instrumentation
```

## Full Example

```typescript
import { register, ProjectType } from "@traceai/fi-core";
import { OpenAIInstrumentation } from "@traceai/openai";
import { registerInstrumentations } from "@opentelemetry/instrumentation";
import OpenAI from "openai";

// 1. Register tracer provider FIRST
const tracerProvider = register({
    projectName: "openai_chatbot",
    projectType: ProjectType.OBSERVE,
});

// 2. Register instrumentations BEFORE creating client
registerInstrumentations({
    tracerProvider,
    instrumentations: [new OpenAIInstrumentation()],
});

// 3. NOW create OpenAI client
const openai = new OpenAI({
    apiKey: process.env.OPENAI_API_KEY,
});

// 4. Chat function
async function chat(userMessage: string): Promise<string> {
    const response = await openai.chat.completions.create({
        model: "gpt-4",
        messages: [
            { role: "system", content: "You are a helpful assistant." },
            { role: "user", content: userMessage }
        ],
        temperature: 0.7,
        max_tokens: 500
    });

    return response.choices[0].message.content || "";
}

// 5. Main function
async function main() {
    try {
        const response = await chat("What is the capital of France?");
        console.log("Response:", response);
    } finally {
        // Always shutdown
        await tracerProvider.shutdown();
    }
}

main();
```

## What Gets Captured

| Attribute | Value |
|-----------|-------|
| `fi.span_kind` | `LLM` |
| `llm.system` | `openai` |
| `llm.model_name` | `gpt-4` |
| `llm.input_messages` | System + user messages |
| `llm.output_messages` | Assistant response |
| `llm.token_count.prompt` | Input tokens |
| `llm.token_count.completion` | Output tokens |
| `llm.invocation_parameters` | Model parameters |

## With Streaming

```typescript
async function chatStream(userMessage: string): Promise<string> {
    const stream = await openai.chat.completions.create({
        model: "gpt-4",
        messages: [{ role: "user", content: userMessage }],
        stream: true,
    });

    let fullResponse = "";

    for await (const chunk of stream) {
        const content = chunk.choices[0]?.delta?.content;
        if (content) {
            process.stdout.write(content);
            fullResponse += content;
        }
    }

    console.log(); // Newline
    return fullResponse;
}

// Use it
const response = await chatStream("Tell me a short story.");
```

## With Tool Calling

```typescript
const tools: OpenAI.Chat.ChatCompletionTool[] = [
    {
        type: "function",
        function: {
            name: "get_weather",
            description: "Get weather for a location",
            parameters: {
                type: "object",
                properties: {
                    location: { type: "string", description: "City name" },
                    unit: { type: "string", enum: ["celsius", "fahrenheit"] }
                },
                required: ["location"]
            }
        }
    }
];

function getWeather(location: string, unit = "celsius"): object {
    return { location, temperature: 22, unit };
}

async function chatWithTools(userMessage: string): Promise<string> {
    const response = await openai.chat.completions.create({
        model: "gpt-4",
        messages: [{ role: "user", content: userMessage }],
        tools,
        tool_choice: "auto",
    });

    const message = response.choices[0].message;

    if (message.tool_calls) {
        const messages: OpenAI.Chat.ChatCompletionMessageParam[] = [
            { role: "user", content: userMessage },
            message,
        ];

        for (const toolCall of message.tool_calls) {
            if (toolCall.function.name === "get_weather") {
                const args = JSON.parse(toolCall.function.arguments);
                const result = getWeather(args.location, args.unit);

                messages.push({
                    role: "tool",
                    tool_call_id: toolCall.id,
                    content: JSON.stringify(result),
                });
            }
        }

        const finalResponse = await openai.chat.completions.create({
            model: "gpt-4",
            messages,
            tools,
        });

        return finalResponse.choices[0].message.content || "";
    }

    return message.content || "";
}

// Use it
const weather = await chatWithTools("What's the weather in Paris?");
console.log(weather);
```

## With Experiments

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
        model: ModelChoices.PROTECT_FLASH
    }),
    new EvalTag({
        type: EvalTagType.OBSERVATION_SPAN,
        value: EvalSpanKind.LLM,
        eval_name: EvalName.IS_HELPFUL,
        custom_eval_name: "helpfulness_check",
        mapping: { output: "raw.output" },
        model: ModelChoices.TURING_SMALL
    })
];

const tracerProvider = register({
    projectName: "openai_experiment",
    projectType: ProjectType.EXPERIMENT,
    projectVersionName: "v1.0",
    evalTags,
});
```

## Privacy Controls

```typescript
import { OpenAIInstrumentation } from "@traceai/openai";

const instrumentation = new OpenAIInstrumentation({
    traceConfig: {
        hideInputs: true,
        hideOutputs: true,
        hideInputMessages: false,
        hideOutputMessages: false,
    }
});

registerInstrumentations({
    tracerProvider,
    instrumentations: [instrumentation],
});
```

## Express.js Integration

```typescript
import express from "express";
import { register, ProjectType } from "@traceai/fi-core";
import { OpenAIInstrumentation } from "@traceai/openai";
import { registerInstrumentations } from "@opentelemetry/instrumentation";
import OpenAI from "openai";

// Setup tracing FIRST
const tracerProvider = register({
    projectName: "express_chatbot",
    projectType: ProjectType.OBSERVE,
});

registerInstrumentations({
    tracerProvider,
    instrumentations: [new OpenAIInstrumentation()],
});

// Then create clients
const openai = new OpenAI();
const app = express();

app.use(express.json());

app.post("/chat", async (req, res) => {
    try {
        const { message } = req.body;

        const response = await openai.chat.completions.create({
            model: "gpt-4",
            messages: [{ role: "user", content: message }],
        });

        res.json({
            reply: response.choices[0].message.content
        });
    } catch (error) {
        res.status(500).json({ error: "Chat failed" });
    }
});

// Graceful shutdown
process.on("SIGTERM", async () => {
    await tracerProvider.shutdown();
    process.exit(0);
});

app.listen(3000, () => {
    console.log("Server running on port 3000");
});
```

## Next.js API Route

```typescript
// app/api/chat/route.ts
import { register, ProjectType } from "@traceai/fi-core";
import { OpenAIInstrumentation } from "@traceai/openai";
import { registerInstrumentations } from "@opentelemetry/instrumentation";
import OpenAI from "openai";
import { NextRequest, NextResponse } from "next/server";

// Initialize once
let initialized = false;
let openai: OpenAI;

function initTracing() {
    if (initialized) return;

    const tracerProvider = register({
        projectName: "nextjs_chatbot",
        projectType: ProjectType.OBSERVE,
    });

    registerInstrumentations({
        tracerProvider,
        instrumentations: [new OpenAIInstrumentation()],
    });

    openai = new OpenAI();
    initialized = true;
}

export async function POST(request: NextRequest) {
    initTracing();

    const { message } = await request.json();

    const response = await openai.chat.completions.create({
        model: "gpt-4",
        messages: [{ role: "user", content: message }],
    });

    return NextResponse.json({
        reply: response.choices[0].message.content
    });
}
```

## Error Handling

```typescript
async function safeChat(message: string): Promise<string> {
    try {
        return await chat(message);
    } catch (error) {
        if (error instanceof OpenAI.APIError) {
            // API errors are captured in trace
            console.error("OpenAI API error:", error.message);
            return "";
        }
        throw error;
    }
}
```

## Related

- [LangChain Example](langchain-example.md)
- [fi-core Reference](../../typescript/fi-core.md)
- [Instrumentations](../../typescript/instrumentations.md)
