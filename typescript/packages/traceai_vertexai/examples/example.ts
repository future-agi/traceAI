/**
 * Example of using Vertex AI instrumentation with FI tracing.
 *
 * This example shows how to instrument the Google Cloud Vertex AI SDK
 * for observability with OpenTelemetry.
 */

import { NodeTracerProvider } from "@opentelemetry/sdk-trace-node";
import { SimpleSpanProcessor, ConsoleSpanExporter } from "@opentelemetry/sdk-trace-base";
import { VertexAIInstrumentation } from "@traceai/fi-instrumentation-vertexai";

// Set up OpenTelemetry tracing
const provider = new NodeTracerProvider();
provider.addSpanProcessor(new SimpleSpanProcessor(new ConsoleSpanExporter()));
provider.register();

// Create and register the instrumentation
const instrumentation = new VertexAIInstrumentation({
  traceConfig: {
    hideInputs: false,
    hideOutputs: false,
  },
});
instrumentation.setTracerProvider(provider);
instrumentation.enable();

async function main() {
  // Import Vertex AI after instrumentation is set up
  // const { VertexAI } = await import("@google-cloud/vertexai");

  console.log("Vertex AI instrumentation example");
  console.log("=================================");
  console.log("");
  console.log("This example demonstrates how to set up Vertex AI instrumentation.");
  console.log("To run this with actual Vertex AI calls, you would:");
  console.log("");
  console.log("1. Install the @google-cloud/vertexai package:");
  console.log("   npm install @google-cloud/vertexai");
  console.log("");
  console.log("2. Set up your Google Cloud credentials:");
  console.log("   export GOOGLE_APPLICATION_CREDENTIALS=/path/to/credentials.json");
  console.log("   OR run: gcloud auth application-default login");
  console.log("");
  console.log("3. Uncomment the code below and run:");
  console.log("");

  /*
  // Initialize Vertex AI
  const vertexAI = new VertexAI({
    project: process.env.GOOGLE_CLOUD_PROJECT!,
    location: "us-central1",
  });

  // Get the generative model
  const model = vertexAI.getGenerativeModel({
    model: "gemini-1.5-pro",
  });

  // Generate content - will be traced
  const result = await model.generateContent({
    contents: [
      {
        role: "user",
        parts: [{ text: "What is the capital of France?" }],
      },
    ],
  });

  console.log("Response:", result.response.candidates?.[0]?.content?.parts?.[0]?.text);

  // Generate with streaming - will be traced
  const streamResult = await model.generateContentStream({
    contents: [
      {
        role: "user",
        parts: [{ text: "Write a short poem about AI." }],
      },
    ],
  });

  console.log("Streaming response:");
  for await (const chunk of streamResult.stream) {
    const text = chunk.candidates?.[0]?.content?.parts?.[0]?.text;
    if (text) {
      process.stdout.write(text);
    }
  }
  console.log("\n");

  // Start a chat session - messages will be traced
  const chat = model.startChat();
  const chatResponse = await chat.sendMessage("Hello! How are you?");
  console.log("Chat response:", chatResponse.response.candidates?.[0]?.content?.parts?.[0]?.text);

  // Count tokens - will be traced
  const tokenCount = await model.countTokens({
    contents: [{ role: "user", parts: [{ text: "Hello world" }] }],
  });
  console.log("Token count:", tokenCount.totalTokens);
  */

  console.log("Instrumentation is active and ready to trace Vertex AI calls.");
}

main().catch(console.error);
