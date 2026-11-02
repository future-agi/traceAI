import { register, ProjectType } from "@traceai/fi-core";
import { BeeAIInstrumentation } from "@traceai/beeai";
import { context } from "@opentelemetry/api";
import { AsyncLocalStorageContextManager } from "@opentelemetry/context-async-hooks";
import { RunContext } from "beeai-framework/context";
import { Workflow } from "beeai-framework/workflows/workflow";
import { z } from "zod";

// Reuse your application's context manager if it already has one.
context.setGlobalContextManager(new AsyncLocalStorageContextManager().enable());
const provider = register({ projectName: "beeai-example", projectType: ProjectType.OBSERVE });
const instrumentation = new BeeAIInstrumentation({
  traceConfig: { hideInputs: true, hideOutputs: true, hideErrorDetails: true },
});
instrumentation.setTracerProvider(provider);
instrumentation.manuallyInstrument({ RunContext });

async function main() {
  const workflow = new Workflow({
    name: "normalize-question",
    schema: z.object({ question: z.string() }),
  }).addStep("trim", async state => {
    state.question = state.question.trim();
    return Workflow.END;
  });
  try {
    const result = await workflow.run({ question: " How do I trace my agent? " });
    console.log(result.state.question);
  } finally {
    instrumentation.disable();
    await provider.shutdown();
  }
}

main().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
