/**
 * Example of using Instructor instrumentation with FI tracing.
 *
 * This example shows how to instrument the Instructor library
 * for observability with OpenTelemetry.
 */

import { NodeTracerProvider } from "@opentelemetry/sdk-trace-node";
import { SimpleSpanProcessor, ConsoleSpanExporter } from "@opentelemetry/sdk-trace-base";
import { InstructorInstrumentation } from "@traceai/fi-instrumentation-instructor";

// Set up OpenTelemetry tracing
const provider = new NodeTracerProvider();
provider.addSpanProcessor(new SimpleSpanProcessor(new ConsoleSpanExporter()));
provider.register();

// Create and register the instrumentation
const instrumentation = new InstructorInstrumentation({
  traceConfig: {
    hideInputs: false,
    hideOutputs: false,
  },
});
instrumentation.setTracerProvider(provider);
instrumentation.enable();

async function main() {
  // Import Instructor after instrumentation is set up
  // const Instructor = (await import("@instructor-ai/instructor")).default;
  // const OpenAI = (await import("openai")).default;

  console.log("Instructor instrumentation example");
  console.log("==================================");
  console.log("");
  console.log("This example demonstrates how to set up Instructor instrumentation.");
  console.log("To run this with actual Instructor calls, you would:");
  console.log("");
  console.log("1. Install the required packages:");
  console.log("   npm install @instructor-ai/instructor openai zod");
  console.log("");
  console.log("2. Set up your environment:");
  console.log("   export OPENAI_API_KEY=your-api-key");
  console.log("");
  console.log("3. Uncomment the code below and run:");
  console.log("");

  /*
  import { z } from "zod";

  // Define the schema for structured extraction
  const UserSchema = z.object({
    name: z.string(),
    age: z.number(),
    email: z.string().email(),
  });

  // Create the instructor client
  const openai = new OpenAI();
  const client = Instructor({
    client: openai,
    mode: "FUNCTIONS",
  });

  // Extract structured data - will be traced
  const user = await client.chat.completions.create({
    model: "gpt-4",
    messages: [
      {
        role: "user",
        content: "John Doe is 30 years old and his email is john@example.com",
      },
    ],
    response_model: { schema: UserSchema, name: "User" },
  });

  console.log("Extracted user:", user);
  // Output: { name: "John Doe", age: 30, email: "john@example.com" }
  */

  console.log("Instrumentation is active and ready to trace Instructor calls.");
}

main().catch(console.error);
