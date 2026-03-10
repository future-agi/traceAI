/**
 * Example of using Guardrails instrumentation with FI tracing.
 *
 * This example shows how to instrument the Guardrails AI framework
 * for observability with OpenTelemetry.
 */

import { NodeTracerProvider } from "@opentelemetry/sdk-trace-node";
import { SimpleSpanProcessor, ConsoleSpanExporter } from "@opentelemetry/sdk-trace-base";
import { GuardrailsInstrumentation } from "@traceai/fi-instrumentation-guardrails";

// Set up OpenTelemetry tracing
const provider = new NodeTracerProvider();
provider.addSpanProcessor(new SimpleSpanProcessor(new ConsoleSpanExporter()));
provider.register();

// Create and register the instrumentation
const instrumentation = new GuardrailsInstrumentation({
  traceConfig: {
    hideInputs: false,
    hideOutputs: false,
  },
});
instrumentation.setTracerProvider(provider);
instrumentation.enable();

async function main() {
  // Import Guardrails after instrumentation is set up
  // const { Guard, Validator } = await import("@guardrails-ai/core");

  console.log("Guardrails instrumentation example");
  console.log("==================================");
  console.log("");
  console.log("This example demonstrates how to set up Guardrails instrumentation.");
  console.log("To run this with actual Guardrails guards, you would:");
  console.log("");
  console.log("1. Install the @guardrails-ai/core package:");
  console.log("   npm install @guardrails-ai/core");
  console.log("");
  console.log("2. Set up your environment:");
  console.log("   export OPENAI_API_KEY=your-api-key");
  console.log("");
  console.log("3. Uncomment the code below and run:");
  console.log("");

  /*
  // Define a guard with validators
  const guard = new Guard({
    name: "email_validator",
    validators: [
      new RegexMatch({
        regex: /^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$/,
        onFail: "reask",
      }),
    ],
  });

  // Validate input - will be traced
  const result = await guard.validate("user@example.com");
  console.log("Validation result:", result);

  // Parse with guard - will be traced
  const parseResult = await guard.parse("Please extract: john@company.org");
  console.log("Parse result:", parseResult);
  */

  console.log("Instrumentation is active and ready to trace Guardrails operations.");
}

main().catch(console.error);
