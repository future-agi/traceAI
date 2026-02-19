/**
 * Example usage of Strands instrumentation with TraceAI.
 *
 * IMPORTANT: Register instrumentation BEFORE importing @strands-agents/sdk.
 */

import { register, ProjectType } from "@traceai/fi-core";
import { diag, DiagConsoleLogger, DiagLogLevel } from "@opentelemetry/api";
import { StrandsInstrumentation } from "@traceai/strands";
import { registerInstrumentations } from "@opentelemetry/instrumentation";
import * as dotenv from "dotenv";

dotenv.config();

// 1. Enable diagnostic logging (optional, for debugging)
diag.setLogger(new DiagConsoleLogger(), DiagLogLevel.DEBUG);

// 2. Register FI Core TracerProvider
const tracerProvider = register({
  projectName: "strands-example",
  projectType: ProjectType.OBSERVE,
  sessionName: `strands-session-${Date.now()}`,
});

// 3. Register Strands instrumentation BEFORE importing the SDK
registerInstrumentations({
  tracerProvider: tracerProvider as any,
  instrumentations: [new StrandsInstrumentation()],
});

// 4. Now import and use @strands-agents/sdk
import { Agent } from "@strands-agents/sdk";

async function main() {
  // Create a Strands Agent with Bedrock model
  const agent = new Agent({
    model: "us.anthropic.claude-sonnet-4-20250514-v1:0",
    systemPrompt: "You are a helpful assistant that answers questions concisely.",
  });

  // Invoke the agent
  const response = await agent.invoke("What is the capital of France?");

  console.log("Agent response:", response);
}

main().catch(console.error);
