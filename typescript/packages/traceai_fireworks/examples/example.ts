/**
 * Example: Using Fireworks AI instrumentation with TraceAI
 *
 * This example demonstrates how to set up and use the @traceai/fireworks
 * instrumentation to trace Fireworks AI API calls.
 *
 * Fireworks AI uses OpenAI-compatible API, so you use the OpenAI SDK with a custom baseURL.
 */
import { NodeTracerProvider } from "@opentelemetry/sdk-trace-node";
import { SimpleSpanProcessor, ConsoleSpanExporter } from "@opentelemetry/sdk-trace-base";
import { FireworksInstrumentation } from "@traceai/fireworks";
import OpenAI from "openai";

async function main() {
  // Step 1: Set up the tracer provider
  const provider = new NodeTracerProvider();
  provider.addSpanProcessor(new SimpleSpanProcessor(new ConsoleSpanExporter()));
  provider.register();

  // Step 2: Create and enable the instrumentation
  const instrumentation = new FireworksInstrumentation({
    traceConfig: {
      // Optional: Configure tracing options
      // hideInputs: true,  // Hide input content in traces
      // hideOutputs: true, // Hide output content in traces
    },
  });
  instrumentation.setTracerProvider(provider);
  instrumentation.enable();

  // Step 3: Import and patch the OpenAI module
  const openaiModule = await import("openai");
  instrumentation.manuallyInstrument(openaiModule);

  // Step 4: Create the Fireworks AI client (OpenAI SDK with custom baseURL)
  const client = new OpenAI({
    baseURL: "https://api.fireworks.ai/inference/v1",
    apiKey: process.env.FIREWORKS_API_KEY,
  });

  console.log("=== Chat Completion Example ===\n");

  // Example 1: Chat completion
  const chatResponse = await client.chat.completions.create({
    model: "accounts/fireworks/models/llama-v3p1-8b-instruct",
    messages: [
      { role: "system", content: "You are a helpful assistant." },
      { role: "user", content: "What is the capital of France?" },
    ],
    max_tokens: 100,
    temperature: 0.7,
  });

  console.log("Chat Response:", chatResponse.choices[0]?.message?.content);
  console.log("\n");

  console.log("=== Streaming Chat Example ===\n");

  // Example 2: Streaming chat completion
  const stream = await client.chat.completions.create({
    model: "accounts/fireworks/models/llama-v3p1-8b-instruct",
    messages: [
      { role: "user", content: "Count from 1 to 5." },
    ],
    max_tokens: 50,
    stream: true,
  });

  process.stdout.write("Stream Response: ");
  for await (const chunk of stream) {
    const content = chunk.choices[0]?.delta?.content;
    if (content) {
      process.stdout.write(content);
    }
  }
  console.log("\n");

  console.log("=== Completions API Example ===\n");

  // Example 3: Text completion (legacy completions API)
  const completion = await client.completions.create({
    model: "accounts/fireworks/models/llama-v3p1-8b-instruct",
    prompt: "The quick brown fox",
    max_tokens: 30,
  });

  console.log("Completion:", completion.choices[0]?.text);
  console.log("\n");

  console.log("=== Embeddings Example ===\n");

  // Example 4: Embeddings
  const embeddingResponse = await client.embeddings.create({
    model: "nomic-ai/nomic-embed-text-v1.5",
    input: "Hello, world!",
  });

  console.log("Embedding dimensions:", embeddingResponse.data[0]?.embedding.length);
  console.log("First 5 values:", embeddingResponse.data[0]?.embedding.slice(0, 5));
  console.log("\n");

  // Shutdown provider
  await provider.shutdown();
  console.log("Done! Check the console output above for trace spans.");
}

main().catch(console.error);
