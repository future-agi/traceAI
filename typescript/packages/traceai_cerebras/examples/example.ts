/**
 * Example: Using Cerebras instrumentation with TraceAI
 *
 * This example demonstrates how to set up and use the @traceai/cerebras
 * instrumentation to trace Cerebras Cloud SDK calls.
 *
 * Cerebras uses its own SDK (@cerebras/cerebras_cloud_sdk), not the OpenAI SDK.
 */
import { NodeTracerProvider } from "@opentelemetry/sdk-trace-node";
import { SimpleSpanProcessor, ConsoleSpanExporter } from "@opentelemetry/sdk-trace-base";
import { CerebrasInstrumentation } from "@traceai/cerebras";
import Cerebras from "@cerebras/cerebras_cloud_sdk";

async function main() {
  // Step 1: Set up the tracer provider
  const provider = new NodeTracerProvider();
  provider.addSpanProcessor(new SimpleSpanProcessor(new ConsoleSpanExporter()));
  provider.register();

  // Step 2: Create and enable the instrumentation
  const instrumentation = new CerebrasInstrumentation({
    traceConfig: {
      // Optional: Configure tracing options
      // hideInputs: true,  // Hide input content in traces
      // hideOutputs: true, // Hide output content in traces
    },
  });
  instrumentation.setTracerProvider(provider);
  instrumentation.enable();

  // Step 3: Import and patch the Cerebras module
  const cerebrasModule = await import("@cerebras/cerebras_cloud_sdk");
  instrumentation.manuallyInstrument(cerebrasModule);

  // Step 4: Create the Cerebras client
  const client = new Cerebras({
    apiKey: process.env.CEREBRAS_API_KEY,
  });

  console.log("=== Chat Completion Example ===\n");

  // Example 1: Chat completion
  const chatResponse = await client.chat.completions.create({
    model: "llama3.1-8b",
    messages: [
      { role: "system", content: "You are a helpful assistant." },
      { role: "user", content: "What is the capital of France?" },
    ],
    max_tokens: 100,
    temperature: 0.7,
  });

  console.log("Chat Response:", chatResponse.choices[0]?.message?.content);

  // Cerebras provides detailed time_info metrics
  if (chatResponse.time_info) {
    console.log("\nTime Info (Cerebras-specific):");
    console.log("  Queue time:", chatResponse.time_info.queue_time, "s");
    console.log("  Prompt time:", chatResponse.time_info.prompt_time, "s");
    console.log("  Completion time:", chatResponse.time_info.completion_time, "s");
    console.log("  Total time:", chatResponse.time_info.total_time, "s");
  }
  console.log("\n");

  console.log("=== Streaming Chat Example ===\n");

  // Example 2: Streaming chat completion
  const stream = await client.chat.completions.create({
    model: "llama3.1-8b",
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

  console.log("=== Fast Inference Example ===\n");

  // Example 3: Cerebras is known for ultra-fast inference
  const startTime = Date.now();

  const fastResponse = await client.chat.completions.create({
    model: "llama3.1-70b",
    messages: [
      { role: "user", content: "Summarize the benefits of AI in healthcare in one paragraph." },
    ],
    max_tokens: 200,
  });

  const endTime = Date.now();
  console.log("Response:", fastResponse.choices[0]?.message?.content);
  console.log(`\nClient-side latency: ${endTime - startTime}ms`);

  if (fastResponse.usage) {
    const tokensPerSecond = fastResponse.usage.completion_tokens /
      (fastResponse.time_info?.completion_time || 1);
    console.log(`Server throughput: ~${Math.round(tokensPerSecond)} tokens/second`);
  }
  console.log("\n");

  // Shutdown provider
  await provider.shutdown();
  console.log("Done! Check the console output above for trace spans.");
}

main().catch(console.error);
