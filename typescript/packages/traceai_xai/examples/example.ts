/**
 * Example: Using xAI (Grok) instrumentation with TraceAI
 *
 * This example demonstrates how to set up and use the @traceai/xai
 * instrumentation to trace xAI API calls.
 *
 * xAI uses OpenAI-compatible API, so you use the OpenAI SDK with a custom baseURL.
 */
import { NodeTracerProvider } from "@opentelemetry/sdk-trace-node";
import { SimpleSpanProcessor, ConsoleSpanExporter } from "@opentelemetry/sdk-trace-base";
import { XAIInstrumentation } from "@traceai/xai";
import OpenAI from "openai";

async function main() {
  // Step 1: Set up the tracer provider
  const provider = new NodeTracerProvider();
  provider.addSpanProcessor(new SimpleSpanProcessor(new ConsoleSpanExporter()));
  provider.register();

  // Step 2: Create and enable the instrumentation
  const instrumentation = new XAIInstrumentation({
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

  // Step 4: Create the xAI client (OpenAI SDK with custom baseURL)
  const client = new OpenAI({
    baseURL: "https://api.x.ai/v1",
    apiKey: process.env.XAI_API_KEY,
  });

  console.log("=== Chat Completion Example ===\n");

  // Example 1: Chat completion with Grok
  const chatResponse = await client.chat.completions.create({
    model: "grok-beta",
    messages: [
      { role: "system", content: "You are Grok, a witty AI assistant." },
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
    model: "grok-beta",
    messages: [
      { role: "user", content: "Tell me a short joke." },
    ],
    max_tokens: 100,
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

  console.log("=== Tool Calling Example ===\n");

  // Example 3: Tool calling with Grok
  const toolResponse = await client.chat.completions.create({
    model: "grok-beta",
    messages: [
      { role: "user", content: "What's the current time in Tokyo?" },
    ],
    tools: [
      {
        type: "function",
        function: {
          name: "get_current_time",
          description: "Get the current time in a specific timezone",
          parameters: {
            type: "object",
            properties: {
              timezone: { type: "string", description: "IANA timezone (e.g., 'Asia/Tokyo')" },
            },
            required: ["timezone"],
          },
        },
      },
    ],
    tool_choice: "auto",
  });

  const toolCall = toolResponse.choices[0]?.message?.tool_calls?.[0];
  if (toolCall) {
    console.log("Tool called:", toolCall.function.name);
    console.log("Arguments:", toolCall.function.arguments);
  } else {
    console.log("Response:", toolResponse.choices[0]?.message?.content);
  }
  console.log("\n");

  console.log("=== Embeddings Example ===\n");

  // Example 4: Embeddings (if available)
  try {
    const embeddingResponse = await client.embeddings.create({
      model: "grok-embed",
      input: "Hello, world!",
    });

    console.log("Embedding dimensions:", embeddingResponse.data[0]?.embedding.length);
    console.log("First 5 values:", embeddingResponse.data[0]?.embedding.slice(0, 5));
  } catch (error) {
    console.log("Embeddings not available with this model/account");
  }
  console.log("\n");

  // Shutdown provider
  await provider.shutdown();
  console.log("Done! Check the console output above for trace spans.");
}

main().catch(console.error);
