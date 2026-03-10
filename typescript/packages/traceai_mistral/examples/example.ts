/**
 * @traceai/mistral Real-World Example
 *
 * This example demonstrates how to use the Mistral instrumentation with TraceAI
 * for observability of Mistral API calls.
 *
 * Prerequisites:
 * 1. Set environment variables (see .env.example)
 * 2. Install dependencies: pnpm install
 * 3. Run: npx ts-node example.ts
 */

import { register, ProjectType } from "@traceai/fi-core";
import { MistralInstrumentation } from "@traceai/mistral";
import { registerInstrumentations } from "@opentelemetry/instrumentation";
import { diag, DiagConsoleLogger, DiagLogLevel } from "@opentelemetry/api";
import "dotenv/config";

// Enable OpenTelemetry diagnostics for debugging
diag.setLogger(new DiagConsoleLogger(), DiagLogLevel.INFO);

// Validate environment variables
const fiApiKey = process.env.FI_API_KEY;
const fiSecretKey = process.env.FI_SECRET_KEY;
const mistralApiKey = process.env.MISTRAL_API_KEY;

if (!fiApiKey || !fiSecretKey) {
  console.error("FI_API_KEY and FI_SECRET_KEY environment variables must be set.");
  process.exit(1);
}

if (!mistralApiKey) {
  console.error("MISTRAL_API_KEY environment variable must be set.");
  process.exit(1);
}

async function main() {
  console.log("Starting Mistral Instrumentation Example...\n");

  // 1. Register TraceAI Core TracerProvider
  const tracerProvider = register({
    projectName: "mistral-instrumentation-example",
    projectType: ProjectType.OBSERVE,
    sessionName: "mistral-example-session-" + Date.now(),
  });
  console.log("TraceAI Core TracerProvider registered.");

  // 2. Register Mistral Instrumentation BEFORE importing the SDK
  registerInstrumentations({
    tracerProvider: tracerProvider,
    instrumentations: [
      new MistralInstrumentation({
        traceConfig: {
          hideInputs: false,
          hideOutputs: false,
        },
      }),
    ],
  });
  console.log("Mistral Instrumentation registered.\n");

  // 3. Dynamically import Mistral SDK AFTER instrumentation is registered
  const { Mistral } = await import("@mistralai/mistralai");
  const client = new Mistral({ apiKey: mistralApiKey });

  try {
    // === Test Case 1: Basic Chat Completion ===
    console.log("--- Test Case 1: Basic Chat Completion ---");
    const basicResponse = await client.chat.complete({
      model: "mistral-small-latest",
      messages: [
        { role: "user", content: "What is the capital of Japan? Answer in one sentence." }
      ],
    });
    console.log("Response:", basicResponse.choices?.[0]?.message?.content);
    console.log("Usage:", basicResponse.usage);
    console.log();

    // === Test Case 2: Chat with System Prompt ===
    console.log("--- Test Case 2: Chat with System Prompt ---");
    const systemPromptResponse = await client.chat.complete({
      model: "mistral-small-latest",
      messages: [
        { role: "system", content: "You are a helpful assistant that speaks like a pirate." },
        { role: "user", content: "Tell me about the ocean." }
      ],
    });
    console.log("Response:", systemPromptResponse.choices?.[0]?.message?.content);
    console.log();

    // === Test Case 3: Multi-turn Conversation ===
    console.log("--- Test Case 3: Multi-turn Conversation ---");
    const conversationResponse = await client.chat.complete({
      model: "mistral-small-latest",
      messages: [
        { role: "user", content: "My favorite color is blue." },
        { role: "assistant", content: "That's a beautiful choice! Blue is often associated with calmness and tranquility." },
        { role: "user", content: "What's my favorite color?" }
      ],
    });
    console.log("Response:", conversationResponse.choices?.[0]?.message?.content);
    console.log();

    // === Test Case 4: Streaming Chat Completion ===
    console.log("--- Test Case 4: Streaming Chat Completion ---");
    const stream = await client.chat.stream({
      model: "mistral-small-latest",
      messages: [
        { role: "user", content: "Write a short poem about coding." }
      ],
    });

    process.stdout.write("Streaming response: ");
    for await (const chunk of stream) {
      const content = chunk.data?.choices?.[0]?.delta?.content || "";
      process.stdout.write(content);
    }
    console.log("\n");

    // === Test Case 5: JSON Mode ===
    console.log("--- Test Case 5: JSON Mode ---");
    const jsonResponse = await client.chat.complete({
      model: "mistral-small-latest",
      messages: [
        { role: "user", content: "List 3 famous scientists with their field of study. Return as JSON array with 'name' and 'field' keys." }
      ],
      responseFormat: { type: "json_object" },
    });
    console.log("JSON Response:", jsonResponse.choices?.[0]?.message?.content);
    console.log();

    // === Test Case 6: Function/Tool Calling ===
    console.log("--- Test Case 6: Function/Tool Calling ---");
    const toolResponse = await client.chat.complete({
      model: "mistral-small-latest",
      messages: [
        { role: "user", content: "What's the weather like in Paris?" }
      ],
      tools: [
        {
          type: "function",
          function: {
            name: "get_weather",
            description: "Get the current weather in a given location",
            parameters: {
              type: "object",
              properties: {
                location: {
                  type: "string",
                  description: "The city name, e.g. Paris, London"
                },
                unit: {
                  type: "string",
                  enum: ["celsius", "fahrenheit"],
                  description: "The unit of temperature"
                }
              },
              required: ["location"]
            }
          }
        }
      ],
      toolChoice: "auto",
    });

    if (toolResponse.choices?.[0]?.message?.toolCalls) {
      console.log("Tool calls:", JSON.stringify(toolResponse.choices[0].message.toolCalls, null, 2));
    } else {
      console.log("Response:", toolResponse.choices?.[0]?.message?.content);
    }
    console.log();

    // === Test Case 7: Embeddings ===
    console.log("--- Test Case 7: Embeddings ---");
    const embeddingsResponse = await client.embeddings.create({
      model: "mistral-embed",
      inputs: ["Hello, world!", "How are you?", "Mistral AI is awesome!"],
    });
    console.log("Embeddings count:", embeddingsResponse.data?.length);
    console.log("First embedding dimensions:", embeddingsResponse.data?.[0]?.embedding?.length);
    console.log("First embedding (first 5 values):", embeddingsResponse.data?.[0]?.embedding?.slice(0, 5));
    console.log();

    console.log("--- All Test Cases Completed Successfully ---\n");

  } catch (error) {
    console.error("Error during Mistral API calls:", error);
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
