import { register, ProjectType } from "@traceai/fi-core";
import { registerInstrumentations } from "@opentelemetry/instrumentation";
import { diag, DiagConsoleLogger, DiagLogLevel } from "@opentelemetry/api";
import { LangChainInstrumentation } from "../src";
import "./instrumentation";
import "dotenv/config";
import { ChatOpenAI } from "@langchain/openai";
import { HumanMessage } from "@langchain/core/messages";

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

  // 1. Register FI Core TracerProvider (sets up exporter)
  const tracerProvider = register({
    projectName: "langchain-chat-example",
    projectType: ProjectType.OBSERVE,
    sessionName: "langchain-chat-session-" + Date.now(),
    // Add configuration for the TraceAI endpoint
    endpoint: process.env.FI_BASE_URL || "https://api.trace.ai", // Use production endpoint by default
    headers: {
      "x-api-key": fiApiKey,
      "x-secret-key": fiSecretKey,
    },
    verbose: true, // Enable verbose mode to see endpoint construction
  });

  // 2. Register LangChain Instrumentation
  registerInstrumentations({
    tracerProvider: tracerProvider,
    instrumentations: [new LangChainInstrumentation()],
  });

  // 3. Initialize Chat Model
  const chatModel = new ChatOpenAI({
    openAIApiKey: process.env.OPENAI_API_KEY,
    metadata: {
      session_id: "test-session-123",
    },
  });

  const request = new HumanMessage("Hello! How are you?");

  const response = await chatModel.invoke([request]);

  // get a new response, including a greeting in the message history
  const finalResponse = await chatModel.invoke([
    request,
    response,
    new HumanMessage("That is great to hear!"),
  ]);

  // eslint-disable-next-line no-console
  console.log(finalResponse.content);

  // 4. Shutdown the provider to ensure spans are flushed
  try {
    await tracerProvider.shutdown();
    console.log("Tracer provider shut down successfully.");
  } catch (error) {
    console.error("Error shutting down tracer provider:", error);
  }

  return finalResponse;
};

main().catch((error) => {
  console.error("Unhandled error in main function:", error);
  process.exit(1);
});
