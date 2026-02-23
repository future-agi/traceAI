/**
 * Example: Using vLLM instrumentation with TraceAI
 *
 * This example demonstrates how to set up and use the @traceai/vllm
 * instrumentation to trace vLLM inference calls.
 *
 * vLLM uses OpenAI-compatible API, so you use the OpenAI SDK with a custom baseURL.
 */
import { NodeTracerProvider } from "@opentelemetry/sdk-trace-node";
import { SimpleSpanProcessor, ConsoleSpanExporter } from "@opentelemetry/sdk-trace-base";
import { VLLMInstrumentation } from "@traceai/vllm";
import OpenAI from "openai";

async function main() {
  // Step 1: Set up the tracer provider
  const provider = new NodeTracerProvider();
  provider.addSpanProcessor(new SimpleSpanProcessor(new ConsoleSpanExporter()));
  provider.register();

  // Step 2: Create and enable the instrumentation
  const instrumentation = new VLLMInstrumentation({
    // Optional: Configure a URL pattern to identify vLLM requests
    // If not set, all OpenAI SDK requests will be traced as vLLM
    baseUrlPattern: "localhost:8000",
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

  // Step 4: Create the vLLM client (OpenAI SDK with custom baseURL)
  const client = new OpenAI({
    baseURL: "http://localhost:8000/v1", // vLLM server URL
    apiKey: "not-needed", // vLLM doesn't require API key
  });

  console.log("=== Chat Completion Example ===\n");

  // Example 1: Chat completion
  const chatResponse = await client.chat.completions.create({
    model: "meta-llama/Llama-2-7b-chat-hf", // Model served by vLLM
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
    model: "meta-llama/Llama-2-7b-chat-hf",
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
    model: "meta-llama/Llama-2-7b-hf",
    prompt: "The quick brown fox",
    max_tokens: 30,
  });

  console.log("Completion:", completion.choices[0]?.text);
  console.log("\n");

  // Shutdown provider
  await provider.shutdown();
  console.log("Done! Check the console output above for trace spans.");
}

main().catch(console.error);
