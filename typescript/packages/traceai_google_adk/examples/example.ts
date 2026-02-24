/**
 * Example of using Google ADK instrumentation with FI tracing.
 *
 * This example shows how to instrument the Google AI Development Kit
 * for observability with OpenTelemetry.
 */

import { NodeTracerProvider } from "@opentelemetry/sdk-trace-node";
import { SimpleSpanProcessor, ConsoleSpanExporter } from "@opentelemetry/sdk-trace-base";
import { GoogleADKInstrumentation } from "@traceai/fi-instrumentation-google-adk";

// Set up OpenTelemetry tracing
const provider = new NodeTracerProvider();
provider.addSpanProcessor(new SimpleSpanProcessor(new ConsoleSpanExporter()));
provider.register();

// Create and register the instrumentation
const instrumentation = new GoogleADKInstrumentation({
  traceConfig: {
    hideInputs: false,
    hideOutputs: false,
  },
});
instrumentation.setTracerProvider(provider);
instrumentation.enable();

async function main() {
  // Import Google ADK after instrumentation is set up
  // const { Agent, Tool, Runner } = await import("@google/adk");

  console.log("Google ADK instrumentation example");
  console.log("==================================");
  console.log("");
  console.log("This example demonstrates how to set up Google ADK instrumentation.");
  console.log("To run this with actual Google ADK calls, you would:");
  console.log("");
  console.log("1. Install the @google/adk package:");
  console.log("   npm install @google/adk");
  console.log("");
  console.log("2. Set up your Google Cloud credentials:");
  console.log("   export GOOGLE_APPLICATION_CREDENTIALS=/path/to/credentials.json");
  console.log("   OR run: gcloud auth application-default login");
  console.log("");
  console.log("3. Uncomment the code below and run:");
  console.log("");

  /*
  // Define tools for the agent
  const searchTool = new Tool({
    name: "search",
    description: "Search the web for information",
    parameters: {
      type: "object",
      properties: {
        query: { type: "string", description: "Search query" },
      },
      required: ["query"],
    },
    execute: async (params) => {
      // Simulated search - will be traced
      return { results: [`Results for: ${params.query}`] };
    },
  });

  const calculatorTool = new Tool({
    name: "calculator",
    description: "Perform mathematical calculations",
    parameters: {
      type: "object",
      properties: {
        expression: { type: "string", description: "Math expression" },
      },
      required: ["expression"],
    },
    execute: async (params) => {
      // Simulated calculator - will be traced
      return { result: eval(params.expression) };
    },
  });

  // Create an agent
  const agent = new Agent({
    name: "ResearchAssistant",
    model: "gemini-1.5-pro",
    systemPrompt: "You are a helpful research assistant.",
    tools: [searchTool, calculatorTool],
  });

  // Run the agent - will be traced
  const result = await agent.run("What is 25 * 4 and find information about AI?");
  console.log("Agent response:", result.output);

  // Use invoke for single-turn - will be traced
  const invokeResult = await agent.invoke("What is the weather like?");
  console.log("Invoke response:", invokeResult.response);

  // Use stream for real-time responses - will be traced
  console.log("Streaming response:");
  for await (const chunk of agent.stream("Tell me a story about robots.")) {
    if (chunk.content) {
      process.stdout.write(chunk.content);
    }
  }
  console.log("\n");

  // Use Runner for more control - will be traced
  const runner = new Runner({ agent });
  const runnerResult = await runner.run({
    message: "Summarize the latest news",
    context: { topic: "technology" },
  });
  console.log("Runner result:", runnerResult.result);
  */

  console.log("Instrumentation is active and ready to trace Google ADK calls.");
}

main().catch(console.error);
