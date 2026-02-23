/**
 * Example usage of OpenAI Agents instrumentation with TraceAI.
 *
 * IMPORTANT: Register instrumentation BEFORE importing @openai/agents.
 */

import { register, ProjectType } from "@traceai/fi-core";
import { diag, DiagConsoleLogger, DiagLogLevel } from "@opentelemetry/api";
import { OpenAIAgentsInstrumentation } from "@traceai/openai-agents";
import { registerInstrumentations } from "@opentelemetry/instrumentation";
import * as dotenv from "dotenv";

dotenv.config();

// 1. Enable diagnostic logging (optional, for debugging)
diag.setLogger(new DiagConsoleLogger(), DiagLogLevel.DEBUG);

// 2. Register FI Core TracerProvider
const tracerProvider = register({
  projectName: "openai-agents-example",
  projectType: ProjectType.OBSERVE,
  sessionName: `agents-session-${Date.now()}`,
});

// 3. Register OpenAI Agents instrumentation BEFORE importing the SDK
registerInstrumentations({
  tracerProvider: tracerProvider as any,
  instrumentations: [new OpenAIAgentsInstrumentation()],
});

// 4. Now import and use @openai/agents
import { Agent, run } from "@openai/agents";

async function main() {
  // Create an agent
  const agent = new Agent({
    name: "Assistant",
    instructions: "You are a helpful assistant that answers questions concisely.",
    model: "gpt-4o-mini",
  });

  // Run the agent
  const result = await run(agent, "What is the capital of France?");

  console.log("Agent response:", result.finalOutput);
}

main().catch(console.error);
