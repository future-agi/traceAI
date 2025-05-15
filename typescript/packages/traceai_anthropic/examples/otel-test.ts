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
    const streamTC4 = await client.messages.create({
        model: "claude-3-haiku-20240307",
        max_tokens: 100,
        messages: [{ role: "user", content: "Hello, Claude! Tell me a 2-sentence short story." }],
        stream: true,
    });

    console.log("\nStreaming response content (TC4 - will print as it arrives):");
    let accumulatedTextTC4 = "";
    let finalInputTokensTC4: number | undefined = undefined;
    let currentOutputTokensTC4 = 0;

    for await (const event of streamTC4) {
        if (event.type === "message_start" && event.message.usage) {
            finalInputTokensTC4 = event.message.usage.input_tokens;
        } else if (event.type === "content_block_delta" && event.delta.type === "text_delta") {
            process.stdout.write(event.delta.text);
            accumulatedTextTC4 += event.delta.text;
        } else if (event.type === "message_delta" && event.usage) {
            currentOutputTokensTC4 += event.usage.output_tokens;
        } else if (event.type === "message_stop") {
            console.log("\n--- Stream ended (TC4 message_stop event) ---");
            if (finalInputTokensTC4 !== undefined) {
                 console.log(`Usage (streaming TC4): Input: ${finalInputTokensTC4}, Output: ${currentOutputTokensTC4}`);
            } else {
                 console.log(`Usage (streaming TC4 - input tokens not captured): Output: ${currentOutputTokensTC4}`);
            }
        }
    }
    console.log("Final accumulated text (TC4):", accumulatedTextTC4);


    // === Test Case 5: Streaming with Tool Use (reverted to manual iteration) ===
    console.log("\n--- Test Case 5: Streaming with Tool Use (manual iteration) ---");
    const streamTC5 = await client.messages.create({
        model: "claude-3-haiku-20240307",
        max_tokens: 200,
        tools: dummyTools,
        messages: [{ role: "user", content: "What is the current price of GOOG stock?" }],
        stream: true,
    });
    
    console.log("\nStreaming response content (TC5 - will print as it arrives):");
    let accumulatedTextTC5 = "";
    let finalInputTokensTC5: number | undefined = undefined;
    let currentOutputTokensTC5 = 0;
    const detectedToolUsesTC5: AnthropicSDK.ToolUseBlock[] = [];

    for await (const event of streamTC5) {
        if (event.type === "message_start" && event.message.usage) {
            finalInputTokensTC5 = event.message.usage.input_tokens;
        } else if (event.type === "content_block_start" && event.content_block.type === "tool_use") {
            // Initialize tool_use block
            detectedToolUsesTC5.push({
                id: event.content_block.id,
                input: {},
                name: event.content_block.name,
                type: "tool_use",
            });
            console.log(`\nStream event: content_block_start (tool_use) detected. Name: ${event.content_block.name}, ID: ${event.content_block.id}`);
        } else if (event.type === "content_block_delta" && event.delta.type === "input_json_delta") {
            // Append to the input of the last detected tool_use block
            const currentTool = detectedToolUsesTC5[detectedToolUsesTC5.length - 1];
            if (currentTool) {
                // This is a simplified accumulation; proper JSON delta parsing might be needed for complex inputs
                currentTool.input = { ...(currentTool.input || {}), ...JSON.parse(event.delta.partial_json || "{}") }; 
                // A more robust way for partial_json is to accumulate the string and parse once at content_block_stop
                // For now, this just prints the delta
                 process.stdout.write(event.delta.partial_json);
            }
        } else if (event.type === "content_block_delta" && event.delta.type === "text_delta") {
            process.stdout.write(event.delta.text);
            accumulatedTextTC5 += event.delta.text;
        } else if (event.type === "message_delta" && event.usage) {
            currentOutputTokensTC5 += event.usage.output_tokens;
        } else if (event.type === "message_stop") {
            console.log("\n--- Stream ended (TC5 message_stop event) ---");
            if (finalInputTokensTC5 !== undefined) {
                 console.log(`Usage (streaming TC5): Input: ${finalInputTokensTC5}, Output: ${currentOutputTokensTC5}`);
            } else {
                 console.log(`Usage (streaming TC5 - input tokens not captured): Output: ${currentOutputTokensTC5}`);
            }
        }
    }
    console.log("Final accumulated text (TC5):", accumulatedTextTC5);
    if (detectedToolUsesTC5.length > 0) {
        console.log("Detected tool uses in stream (TC5):");
        detectedToolUsesTC5.forEach(tool => {
            console.log(`  Tool: ID=${tool.id}, Name=${tool.name}, Input:`, tool.input);
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
