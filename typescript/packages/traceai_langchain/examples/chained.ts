import { register, ProjectType } from "@traceai/fi-core";
import { registerInstrumentations } from "@opentelemetry/instrumentation";
import { diag, DiagConsoleLogger, DiagLogLevel } from "@opentelemetry/api";
import { LangChainInstrumentation } from "../src";
import "./instrumentationchat";
import "dotenv/config";
import { randomUUID } from 'crypto';
import { ChatOpenAI } from "@langchain/openai";
import { HumanMessage } from "@langchain/core/messages";
import { PromptTemplate, ChatPromptTemplate } from "@langchain/core/prompts";
import { createOpenAIToolsAgent, AgentExecutor } from "langchain/agents";
import { Calculator } from "@langchain/community/tools/calculator";
import { DynamicTool } from "@langchain/core/tools";
import { RunnableSequence } from "@langchain/core/runnables";
import { pull } from "langchain/hub";

// Enable OpenTelemetry internal diagnostics
diag.setLogger(new DiagConsoleLogger(), DiagLogLevel.DEBUG);

const main = async () => {
  // FI Telemetry Credentials
  const fiApiKey = process.env.FI_API_KEY;
  const fiSecretKey = process.env.FI_SECRET_KEY;
  if (!fiApiKey || !fiSecretKey) {
    console.error(
      "FI_API_KEY and FI_SECRET_KEY environment variables must be set for telemetry.",
    );
    process.exit(1);
  }

  // Log environment variables for debugging
  console.log("Environment variables:");
  console.log("FI_BASE_URL:", process.env.FI_BASE_URL);
  console.log("FI_COLLECTOR_ENDPOINT:", process.env.FI_COLLECTOR_ENDPOINT);
  console.log("FI_API_KEY:", fiApiKey);
  console.log("FI_SECRET_KEY:", fiSecretKey);

  // 1. Register FI Core TracerProvider (sets up exporter)
  const tracerProvider = register({
    projectName: "langchain-chained-agent",
    projectType: ProjectType.OBSERVE,
    sessionName: "langchain"
  });

  // 2. Register LangChain Instrumentation
  registerInstrumentations({
    tracerProvider: tracerProvider,
    instrumentations: [new LangChainInstrumentation()],
  });

  // 3. Create a simple chain
  const chatModel = new ChatOpenAI({
    openAIApiKey: process.env.OPENAI_API_KEY,
    temperature: 0,
  });

  // Create a simple summarization chain
  const summarizationPrompt = PromptTemplate.fromTemplate(
    "Summarize the following text in 1-2 sentences:\n\n{text}"
  );

  const summarizationChain = RunnableSequence.from([
    summarizationPrompt,
    chatModel,
  ]);

  // Create a tool from the summarization chain
  const summarizationTool = new DynamicTool({
    name: "text-summarizer",
    description: "Summarizes longer text into 1-2 sentences",
    func: async (text) => {
      const result = await summarizationChain.invoke({ text });
      return result.content;
    },
  });

  // Create an agent with tools
  const tools = [
    new Calculator(),
    summarizationTool,
  ];

  // Pull the prompt
  const prompt = await pull<ChatPromptTemplate>("hwchase17/openai-tools-agent");

  const agent = await createOpenAIToolsAgent({
    llm: chatModel,
    tools: tools as any[],
    prompt,
  });

  const agentExecutor = new AgentExecutor({
    agent,
    tools: tools as any[],
  });

  // 4. Run the agent
  console.log("Running agent...");
  const result = await agentExecutor.invoke({
    input: "First, calculate 234 * 78. Then, summarize the following text: 'The Industrial Revolution was a period of major industrialization and innovation that took place during the late 1700s and early 1800s. The Industrial Revolution began in Great Britain and quickly spread throughout the world. The American Industrial Revolution, commonly referred to as the Second Industrial Revolution, started sometime between 1820 and 1870. This period saw the mechanization of agriculture and textile manufacturing and a revolution in power, including steamships and railroads, that affected social, cultural, and economic conditions.'"
  });

  console.log("Agent result:");
  console.log(result.output);

  // 5. Shutdown the provider to ensure spans are flushed
  try {
    await tracerProvider.shutdown();
    console.log("Tracer provider shut down successfully.");
  } catch (error) {
    console.error("Error shutting down tracer provider:", error);
  }

  return result;
};

main().catch((error) => {
  console.error("Unhandled error in main function:", error);
  process.exit(1);
});
