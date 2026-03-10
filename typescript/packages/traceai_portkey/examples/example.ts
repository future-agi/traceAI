/**
 * Example of using Portkey instrumentation with FI tracing.
 *
 * This example shows how to instrument the Portkey AI Gateway SDK
 * for observability with OpenTelemetry.
 */

import { NodeTracerProvider } from "@opentelemetry/sdk-trace-node";
import { SimpleSpanProcessor, ConsoleSpanExporter } from "@opentelemetry/sdk-trace-base";
import { PortkeyInstrumentation } from "@traceai/fi-instrumentation-portkey";

// Set up OpenTelemetry tracing
const provider = new NodeTracerProvider();
provider.addSpanProcessor(new SimpleSpanProcessor(new ConsoleSpanExporter()));
provider.register();

// Create and register the instrumentation
const instrumentation = new PortkeyInstrumentation({
  traceConfig: {
    hideInputs: false,
    hideOutputs: false,
  },
});
instrumentation.setTracerProvider(provider);
instrumentation.enable();

async function main() {
  // Import Portkey after instrumentation is set up
  // const { Portkey } = await import("portkey-ai");

  console.log("Portkey instrumentation example");
  console.log("================================");
  console.log("");
  console.log("This example demonstrates how to set up Portkey instrumentation.");
  console.log("To run this with actual Portkey calls, you would:");
  console.log("");
  console.log("1. Install the portkey-ai package:");
  console.log("   npm install portkey-ai");
  console.log("");
  console.log("2. Set up your environment:");
  console.log("   export PORTKEY_API_KEY=your-api-key");
  console.log("   export OPENAI_API_KEY=your-openai-key");
  console.log("");
  console.log("3. Uncomment the code below and run:");
  console.log("");

  /*
  const portkey = new Portkey({
    apiKey: process.env.PORTKEY_API_KEY,
    virtualKey: process.env.PORTKEY_VIRTUAL_KEY,
  });

  // Chat completion - will be traced
  const chatResponse = await portkey.chat.completions.create({
    model: "gpt-4",
    messages: [
      { role: "system", content: "You are a helpful assistant." },
      { role: "user", content: "What is 2 + 2?" },
    ],
  });
  console.log("Chat response:", chatResponse.choices[0].message.content);

  // Embeddings - will be traced
  const embeddingResponse = await portkey.embeddings.create({
    model: "text-embedding-ada-002",
    input: "Hello, world!",
  });
  console.log("Embedding dimensions:", embeddingResponse.data[0].embedding.length);
  */

  console.log("Instrumentation is active and ready to trace Portkey calls.");
}

main().catch(console.error);
