/**
 * @traceai/groq Real-World Example
 *
 * This example demonstrates how to use the Groq instrumentation with TraceAI
 * for observability of Groq API calls.
 *
 * Prerequisites:
 * 1. Set environment variables (see .env.example)
 * 2. Install dependencies: pnpm install
 * 3. Run: npx ts-node example.ts
 */

import { register, ProjectType } from "@traceai/fi-core";
import { GroqInstrumentation } from "@traceai/groq";
import { registerInstrumentations } from "@opentelemetry/instrumentation";
import { diag, DiagConsoleLogger, DiagLogLevel } from "@opentelemetry/api";
import "dotenv/config";

// Enable OpenTelemetry diagnostics for debugging
diag.setLogger(new DiagConsoleLogger(), DiagLogLevel.INFO);

// Validate environment variables
const fiApiKey = process.env.FI_API_KEY;
const fiSecretKey = process.env.FI_SECRET_KEY;
const groqApiKey = process.env.GROQ_API_KEY;

if (!fiApiKey || !fiSecretKey) {
  console.error("FI_API_KEY and FI_SECRET_KEY environment variables must be set.");
  process.exit(1);
}

if (!groqApiKey) {
  console.error("GROQ_API_KEY environment variable must be set.");
  process.exit(1);
}

async function main() {
  console.log("Starting Groq Instrumentation Example...\n");

  // 1. Register TraceAI Core TracerProvider
  const tracerProvider = register({
    projectName: "groq-instrumentation-example",
    projectType: ProjectType.OBSERVE,
    sessionName: "groq-example-session-" + Date.now(),
  });
  console.log("TraceAI Core TracerProvider registered.");

  // 2. Register Groq Instrumentation BEFORE importing the SDK
  registerInstrumentations({
    tracerProvider: tracerProvider,
    instrumentations: [
      new GroqInstrumentation({
        traceConfig: {
          hideInputs: false,  // Set to true to hide input content in traces
          hideOutputs: false, // Set to true to hide output content in traces
        },
      }),
    ],
  });
  console.log("Groq Instrumentation registered.\n");

  // 3. Dynamically import Groq SDK AFTER instrumentation is registered
  const Groq = (await import("groq-sdk")).default;
  const client = new Groq({ apiKey: groqApiKey });

  try {
    // === Test Case 1: Basic Chat Completion ===
    console.log("--- Test Case 1: Basic Chat Completion ---");
    const basicResponse = await client.chat.completions.create({
      model: "llama-3.1-8b-instant",
      messages: [
        { role: "user", content: "What is the capital of France? Answer in one sentence." }
      ],
      max_tokens: 100,
    });
    console.log("Response:", basicResponse.choices[0]?.message?.content);
    console.log("Usage:", basicResponse.usage);
    console.log();

    // === Test Case 2: Chat with System Prompt ===
    console.log("--- Test Case 2: Chat with System Prompt ---");
    const systemPromptResponse = await client.chat.completions.create({
      model: "llama-3.1-8b-instant",
      messages: [
        { role: "system", content: "You are a helpful assistant that responds in haiku format." },
        { role: "user", content: "Describe a sunset." }
      ],
      max_tokens: 100,
    });
    console.log("Response:", systemPromptResponse.choices[0]?.message?.content);
    console.log();

    // === Test Case 3: Multi-turn Conversation ===
    console.log("--- Test Case 3: Multi-turn Conversation ---");
    const conversationResponse = await client.chat.completions.create({
      model: "llama-3.1-8b-instant",
      messages: [
        { role: "user", content: "My name is Alice." },
        { role: "assistant", content: "Hello Alice! Nice to meet you. How can I help you today?" },
        { role: "user", content: "What's my name?" }
      ],
      max_tokens: 50,
    });
    console.log("Response:", conversationResponse.choices[0]?.message?.content);
    console.log();

    // === Test Case 4: Streaming Chat Completion ===
    console.log("--- Test Case 4: Streaming Chat Completion ---");
    const stream = await client.chat.completions.create({
      model: "llama-3.1-8b-instant",
      messages: [
        { role: "user", content: "Count from 1 to 5 slowly." }
      ],
      max_tokens: 100,
      stream: true,
    });

    process.stdout.write("Streaming response: ");
    for await (const chunk of stream) {
      const content = chunk.choices[0]?.delta?.content || "";
      process.stdout.write(content);
    }
    console.log("\n");

    // === Test Case 5: JSON Mode ===
    console.log("--- Test Case 5: JSON Mode ---");
    const jsonResponse = await client.chat.completions.create({
      model: "llama-3.1-8b-instant",
      messages: [
        { role: "system", content: "You are a helpful assistant that responds in JSON format." },
        { role: "user", content: "List 3 programming languages with their year of creation. Return as JSON array." }
      ],
      max_tokens: 200,
      response_format: { type: "json_object" },
    });
    console.log("JSON Response:", jsonResponse.choices[0]?.message?.content);
    console.log();

    // === Test Case 6: Tool/Function Calling ===
    console.log("--- Test Case 6: Tool/Function Calling ---");
    const toolResponse = await client.chat.completions.create({
      model: "llama-3.3-70b-versatile",
      messages: [
        { role: "user", content: "What's the weather in San Francisco?" }
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
                  description: "The city and state, e.g. San Francisco, CA"
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
      tool_choice: "auto",
      max_tokens: 200,
    });

    if (toolResponse.choices[0]?.message?.tool_calls) {
      console.log("Tool calls:", JSON.stringify(toolResponse.choices[0].message.tool_calls, null, 2));
    } else {
      console.log("Response:", toolResponse.choices[0]?.message?.content);
    }
    console.log();

    console.log("--- All Test Cases Completed Successfully ---\n");

  } catch (error) {
    console.error("Error during Groq API calls:", error);
  } finally {
    // 4. Shutdown the provider to ensure all spans are flushed
    console.log("Shutting down tracer provider...");
    await new Promise(resolve => setTimeout(resolve, 2000)); // Wait for spans to flush
    await tracerProvider.shutdown();
    console.log("Tracer provider shut down successfully.");
  }
}

main().catch((error) => {
  console.error("Unhandled error:", error);
  process.exit(1);
});
