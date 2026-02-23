# LangChain Example (TypeScript)

Trace LangChain applications in TypeScript.

## Prerequisites

```bash
npm install @traceai/fi-core @traceai/langchain @traceai/openai
npm install @langchain/core @langchain/openai @opentelemetry/instrumentation
```

## Full Example

```typescript
import { register, ProjectType } from "@traceai/fi-core";
import { LangChainInstrumentation } from "@traceai/langchain";
import { OpenAIInstrumentation } from "@traceai/openai";
import { registerInstrumentations } from "@opentelemetry/instrumentation";

import { ChatOpenAI } from "@langchain/openai";
import { ChatPromptTemplate } from "@langchain/core/prompts";
import { StringOutputParser } from "@langchain/core/output_parsers";

// 1. Register tracer provider FIRST
const tracerProvider = register({
    projectName: "langchain_app",
    projectType: ProjectType.OBSERVE,
});

// 2. Register instrumentations
registerInstrumentations({
    tracerProvider,
    instrumentations: [
        new LangChainInstrumentation(),
        new OpenAIInstrumentation(),
    ],
});

// 3. Create LangChain components
const model = new ChatOpenAI({
    model: "gpt-4",
    temperature: 0.7,
});

const prompt = ChatPromptTemplate.fromTemplate(
    "You are a helpful assistant. Answer this question: {question}"
);

const outputParser = new StringOutputParser();

// 4. Build chain
const chain = prompt.pipe(model).pipe(outputParser);

// 5. Use function
async function ask(question: string): Promise<string> {
    return await chain.invoke({ question });
}

// 6. Main
async function main() {
    try {
        const answer = await ask("What is TypeScript?");
        console.log("Answer:", answer);
    } finally {
        await tracerProvider.shutdown();
    }
}

main();
```

## Trace Structure

```
chain (CHAIN)
├── prompt.format (CHAIN)
├── model.invoke (LLM)
└── outputParser.parse (CHAIN)
```

## With Streaming

```typescript
async function askStream(question: string): Promise<string> {
    let fullResponse = "";

    for await (const chunk of await chain.stream({ question })) {
        process.stdout.write(chunk);
        fullResponse += chunk;
    }

    console.log();
    return fullResponse;
}

const answer = await askStream("Explain async/await in JavaScript.");
```

## Multi-Step Chain

```typescript
import { RunnableSequence } from "@langchain/core/runnables";

const analysisPrompt = ChatPromptTemplate.fromTemplate(
    "Analyze this topic: {topic}. Provide key points."
);

const summaryPrompt = ChatPromptTemplate.fromTemplate(
    "Summarize these points in 2 sentences: {analysis}"
);

const analysisChain = analysisPrompt.pipe(model).pipe(outputParser);
const summaryChain = summaryPrompt.pipe(model).pipe(outputParser);

const fullChain = RunnableSequence.from([
    {
        analysis: analysisChain,
        topic: (input: { topic: string }) => input.topic,
    },
    summaryChain,
]);

async function analyzeAndSummarize(topic: string): Promise<string> {
    return await fullChain.invoke({ topic });
}

const summary = await analyzeAndSummarize("Machine Learning");
console.log(summary);
```

## With Memory

```typescript
import { BufferMemory } from "langchain/memory";
import { ConversationChain } from "langchain/chains";

const memory = new BufferMemory();

const conversationChain = new ConversationChain({
    llm: model,
    memory,
});

async function chat(message: string): Promise<string> {
    const response = await conversationChain.call({ input: message });
    return response.response;
}

// Multi-turn conversation
console.log(await chat("My name is Alice."));
console.log(await chat("What's my name?")); // Should remember
```

## With Tools

```typescript
import { DynamicTool } from "@langchain/core/tools";
import { AgentExecutor, createOpenAIFunctionsAgent } from "langchain/agents";

const tools = [
    new DynamicTool({
        name: "get_current_time",
        description: "Get the current time",
        func: async () => new Date().toISOString(),
    }),
    new DynamicTool({
        name: "calculate",
        description: "Calculate a math expression",
        func: async (expression: string) => {
            try {
                return String(eval(expression));
            } catch {
                return "Invalid expression";
            }
        },
    }),
];

const agentPrompt = ChatPromptTemplate.fromMessages([
    ["system", "You are a helpful assistant with access to tools."],
    ["human", "{input}"],
    ["placeholder", "{agent_scratchpad}"],
]);

const agent = await createOpenAIFunctionsAgent({
    llm: model,
    tools,
    prompt: agentPrompt,
});

const agentExecutor = new AgentExecutor({
    agent,
    tools,
});

async function askAgent(input: string): Promise<string> {
    const result = await agentExecutor.invoke({ input });
    return result.output;
}

console.log(await askAgent("What time is it?"));
console.log(await askAgent("What is 15 * 7?"));
```

## RAG Example

```typescript
import { MemoryVectorStore } from "langchain/vectorstores/memory";
import { OpenAIEmbeddings } from "@langchain/openai";
import { createRetrievalChain } from "langchain/chains/retrieval";
import { createStuffDocumentsChain } from "langchain/chains/combine_documents";

// Sample documents
const documents = [
    { pageContent: "Paris is the capital of France.", metadata: {} },
    { pageContent: "London is the capital of England.", metadata: {} },
    { pageContent: "Tokyo is the capital of Japan.", metadata: {} },
];

// Create vector store
const embeddings = new OpenAIEmbeddings();
const vectorStore = await MemoryVectorStore.fromTexts(
    documents.map(d => d.pageContent),
    documents.map(d => d.metadata),
    embeddings
);

const retriever = vectorStore.asRetriever({ k: 2 });

// RAG prompt
const ragPrompt = ChatPromptTemplate.fromTemplate(`
Answer based on the context below:

Context: {context}

Question: {input}

Answer:
`);

// Create chains
const combineDocsChain = await createStuffDocumentsChain({
    llm: model,
    prompt: ragPrompt,
});

const retrievalChain = await createRetrievalChain({
    combineDocsChain,
    retriever,
});

async function askRAG(question: string): Promise<string> {
    const result = await retrievalChain.invoke({ input: question });
    return result.answer;
}

console.log(await askRAG("What is the capital of France?"));
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
        eval_name: EvalName.COMPLETENESS,
        custom_eval_name: "response_completeness",
        mapping: { output: "raw.output" },
        model: ModelChoices.TURING_SMALL
    }),
    new EvalTag({
        type: EvalTagType.OBSERVATION_SPAN,
        value: EvalSpanKind.RETRIEVER,
        eval_name: EvalName.CONTEXT_RELEVANCE,
        custom_eval_name: "retrieval_relevance",
        mapping: {
            context: "raw.output",
            query: "raw.input"
        },
        model: ModelChoices.TURING_SMALL
    })
];

const tracerProvider = register({
    projectName: "langchain_experiment",
    projectType: ProjectType.EXPERIMENT,
    projectVersionName: "v1.0",
    evalTags,
});
```

## Manual Instrumentation

For some LangChain versions, use manual instrumentation:

```typescript
import * as langchainCore from "@langchain/core";

const instrumentation = new LangChainInstrumentation({
    tracerProvider,
});

instrumentation.manuallyInstrument(langchainCore);
```

## Error Handling

```typescript
async function safeAsk(question: string): Promise<string> {
    try {
        return await ask(question);
    } catch (error) {
        // Errors are captured in trace
        console.error("Chain error:", error);
        return "Sorry, something went wrong.";
    }
}
```

## Related

- [Basic OpenAI](basic-openai.md)
- [fi-core Reference](../../typescript/fi-core.md)
- [Instrumentations](../../typescript/instrumentations.md)
