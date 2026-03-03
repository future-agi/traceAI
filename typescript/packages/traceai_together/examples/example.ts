/**
 * Example: Using Together AI instrumentation with TraceAI
 *
 * This example demonstrates how to set up and use the @traceai/together
 * instrumentation to trace Together AI API calls.
 */
import { NodeTracerProvider } from "@opentelemetry/sdk-trace-node";
import { SimpleSpanProcessor, ConsoleSpanExporter } from "@opentelemetry/sdk-trace-base";
import { TogetherInstrumentation } from "@traceai/together";
import Together from "together-ai";

async function main() {
  // Step 1: Set up the tracer provider
  const provider = new NodeTracerProvider();
  provider.addSpanProcessor(new SimpleSpanProcessor(new ConsoleSpanExporter()));
  provider.register();

  // Step 2: Create and enable the instrumentation
  const instrumentation = new TogetherInstrumentation({
    traceConfig: {
      // Optional: Configure tracing options
      // hideInputs: true,  // Hide input content in traces
      // hideOutputs: true, // Hide output content in traces
    },
  });
  instrumentation.setTracerProvider(provider);
  instrumentation.enable();

  // Step 3: Import and patch the Together module
  const togetherModule = await import("together-ai");
  instrumentation.manuallyInstrument(togetherModule);

  // Step 4: Create the Together client
  const client = new Together({
    apiKey: process.env.TOGETHER_API_KEY,
  });

  console.log("=== Chat Completion Example ===\n");

  // Example 1: Basic chat completion
  const chatResponse = await client.chat.completions.create({
    model: "meta-llama/Llama-3-8b-chat-hf",
    messages: [
      { role: "system", content: "You are a helpful assistant." },
      { role: "user", content: "What is the capital of France?" },
    ],
    max_tokens: 100,
    temperature: 0.7,
  });

  console.log("Chat Response:", chatResponse.choices[0]?.message?.content);
  console.log("Tokens used:", chatResponse.usage);
  console.log("\n");

  console.log("=== Streaming Example ===\n");

  // Example 2: Streaming chat completion
  const stream = await client.chat.completions.create({
    model: "meta-llama/Llama-3-8b-chat-hf",
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

  console.log("=== Text Completion Example ===\n");

  // Example 3: Legacy text completion
  const completionResponse = await client.completions.create({
    model: "togethercomputer/RedPajama-INCITE-7B-Base",
    prompt: "The quick brown fox",
    max_tokens: 50,
  });

  console.log("Completion Response:", completionResponse.choices[0]?.text);
  console.log("\n");

  console.log("=== Embeddings Example ===\n");

  // Example 4: Embeddings
  const embeddingResponse = await client.embeddings.create({
    model: "togethercomputer/m2-bert-80M-8k-retrieval",
    input: "Hello, world!",
  });

  console.log("Embedding dimensions:", embeddingResponse.data[0]?.embedding?.length);
  console.log("First 5 values:", embeddingResponse.data[0]?.embedding?.slice(0, 5));
  console.log("\n");

  // Example 5: Batch embeddings
  const batchEmbeddingResponse = await client.embeddings.create({
    model: "togethercomputer/m2-bert-80M-8k-retrieval",
    input: ["Hello", "World", "Together AI"],
  });

  console.log("Batch embeddings count:", batchEmbeddingResponse.data.length);

  // Shutdown provider
  await provider.shutdown();
  console.log("\nDone! Check the console output above for trace spans.");
}

main().catch(console.error);
