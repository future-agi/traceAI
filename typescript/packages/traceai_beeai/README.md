# @traceai/beeai

Trace BeeAI agents, tools, chat models and workflows in Future AGI while keeping BeeAI's lazy `Run`, observers and middleware intact.

## Install

```sh
npm install --legacy-peer-deps @traceai/beeai @traceai/fi-core beeai-framework@^0.1.0 zod@^3.25.76 @opentelemetry/api@^1.9.0 @opentelemetry/context-async-hooks@^2.0.1
```

Use `beeai-framework` 0.1.x. The `--legacy-peer-deps` flag avoids conflicting optional LangChain provider peers in BeeAI's dependency tree. It also disables npm's peer-dependency checks, so install your model adapter's required packages explicitly and use its own credentials. This integration exports traces, not metrics or logs.

## Configure tracing

Set `FI_API_KEY` and `FI_SECRET_KEY` in your environment. Save this as `tracing.mjs` and load it before running your agents.

```javascript
import { register, ProjectType } from "@traceai/fi-core";
import { BeeAIInstrumentation } from "@traceai/beeai";
import { RunContext } from "beeai-framework/context";
import { context } from "@opentelemetry/api";
import { AsyncLocalStorageContextManager } from "@opentelemetry/context-async-hooks";

// For a standalone application. Reuse your existing OTel context manager if present.
context.setGlobalContextManager(new AsyncLocalStorageContextManager().enable());

export const tracerProvider = register({
  projectName: "beeai-app",
  projectType: ProjectType.OBSERVE,
});

export const instrumentation = new BeeAIInstrumentation({
  traceConfig: {
    hideInputs: true,
    hideOutputs: true,
    hideErrorDetails: true,
  },
});
instrumentation.setTracerProvider(tracerProvider);
instrumentation.manuallyInstrument({ RunContext });
```

Pass `RunContext` from the same CommonJS or ESM framework copy your application uses. This explicit setup works with either module system and does not require an ESM loader hook.

If your application already owns an OpenTelemetry tracer provider, pass that provider to `setTracerProvider()` instead of creating a second one. Use only one BeeAI tracing integration: do not also attach BeeAI's telemetry middleware or another BeeAI instrumentor.

## Run a workflow

This workflow needs no model credentials. Save it as `app.mjs` next to `tracing.mjs`.

```javascript
import { tracerProvider } from "./tracing.mjs";
import { Workflow } from "beeai-framework/workflows/workflow";
import { z } from "zod";

const workflow = new Workflow({
  name: "normalize-question",
  schema: z.object({ question: z.string() }),
}).addStep("trim", async (state) => {
  state.question = state.question.trim();
  return Workflow.END;
});

try {
  const result = await workflow.run({ question: " How do I trace my agent? " });
  console.log(result.state.question);
} finally {
  await tracerProvider.shutdown();
}
```

```sh
node app.mjs
```

Open the `beeai-app` project in Future AGI to inspect the workflow trace. The same setup captures your existing agents and their nested tools, models and workflows. Keep shutdown at process exit for a long-running service, not after each request.

## What is captured

- One span per native BeeAI run, with parent-child relationships for nested runs.
- Agent, tool, LLM, embedding and workflow span kinds. Workflows use the `CHAIN` kind.
- Configured tool names, chat model and provider identifiers, and token counts when BeeAI supplies usage.
- Structured chat results from `createStructure()`, including usage when returned by BeeAI.
- One model span for a streamed chat completion; token observers continue to receive events normally.
- Error status and cancellation without changing the application's return values or exceptions.

Constructing a `Run` without awaiting it does not create a span or execute the operation. Timing follows BeeAI's native `run.start` through `run.finish` lifecycle. A custom run that returns an unconsumed async iterator ends when BeeAI finishes that run; consuming the iterator later is not a second traced operation.

## Privacy

The setup above hides input, output and error text. Set the corresponding `traceConfig` options to `false` only when you intend to export that content. Environment configuration in `@traceai/fi-core` also applies when options are not set explicitly.

Message/text-hiding options also hide generic payloads in the corresponding direction, including structured model results. Agent outputs capture the final `result`, not the agent's memory or intermediate iterations.

Embedding model outputs are omitted. Set `hideEmbeddingVectors: true` (or `FI_HIDE_EMBEDDING_VECTORS=true`) to redact fields named `embedding`, `embeddings`, `vector` or `vectors` at any depth in captured input payloads. To prevent vectors copied into other output fields from leaking, `@traceai/fi-core` masks the **entire** generic `output.value` on agent, tool and workflow spans in this mode, including public answers and labels. Application return values are unchanged. Custom inputs may also copy vectors into other fields or strings; set `hideInputs: true` to omit all input content when you cannot control the input shape. Configure other instrumentors separately.

## Existing applications

`BeeAIInstrumentation`, `setTracerProvider()`, `enable()`, `disable()` and `manuallyInstrument()` remain available. CommonJS automatic registration must happen before loading `beeai-framework` or its subpaths.

Existing `manuallyInstrument(beeModule)` calls with the package root continue to trace agents and their nested BeeAI runs. Pass `{ RunContext }` as shown above to also capture standalone tools, models and workflows and propagate the active span into other OpenTelemetry-instrumented work inside a run.

## License

Apache-2.0.
