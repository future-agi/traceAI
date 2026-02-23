/**
 * @traceai/ollama Real-World Example
 *
 * This example demonstrates how to use the Ollama instrumentation with TraceAI
 * for observability of local LLM calls including chat, generate, and embeddings.
 *
 * Prerequisites:
 * 1. Install Ollama: https://ollama.ai
 * 2. Start Ollama: ollama serve
 * 3. Pull models:
 *    - ollama pull llama3.2
 *    - ollama pull nomic-embed-text
 * 4. Set environment variables (see .env.example)
 * 5. Install dependencies: pnpm install
 * 6. Run: npx ts-node example.ts
 */

import { register, ProjectType } from "@traceai/fi-core";
import { OllamaInstrumentation } from "@traceai/ollama";
import { registerInstrumentations } from "@opentelemetry/instrumentation";
import { diag, DiagConsoleLogger, DiagLogLevel } from "@opentelemetry/api";
import "dotenv/config";

// Enable OpenTelemetry diagnostics for debugging
diag.setLogger(new DiagConsoleLogger(), DiagLogLevel.INFO);

// Validate environment variables
const fiApiKey = process.env.FI_API_KEY;
const fiSecretKey = process.env.FI_SECRET_KEY;
const ollamaHost = process.env.OLLAMA_HOST || "http://localhost:11434";
const ollamaModel = process.env.OLLAMA_MODEL || "llama3.2";
const ollamaEmbedModel = process.env.OLLAMA_EMBED_MODEL || "nomic-embed-text";

if (!fiApiKey || !fiSecretKey) {
  console.error("FI_API_KEY and FI_SECRET_KEY environment variables must be set.");
  process.exit(1);
}

async function main() {
  console.log("Starting Ollama Instrumentation Example...\n");
  console.log(`Ollama Host: ${ollamaHost}`);
  console.log(`Chat Model: ${ollamaModel}`);
  console.log(`Embed Model: ${ollamaEmbedModel}\n`);

  // 1. Register TraceAI Core TracerProvider
  const tracerProvider = register({
    projectName: "ollama-instrumentation-example",
    projectType: ProjectType.OBSERVE,
    sessionName: "ollama-example-session-" + Date.now(),
  });
  console.log("TraceAI Core TracerProvider registered.");

  // 2. Register Ollama Instrumentation BEFORE importing the SDK
  registerInstrumentations({
    tracerProvider: tracerProvider,
    instrumentations: [
      new OllamaInstrumentation({
        traceConfig: {
          hideInputs: false,
          hideOutputs: false,
        },
      }),
    ],
  });
  console.log("Ollama Instrumentation registered.\n");

  // 3. Dynamically import Ollama SDK AFTER instrumentation is registered
  const { Ollama } = await import("ollama");
  const client = new Ollama({ host: ollamaHost });

  try {
    // === Test Case 1: Basic Chat ===
    console.log("--- Test Case 1: Basic Chat ---");
    const basicChatResponse = await client.chat({
      model: ollamaModel,
      messages: [
        { role: "user", content: "What is the capital of France? Answer in one sentence." }
      ],
      stream: false,
    });
    console.log("Response:", basicChatResponse.message.content);
    console.log("Tokens - Prompt:", basicChatResponse.prompt_eval_count, "Completion:", basicChatResponse.eval_count);
    console.log();

    // === Test Case 2: Chat with System Prompt ===
    console.log("--- Test Case 2: Chat with System Prompt ---");
    const systemPromptResponse = await client.chat({
      model: ollamaModel,
      messages: [
        { role: "system", content: "You are a helpful assistant that speaks like a pirate." },
        { role: "user", content: "Tell me about the ocean." }
      ],
      stream: false,
    });
    console.log("Response:", systemPromptResponse.message.content);
    console.log();

    // === Test Case 3: Multi-turn Chat with History ===
    console.log("--- Test Case 3: Multi-turn Chat with History ---");
    const historyResponse = await client.chat({
      model: ollamaModel,
      messages: [
        { role: "user", content: "My name is Alice." },
        { role: "assistant", content: "Hello Alice! It's nice to meet you. How can I help you today?" },
        { role: "user", content: "What's my name?" }
      ],
      stream: false,
    });
    console.log("Response:", historyResponse.message.content);
    console.log();

    // === Test Case 4: Streaming Chat ===
    console.log("--- Test Case 4: Streaming Chat ---");
    const streamResponse = await client.chat({
      model: ollamaModel,
      messages: [
        { role: "user", content: "Write a short poem about coding in 4 lines." }
      ],
      stream: true,
    });

    process.stdout.write("Streaming response: ");
    for await (const chunk of streamResponse) {
      if (chunk.message?.content) {
        process.stdout.write(chunk.message.content);
      }
    }
    console.log("\n");

    // === Test Case 5: JSON Format ===
    console.log("--- Test Case 5: JSON Format ---");
    const jsonResponse = await client.chat({
      model: ollamaModel,
      messages: [
        { role: "user", content: "Return a JSON object with keys 'name' set to 'Alice' and 'age' set to 30. Only output valid JSON." }
      ],
      format: "json",
      stream: false,
    });
    console.log("Response:", jsonResponse.message.content);
    try {
      const parsed = JSON.parse(jsonResponse.message.content);
      console.log("Parsed JSON:", parsed);
    } catch (e) {
      console.log("Note: Response may not be valid JSON");
    }
    console.log();

    // === Test Case 6: Generate (Completion) ===
    console.log("--- Test Case 6: Generate (Completion) ---");
    const generateResponse = await client.generate({
      model: ollamaModel,
      prompt: "The three primary colors are:",
      stream: false,
    });
    console.log("Response:", generateResponse.response);
    console.log("Tokens - Prompt:", generateResponse.prompt_eval_count, "Completion:", generateResponse.eval_count);
    console.log();

    // === Test Case 7: Streaming Generate ===
    console.log("--- Test Case 7: Streaming Generate ---");
    const streamGenerate = await client.generate({
      model: ollamaModel,
      prompt: "Write a haiku about programming:",
      stream: true,
    });

    process.stdout.write("Streaming generate: ");
    for await (const chunk of streamGenerate) {
      if (chunk.response) {
        process.stdout.write(chunk.response);
      }
    }
    console.log("\n");

    // === Test Case 8: Single Embedding ===
    console.log("--- Test Case 8: Single Embedding ---");
    const embedResponse = await client.embed({
      model: ollamaEmbedModel,
      input: "Hello, world!",
    });
    console.log("Embeddings count:", embedResponse.embeddings.length);
    console.log("First embedding dimensions:", embedResponse.embeddings[0].length);
    console.log("First embedding (first 5 values):", embedResponse.embeddings[0].slice(0, 5));
    console.log();

    // === Test Case 9: Batch Embeddings ===
    console.log("--- Test Case 9: Batch Embeddings ---");
    const batchEmbedResponse = await client.embed({
      model: ollamaEmbedModel,
      input: [
        "Hello, world!",
        "Bonjour le monde!",
        "Hola mundo!",
        "Hallo Welt!",
      ],
    });
    console.log("Batch embeddings count:", batchEmbedResponse.embeddings.length);
    batchEmbedResponse.embeddings.forEach((emb, i) => {
      console.log(`  Embedding ${i + 1} dimensions:`, emb.length);
    });
    console.log();

    // === Test Case 10: Legacy Embeddings API ===
    console.log("--- Test Case 10: Legacy Embeddings API ---");
    const legacyEmbedResponse = await client.embeddings({
      model: ollamaEmbedModel,
      prompt: "Testing the legacy embeddings API",
    });
    console.log("Legacy embedding dimensions:", legacyEmbedResponse.embedding.length);
    console.log("First 5 values:", legacyEmbedResponse.embedding.slice(0, 5));
    console.log();

    // === Test Case 11: Chat with Options ===
    console.log("--- Test Case 11: Chat with Options ---");
    const optionsResponse = await client.chat({
      model: ollamaModel,
      messages: [
        { role: "user", content: "Generate a random creative sentence." }
      ],
      options: {
        temperature: 0.9,
        top_p: 0.95,
        top_k: 40,
        num_predict: 50,
      },
      stream: false,
    });
    console.log("Response (high temperature):", optionsResponse.message.content);
    console.log();

    console.log("--- All Test Cases Completed Successfully ---\n");

  } catch (error) {
    console.error("Error during Ollama API calls:", error);
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
