/**
 * Example usage of BeeAI instrumentation with TraceAI.
 *
 * IMPORTANT: Register instrumentation BEFORE importing beeai-framework.
 */

import { register, ProjectType } from "@traceai/fi-core";
import { diag, DiagConsoleLogger, DiagLogLevel } from "@opentelemetry/api";
import { BeeAIInstrumentation } from "@traceai/beeai";
import { registerInstrumentations } from "@opentelemetry/instrumentation";
import * as dotenv from "dotenv";

dotenv.config();

// 1. Enable diagnostic logging (optional, for debugging)
diag.setLogger(new DiagConsoleLogger(), DiagLogLevel.DEBUG);

// 2. Register FI Core TracerProvider
const tracerProvider = register({
  projectName: "beeai-example",
  projectType: ProjectType.OBSERVE,
  sessionName: `beeai-session-${Date.now()}`,
});

// 3. Register BeeAI instrumentation BEFORE importing the SDK
registerInstrumentations({
  tracerProvider: tracerProvider as any,
  instrumentations: [new BeeAIInstrumentation()],
});

// 4. Now import and use beeai-framework
import { BeeAgent } from "beeai-framework/agents/bee/agent";
import { OllamaLLM } from "beeai-framework/adapters/ollama/llm";
import { TokenMemory } from "beeai-framework/memory/tokenMemory";

async function main() {
  // Create an LLM adapter
  const llm = new OllamaLLM({
    modelId: "llama3.1",
  });

  // Create a BeeAgent
  const agent = new BeeAgent({
    llm,
    memory: new TokenMemory({ llm }),
    tools: [],
  });

  // Run the agent
  const response = await agent.run(
    { prompt: "What is the capital of France?" },
    { execution: { maxIterations: 3 } }
  );

  console.log("Agent response:", response.result?.text);
}

main().catch(console.error);
