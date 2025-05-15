import { register, ProjectType } from "@traceai/fi-core";
import { AnthropicInstrumentation } from "@traceai/anthropic";
import { registerInstrumentations } from "@opentelemetry/instrumentation";
import { diag, DiagConsoleLogger, DiagLogLevel } from "@opentelemetry/api";
// Anthropic SDK will be imported dynamically AFTER instrumentation is registered.
// Import the SDK for type usage only at the top level.
import type AnthropicSDK from "@anthropic-ai/sdk";

// Enable OpenTelemetry internal diagnostics (optional, but good for debugging)
diag.setLogger(new DiagConsoleLogger(), DiagLogLevel.INFO);

// Ensure your ANTHROPIC_API_KEY environment variable is set
if (!process.env.ANTHROPIC_API_KEY) {
  console.error("ANTHROPIC_API_KEY environment variable is not set.");
  process.exit(1);
}

// FI Telemetry Credentials (similar to the OpenAI example)
const fiApiKey = process.env.FI_API_KEY;
const fiSecretKey = process.env.FI_SECRET_KEY;
if (!fiApiKey || !fiSecretKey) {
  console.error(
    "FI_API_KEY and FI_SECRET_KEY environment variables must be set for TraceAI telemetry.",
  );
  process.exit(1);
}

// Define dummyTools using the imported SDK type AnthropicSDK.Tool
const dummyTools: AnthropicSDK.Tool[] = [
  {
    name: "get_weather",
    description: "Get the current weather in a given location",
    input_schema: {
      type: "object",
      properties: {
        location: { type: "string", description: "The city and state, e.g. San Francisco, CA" },
      },
      required: ["location"],
    },
  },
  {
    name: "get_stock_price",
    description: "Get the current stock price for a given ticker symbol",
    input_schema: {
      type: "object",
      properties: {
        ticker: { type: "string", description: "The stock ticker symbol, e.g. GOOG" },
      },
      required: ["ticker"],
    },
  },
];

async function main() {
  // 1. Register FI Core TracerProvider (sets up exporter and provider)
  const tracerProvider = register({
    projectName: "ts-anthropic-instrumentation", 
    projectType: ProjectType.OBSERVE,
    sessionName: "anthropic-otel-test-session-" + Date.now(),
  });
  console.log("TraceAI Core TracerProvider registered.");

  // 2. Initialize and enable Anthropic Instrumentation
  const anthropicInstrumentation = new AnthropicInstrumentation({});

  registerInstrumentations({
    instrumentations: [anthropicInstrumentation],
    tracerProvider: tracerProvider,
  });
  console.log("Anthropic Instrumentation registered.");

  // 3. Dynamically import Anthropic SDK AFTER instrumentation is registered
  // The runtime Anthropic object is what we use to make calls.
  const Anthropic = (await import("@anthropic-ai/sdk")).default;
  const client = new Anthropic();

  try {
    // === Test Case 1: Non-streaming simple call (already exists) ===
    console.log("\n--- Test Case 1: Non-Streaming Anthropic Call ---");
    const nonStreamMessage = await client.messages.create({
      model: "claude-3-haiku-20240307",
      max_tokens: 50,
      messages: [{ role: "user", content: "Hello, Claude! Write a short haiku." }],
    });
    console.log("Non-streaming response content:", nonStreamMessage.content);
    if (nonStreamMessage.usage) {
        console.log("Usage (non-streaming):", nonStreamMessage.usage.input_tokens, "input,", nonStreamMessage.usage.output_tokens, "output tokens.");
    }

    // === Test Case 2: Non-streaming with System Prompt ===
    console.log("\n--- Test Case 2: Non-Streaming with System Prompt ---");
    const systemPromptMessage = await client.messages.create({
        model: "claude-3-haiku-20240307",
        max_tokens: 70,
        system: "You are a helpful assistant that speaks in pirate dialect.",
        messages: [{ role: "user", content: "How are you today?" }],
    });
    console.log("System prompt response content:", systemPromptMessage.content);
    if (systemPromptMessage.usage) {
        console.log("Usage (system prompt):", systemPromptMessage.usage.input_tokens, "input,", systemPromptMessage.usage.output_tokens, "output tokens.");
    }

    // === Test Case 3: Non-streaming with Tool Use ===
    console.log("\n--- Test Case 3: Non-Streaming with Tool Use ---");
    const toolUseMessage = await client.messages.create({
        model: "claude-3-haiku-20240307",
        max_tokens: 150,
        tools: dummyTools,
        messages: [{ role: "user", content: "What is the weather in San Francisco?" }],
    });
    console.log("Tool use response content:", toolUseMessage.content);
    if (toolUseMessage.content.some(block => block.type === 'tool_use')) {
        console.log("Tool use detected in response.");
    }
    if (toolUseMessage.usage) {
        console.log("Usage (tool use):", toolUseMessage.usage.input_tokens, "input,", toolUseMessage.usage.output_tokens, "output tokens.");
    }

    // === Test Case 4: Streaming simple call (reverted to manual iteration) ===
    console.log("\n--- Test Case 4: Streaming Anthropic Call (manual iteration) ---");
    const streamTC4 = client.messages.stream({
        model: "claude-3-haiku-20240307",
        max_tokens: 100,
        messages: [{ role: "user", content: "Hello, Claude! Tell me a 2-sentence short story." }],
    });

    console.log("\nStreaming response content (TC4 - will print as it arrives):");
    let accumulatedTextTC4 = "";
    
    streamTC4.on('text', (text) => {
        process.stdout.write(text);
        accumulatedTextTC4 += text;
    });

    const finalMessageTC4 = await streamTC4.finalMessage();
    console.log("\n--- Stream ended (TC4 finalMessage received) ---");
    if (finalMessageTC4.usage) {
        console.log(`Usage (streaming TC4): Input: ${finalMessageTC4.usage.input_tokens}, Output: ${finalMessageTC4.usage.output_tokens}`);
    }
    console.log("Final accumulated text (TC4 from .on('text')):", accumulatedTextTC4);


    // === Test Case 5: Streaming with Tool Use (reverted to manual iteration) ===
    console.log("\n--- Test Case 5: Streaming with Tool Use (manual iteration) ---");
    const streamTC5 = client.messages.stream({
        model: "claude-3-haiku-20240307",
        max_tokens: 200,
        tools: dummyTools,
        messages: [{ role: "user", content: "What is the current price of GOOG stock?" }],
    });
    
    console.log("\nStreaming response content (TC5 - will print as it arrives):");
    let accumulatedTextTC5 = "";

    streamTC5.on('text', (textChunk) => {
        process.stdout.write(textChunk);
        accumulatedTextTC5 += textChunk;
    });

    const finalMessageTC5 = await streamTC5.finalMessage();
    console.log("\n--- Stream ended (TC5 finalMessage received) ---");

    if (finalMessageTC5.usage) {
        console.log(`Usage (streaming TC5): Input: ${finalMessageTC5.usage.input_tokens}, Output: ${finalMessageTC5.usage.output_tokens}`);
    }
    console.log("Final accumulated text (TC5 from .on('text')):", accumulatedTextTC5);
    
    const detectedToolUsesTC5 = finalMessageTC5.content.filter(
        (block): block is AnthropicSDK.ToolUseBlock => block.type === 'tool_use'
    );

    if (detectedToolUsesTC5.length > 0) {
        console.log("Detected tool uses in stream (TC5 from finalMessage):");
        detectedToolUsesTC5.forEach(toolUse => {
            console.log(`  Tool: ID=${toolUse.id}, Name=${toolUse.name}, Input:`, toolUse.input);
        });
    }

    console.log("\n--- All Test Cases Completed ---");

  } catch (error) {
    console.error("Error during Anthropic API call tests:", error);
  } finally {
    console.log("\nShutting down OpenTelemetry provider in 5 seconds...");
    setTimeout(() => {
        tracerProvider.shutdown()
        .then(() => console.log("OpenTelemetry provider shutdown complete."))
        .catch((shutdownError: Error) => console.error("Error shutting down OpenTelemetry provider:", shutdownError.message));
    }, 5000); 
  }
}

main().catch((error) => {
  console.error("Unhandled error in main function:", error);
  process.exit(1);
});
