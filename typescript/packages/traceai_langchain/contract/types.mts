import { LangChainInstrumentation } from "@traceai/langchain";
import * as CallbackManagerModule from "@langchain/core/callbacks/manager";
import * as ChatModelModule from "@langchain/core/language_models/chat_models";
import { awaitAllCallbacks } from "@langchain/core/callbacks/promises";
import { RunnableLambda } from "@langchain/core/runnables";
import { BasicTracerProvider } from "@opentelemetry/sdk-trace-base";

// Type-check the built public declarations against each consumer's core version,
// rather than only the version used to build this workspace.
async function example(): Promise<number> {
  const provider = new BasicTracerProvider();
  const instrumentation = new LangChainInstrumentation({ tracerProvider: provider });
  instrumentation.manuallyInstrument(CallbackManagerModule);
  instrumentation.manuallyInstrument(CallbackManagerModule, ChatModelModule);
  const result: number = await RunnableLambda.from((input: number) => input + 1).invoke(3);
  await awaitAllCallbacks();
  await provider.shutdown();
  return result;
}
void example;
