/**
 * Example: Using DeepSeek instrumentation with TraceAI
 *
 * This example demonstrates how to set up and use the @traceai/deepseek
 * instrumentation to trace DeepSeek API calls.
 *
 * DeepSeek uses OpenAI-compatible API, so you use the OpenAI SDK with a custom baseURL.
 */
import { NodeTracerProvider } from "@opentelemetry/sdk-trace-node";
import { SimpleSpanProcessor, ConsoleSpanExporter } from "@opentelemetry/sdk-trace-base";
import { DeepSeekInstrumentation } from "@traceai/deepseek";
import OpenAI from "openai";

async function main() {
  // Step 1: Set up the tracer provider
  const provider = new NodeTracerProvider();
  provider.addSpanProcessor(new SimpleSpanProcessor(new ConsoleSpanExporter()));
  provider.register();

  // Step 2: Create and enable the instrumentation
  const instrumentation = new DeepSeekInstrumentation({
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

  // Step 4: Create the DeepSeek client (OpenAI SDK with custom baseURL)
  const client = new OpenAI({
    baseURL: "https://api.deepseek.com/v1",
    apiKey: process.env.DEEPSEEK_API_KEY,
  });

  console.log("=== Chat Completion Example ===\n");

  // Example 1: Chat completion with DeepSeek Chat model
  const chatResponse = await client.chat.completions.create({
    model: "deepseek-chat",
    messages: [
      { role: "system", content: "You are a helpful assistant." },
      { role: "user", content: "What is the capital of France?" },
    ],
    max_tokens: 100,
    temperature: 0.7,
  });

  console.log("Chat Response:", chatResponse.choices[0]?.message?.content);
  console.log("\n");

  console.log("=== DeepSeek R1 Reasoning Example ===\n");

  // Example 2: DeepSeek R1 reasoning model (includes reasoning_content)
  const reasoningResponse = await client.chat.completions.create({
    model: "deepseek-reasoner",
    messages: [
      { role: "user", content: "What is 15 * 27? Show your reasoning." },
    ],
    max_tokens: 500,
  });

  // DeepSeek R1 models include reasoning_content in the response
  const choice = reasoningResponse.choices[0];
  console.log("Answer:", choice?.message?.content);
  // Note: reasoning_content is captured as a trace attribute
  console.log("\n");

  console.log("=== Streaming Chat Example ===\n");

  // Example 3: Streaming chat completion
  const stream = await client.chat.completions.create({
    model: "deepseek-chat",
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

  console.log("=== Tool Calling Example ===\n");

  // Example 4: Tool calling
  const toolResponse = await client.chat.completions.create({
    model: "deepseek-chat",
    messages: [
      { role: "user", content: "What's the weather in Paris?" },
    ],
    tools: [
      {
        type: "function",
        function: {
          name: "get_weather",
          description: "Get the current weather in a location",
          parameters: {
            type: "object",
            properties: {
              location: { type: "string", description: "City name" },
            },
            required: ["location"],
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
  }
  console.log("\n");

  // Shutdown provider
  await provider.shutdown();
  console.log("Done! Check the console output above for trace spans.");
}

main().catch(console.error);
