/**
 * Example of using Pipecat instrumentation with FI tracing.
 *
 * This example shows how to instrument the Pipecat voice AI SDK
 * for observability with OpenTelemetry.
 */

import { NodeTracerProvider } from "@opentelemetry/sdk-trace-node";
import { SimpleSpanProcessor, ConsoleSpanExporter } from "@opentelemetry/sdk-trace-base";
import { PipecatInstrumentation } from "@traceai/fi-instrumentation-pipecat";

// Set up OpenTelemetry tracing
const provider = new NodeTracerProvider();
provider.addSpanProcessor(new SimpleSpanProcessor(new ConsoleSpanExporter()));
provider.register();

// Create and register the instrumentation
const instrumentation = new PipecatInstrumentation({
  traceConfig: {
    hideInputs: false,
    hideOutputs: false,
  },
});
instrumentation.setTracerProvider(provider);
instrumentation.enable();

async function main() {
  // Import Pipecat after instrumentation is set up
  // const { RTVIClient } = await import("@pipecat-ai/client-js");

  console.log("Pipecat instrumentation example");
  console.log("===============================");
  console.log("");
  console.log("This example demonstrates how to set up Pipecat instrumentation.");
  console.log("To run this with actual Pipecat calls, you would:");
  console.log("");
  console.log("1. Install the @pipecat-ai/client-js package:");
  console.log("   npm install @pipecat-ai/client-js");
  console.log("");
  console.log("2. Set up your Pipecat backend server");
  console.log("");
  console.log("3. Uncomment the code below and run:");
  console.log("");

  /*
  // Create RTVI client
  const client = new RTVIClient({
    baseUrl: "http://localhost:7860",
    transport: "websocket",
    callbacks: {
      onConnected: () => {
        console.log("Connected to Pipecat server");
      },
      onDisconnected: () => {
        console.log("Disconnected from Pipecat server");
      },
      onBotReady: () => {
        console.log("Bot is ready");
      },
      onTranscript: (transcript) => {
        console.log("Transcript:", transcript);
      },
    },
  });

  // Connect to server - will be traced
  await client.connect();
  console.log("Connected to Pipecat");

  // Send a text message - will be traced
  await client.sendMessage({
    type: "text",
    content: "Hello, how are you?",
  });

  // Execute an action - will be traced
  const result = await client.action({
    service: "tts",
    action: "say",
    arguments: {
      text: "This is a test message",
    },
  });
  console.log("Action result:", result);

  // Disconnect - will be traced
  await client.disconnect();
  console.log("Disconnected from Pipecat");
  */

  console.log("Instrumentation is active and ready to trace Pipecat calls.");
}

main().catch(console.error);
