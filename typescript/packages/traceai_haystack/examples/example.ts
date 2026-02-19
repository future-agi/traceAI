/**
 * Example of using Haystack instrumentation with FI tracing.
 *
 * This example shows how to instrument the Haystack framework
 * for observability with OpenTelemetry.
 */

import { NodeTracerProvider } from "@opentelemetry/sdk-trace-node";
import { SimpleSpanProcessor, ConsoleSpanExporter } from "@opentelemetry/sdk-trace-base";
import { HaystackInstrumentation } from "@traceai/fi-instrumentation-haystack";

// Set up OpenTelemetry tracing
const provider = new NodeTracerProvider();
provider.addSpanProcessor(new SimpleSpanProcessor(new ConsoleSpanExporter()));
provider.register();

// Create and register the instrumentation
const instrumentation = new HaystackInstrumentation({
  traceConfig: {
    hideInputs: false,
    hideOutputs: false,
  },
});
instrumentation.setTracerProvider(provider);
instrumentation.enable();

async function main() {
  // Import Haystack after instrumentation is set up
  // const { Pipeline, PromptBuilder } = await import("@haystack-ai/haystack");

  console.log("Haystack instrumentation example");
  console.log("================================");
  console.log("");
  console.log("This example demonstrates how to set up Haystack instrumentation.");
  console.log("To run this with actual Haystack pipelines, you would:");
  console.log("");
  console.log("1. Install the @haystack-ai/haystack package:");
  console.log("   npm install @haystack-ai/haystack");
  console.log("");
  console.log("2. Set up your environment:");
  console.log("   export OPENAI_API_KEY=your-api-key");
  console.log("");
  console.log("3. Uncomment the code below and run:");
  console.log("");

  /*
  // Create a simple RAG pipeline
  const pipeline = new Pipeline();

  // Add components to the pipeline
  pipeline.addComponent("retriever", new InMemoryRetriever());
  pipeline.addComponent("prompt_builder", new PromptBuilder());
  pipeline.addComponent("llm", new OpenAIGenerator());

  // Connect components
  pipeline.connect("retriever.documents", "prompt_builder.documents");
  pipeline.connect("prompt_builder.prompt", "llm.prompt");

  // Run the pipeline - will be traced
  const result = await pipeline.run({
    "retriever": { query: "What is machine learning?" },
  });

  console.log("Pipeline result:", result);
  */

  console.log("Instrumentation is active and ready to trace Haystack pipelines.");
}

main().catch(console.error);
