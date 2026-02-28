/**
 * @traceai/cohere Real-World Example
 *
 * This example demonstrates how to use the Cohere instrumentation with TraceAI
 * for observability of Cohere API calls including chat, embed, and rerank.
 *
 * Prerequisites:
 * 1. Set environment variables (see .env.example)
 * 2. Install dependencies: pnpm install
 * 3. Run: npx ts-node example.ts
 */

import { register, ProjectType } from "@traceai/fi-core";
import { CohereInstrumentation } from "@traceai/cohere";
import { registerInstrumentations } from "@opentelemetry/instrumentation";
import { diag, DiagConsoleLogger, DiagLogLevel } from "@opentelemetry/api";
import "dotenv/config";

// Enable OpenTelemetry diagnostics for debugging
diag.setLogger(new DiagConsoleLogger(), DiagLogLevel.INFO);

// Validate environment variables
const fiApiKey = process.env.FI_API_KEY;
const fiSecretKey = process.env.FI_SECRET_KEY;
const cohereApiKey = process.env.COHERE_API_KEY;

if (!fiApiKey || !fiSecretKey) {
  console.error("FI_API_KEY and FI_SECRET_KEY environment variables must be set.");
  process.exit(1);
}

if (!cohereApiKey) {
  console.error("COHERE_API_KEY environment variable must be set.");
  process.exit(1);
}

async function main() {
  console.log("Starting Cohere Instrumentation Example...\n");

  // 1. Register TraceAI Core TracerProvider
  const tracerProvider = register({
    projectName: "cohere-instrumentation-example",
    projectType: ProjectType.OBSERVE,
    sessionName: "cohere-example-session-" + Date.now(),
  });
  console.log("TraceAI Core TracerProvider registered.");

  // 2. Register Cohere Instrumentation BEFORE importing the SDK
  registerInstrumentations({
    tracerProvider: tracerProvider,
    instrumentations: [
      new CohereInstrumentation({
        traceConfig: {
          hideInputs: false,
          hideOutputs: false,
        },
      }),
    ],
  });
  console.log("Cohere Instrumentation registered.\n");

  // 3. Dynamically import Cohere SDK AFTER instrumentation is registered
  const { CohereClient } = await import("cohere-ai");
  const client = new CohereClient({ token: cohereApiKey });

  try {
    // === Test Case 1: Basic Chat ===
    console.log("--- Test Case 1: Basic Chat ---");
    const basicChatResponse = await client.chat({
      message: "What is the capital of France? Answer in one sentence.",
      model: "command-r-08-2024",
    });
    console.log("Response:", basicChatResponse.text);
    console.log();

    // === Test Case 2: Chat with Preamble (System Prompt) ===
    console.log("--- Test Case 2: Chat with Preamble ---");
    const preambleResponse = await client.chat({
      message: "Tell me about the ocean.",
      model: "command-r-08-2024",
      preamble: "You are a helpful assistant that speaks like a pirate.",
    });
    console.log("Response:", preambleResponse.text);
    console.log();

    // === Test Case 3: Multi-turn Chat with History ===
    console.log("--- Test Case 3: Multi-turn Chat with History ---");
    const historyResponse = await client.chat({
      message: "What's my name?",
      model: "command-r-08-2024",
      chatHistory: [
        { role: "USER", message: "My name is Alice." },
        { role: "CHATBOT", message: "Hello Alice! It's nice to meet you. How can I help you today?" },
      ],
    });
    console.log("Response:", historyResponse.text);
    console.log();

    // === Test Case 4: Streaming Chat ===
    console.log("--- Test Case 4: Streaming Chat ---");
    const streamResponse = await client.chatStream({
      message: "Write a short poem about coding.",
      model: "command-r-08-2024",
    });

    process.stdout.write("Streaming response: ");
    for await (const event of streamResponse) {
      if (event.eventType === "text-generation") {
        process.stdout.write(event.text);
      }
    }
    console.log("\n");

    // === Test Case 5: Chat with Tools ===
    console.log("--- Test Case 5: Chat with Tools ---");
    const toolResponse = await client.chat({
      message: "What's the weather like in San Francisco?",
      model: "command-r-08-2024",
      tools: [
        {
          name: "get_weather",
          description: "Get the current weather in a given location",
          parameterDefinitions: {
            location: {
              type: "str",
              description: "The city and state, e.g. San Francisco, CA",
              required: true,
            },
            unit: {
              type: "str",
              description: "The unit of temperature: celsius or fahrenheit",
              required: false,
            },
          },
        },
      ],
    });

    if (toolResponse.toolCalls && toolResponse.toolCalls.length > 0) {
      console.log("Tool calls:", JSON.stringify(toolResponse.toolCalls, null, 2));
    } else {
      console.log("Response:", toolResponse.text);
    }
    console.log();

    // === Test Case 6: Embeddings ===
    console.log("--- Test Case 6: Embeddings ---");
    const embedResponse = await client.embed({
      texts: ["Hello, world!", "How are you?", "Cohere is an AI company."],
      model: "embed-english-v3.0",
      inputType: "search_document",
    });
    console.log("Embeddings count:", embedResponse.embeddings.length);
    if (Array.isArray(embedResponse.embeddings[0])) {
      console.log("First embedding dimensions:", embedResponse.embeddings[0].length);
      console.log("First embedding (first 5 values):", embedResponse.embeddings[0].slice(0, 5));
    }
    console.log();

    // === Test Case 7: Multilingual Embeddings ===
    console.log("--- Test Case 7: Multilingual Embeddings ---");
    const multilingualEmbedResponse = await client.embed({
      texts: [
        "Hello, world!",
        "Bonjour le monde!",
        "Hola mundo!",
        "Hallo Welt!",
      ],
      model: "embed-multilingual-v3.0",
      inputType: "search_document",
    });
    console.log("Multilingual embeddings count:", multilingualEmbedResponse.embeddings.length);
    console.log();

    // === Test Case 8: Rerank ===
    console.log("--- Test Case 8: Rerank ---");
    const rerankResponse = await client.rerank({
      query: "What is the capital of France?",
      documents: [
        "Paris is the capital of France and its largest city.",
        "Berlin is the capital of Germany.",
        "London is the capital of the United Kingdom.",
        "France is a country in Western Europe.",
        "The Eiffel Tower is located in Paris.",
      ],
      model: "rerank-english-v3.0",
      topN: 3,
    });
    console.log("Reranked results:");
    rerankResponse.results.forEach((result, index) => {
      console.log(`  ${index + 1}. Index: ${result.index}, Score: ${result.relevanceScore.toFixed(4)}`);
    });
    console.log();

    // === Test Case 9: Rerank with Return Documents ===
    console.log("--- Test Case 9: Rerank with Return Documents ---");
    const rerankWithDocsResponse = await client.rerank({
      query: "How do computers work?",
      documents: [
        "Computers process data using electronic circuits and transistors.",
        "The Internet connects millions of computers worldwide.",
        "Programming languages are used to write software.",
        "Artificial intelligence is a branch of computer science.",
      ],
      model: "rerank-english-v3.0",
      topN: 2,
      returnDocuments: true,
    });
    console.log("Reranked results with documents:");
    rerankWithDocsResponse.results.forEach((result, index) => {
      console.log(`  ${index + 1}. Score: ${result.relevanceScore.toFixed(4)}`);
      if (result.document) {
        console.log(`     Document: "${result.document.text.substring(0, 50)}..."`);
      }
    });
    console.log();

    console.log("--- All Test Cases Completed Successfully ---\n");

  } catch (error) {
    console.error("Error during Cohere API calls:", error);
  } finally {
    // 4. Shutdown the provider to ensure all spans are flushed
    console.log("Shutting down tracer provider...");
    await new Promise(resolve => setTimeout(resolve, 2000));
    await tracerProvider.shutdown();
    console.log("Tracer provider shut down successfully.");
  }
}

main().catch((error) => {
  console.error("Unhandled error:", error);
  process.exit(1);
});
