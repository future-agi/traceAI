import { register, ProjectType } from "@traceai/fi-core";
import { registerInstrumentations } from "@opentelemetry/instrumentation";
import { LangChainInstrumentation } from "../src";
import "./instrumentation";
import "dotenv/config";
import "punycode/";
import { ChatOpenAI } from "@langchain/openai";
import { ChatPromptTemplate } from "@langchain/core/prompts";
import { AgentExecutor, createToolCallingAgent } from "langchain/agents";
import { DynamicTool } from "@langchain/core/tools";
import { HumanMessage } from "@langchain/core/messages";

// Define the exchange rate tool
const getExchangeRate = new DynamicTool({
  name: "get_exchange_rate",
  description: "Retrieves the exchange rate between two currencies on a specified date.",
  func: async (input: string) => {
    try {
      // Parse input string into parameters
      const [currencyFrom = "USD", currencyTo = "EUR", currencyDate = "latest"] = input.split(",").map(s => s.trim());
      
      const url = `https://api.frankfurter.app/${currencyDate}`;
      const params = new URLSearchParams({
        from: currencyFrom,
        to: currencyTo
      });

      const response = await fetch(`${url}?${params}`);
      if (!response.ok) {
        throw new Error(`HTTP error! status: ${response.status}`);
      }
      const data = await response.json();
      return JSON.stringify(data);
    } catch (error) {
      return `Error fetching exchange rate: ${error}`;
    }
  }
});

// Create and configure the agent
const createAgent = async (tools: DynamicTool[]): Promise<AgentExecutor> => {
  const llm = new ChatOpenAI({
    openAIApiKey: process.env.OPENAI_API_KEY,
  });

  const prompt = ChatPromptTemplate.fromMessages([
    ["human", "{input}"],
    ["placeholder", "{agent_scratchpad}"]
  ]);

  const agent = await createToolCallingAgent({
    llm,
    tools,
    prompt
  });

  return AgentExecutor.fromAgentAndTools({
    agent,
    tools,
    verbose: false
  });
};

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

  // // Log environment variables for debugging
  // console.log("Environment variables:");
  // console.log("FI_BASE_URL:", process.env.FI_BASE_URL);
  // console.log("FI_COLLECTOR_ENDPOINT:", process.env.FI_COLLECTOR_ENDPOINT);
  // console.log("FI_API_KEY exists:", !!fiApiKey);
  // console.log("FI_SECRET_KEY exists:", !!fiSecretKey);

  // 1. Register FI Core TracerProvider (sets up exporter)
  const tracerProvider = register({
    projectName: "langchain-test03",
    projectType: ProjectType.OBSERVE,
  });

  // 2. Register LangChain Instrumentation
  registerInstrumentations({
    tracerProvider: tracerProvider,
    instrumentations: [new LangChainInstrumentation()],
  });

  // 3. Create agent with exchange rate tool
  const tools = [getExchangeRate];
  const agentExecutor = await createAgent(tools);

  // 4. Execute the agent
  try {
    const result = await agentExecutor.invoke({
      input: "What is the exchange rate from US dollars to Swedish currency today?"
    });
    // console.log("Agent result:", result);
    return result;
  } catch (error) {
    console.error("Error executing agent:", error);
    throw error;
  } finally {
    // 5. Shutdown the provider to ensure spans are flushed
    try {
      await tracerProvider.shutdown();
      // console.log("Tracer provider shut down successfully.");
    } catch (error) {
      console.error("Error shutting down tracer provider:", error);
    }
  }
};

// Run the main function
main().catch((error) => {
  console.error("Unhandled error in main function:", error);
  process.exit(1);
});
