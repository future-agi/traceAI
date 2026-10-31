import { LangChainInstrumentation } from "@traceai/langchain";
import * as CallbackManagerModule from "@langchain/core/callbacks/manager";
import { awaitAllCallbacks } from "@langchain/core/callbacks/promises";
import { RunnableLambda } from "@langchain/core/runnables";
import { BasicTracerProvider } from "@opentelemetry/sdk-trace-base";

// Type-check the built public declarations against each consumer's core version,
// rather than only the version used to build this workspace.
async function example(): Promise<number> {
  const provider = new BasicTracerProvider();
  const instrumentation = new LangChainInstrumentation({ tracerProvider: provider });
  instrumentation.manuallyInstrument(CallbackManagerModule);
  const result: number = await RunnableLambda.from((input: number) => input + 1).invoke(3);
  await awaitAllCallbacks();
  await provider.shutdown();
  return result;
}
void example;
