# @traceai/openai-agents

OpenTelemetry instrumentation for the [OpenAI Agents SDK for TypeScript](https://github.com/openai/openai-agents-js) (`@openai/agents`). It registers a trace processor with the SDK and turns every agent run into OpenTelemetry spans that you can send to [Future AGI](https://futureagi.com) or any OTLP backend.

## Supported versions

| `@traceai/openai-agents` | `@openai/agents` | Tested against |
| --- | --- | --- |
| 0.2.0 | `>=0.4.0 <0.22.0` | 0.4.15, 0.11.8, 0.16.1, 0.21.0 |

The package works with both CommonJS and ES modules.

## Installation

```bash
npm install @traceai/openai-agents @traceai/fi-core @openai/agents
```

## Quick start

```typescript
import { register, ProjectType } from "@traceai/fi-core";
import { OpenAIAgentsInstrumentation } from "@traceai/openai-agents";
import * as agents from "@openai/agents";

// Reads FI_API_KEY and FI_SECRET_KEY from the environment.
const tracerProvider = register({
  projectName: "my-agents-app",
  projectType: ProjectType.OBSERVE,
});

const instrumentation = new OpenAIAgentsInstrumentation();
instrumentation.setTracerProvider(tracerProvider);
instrumentation.manuallyInstrument(agents);

const agent = new agents.Agent({
  name: "Assistant",
  instructions: "Answer concisely.",
  model: "gpt-4.1-mini",
});
const result = await agents.run(agent, "What is the capital of France?");
console.log(result.finalOutput);

// Export the remaining spans before the process exits.
await tracerProvider.forceFlush();
```

Call `setTracerProvider` before `manuallyInstrument`. `manuallyInstrument` works for both module systems. In CommonJS you can instead pass the instrumentation to `registerInstrumentations` before the first `require("@openai/agents")`.

## The SDK's default exporter

By default the instrumentation calls `setTraceProcessors`, which **replaces** the SDK's processors, so traces are no longer sent to the OpenAI Traces dashboard. To keep that export and add Future AGI next to it, pass `exclusiveProcessor: false`. The instrumentation then calls `addTraceProcessor`:

```typescript
const instrumentation = new OpenAIAgentsInstrumentation({ exclusiveProcessor: false });
```

This matches `exclusive_processor` in the Python package `traceAI-openai-agents`.

## What is captured

One trace per run, rooted at a span named after the workflow. Child spans follow the SDK's own span tree:

| SDK span | Span kind | Attributes |
| --- | --- | --- |
| `agent` | AGENT | `graph.node.id`; after a handoff, `graph.node.parent_id` is the agent that handed off |
| `response` (Responses API) | LLM | input, output text, model, token counts |
| `generation` (Chat Completions) | LLM | input messages, output text, model, invocation parameters, token counts |
| `function` | TOOL | tool name, arguments, result |
| `handoff` | TOOL | named `handoff to <agent>` |
| `task`, `turn`, `guardrail`, `custom`, `mcp_tools` | CHAIN | raw span data |

`traceConfig` (for example `{ hideInputs: true, hideOutputs: true }`) and the `FI_HIDE_INPUTS` / `FI_HIDE_OUTPUTS` environment variables replace `input.value` and `output.value` with `__REDACTED__`. They do not yet cover the raw request and response bodies (`raw.input`, `raw.output`): `@traceai/fi-core` 1.0.0 exports those even when hiding is on. The fix is in the next `@traceai/fi-core` release (1.1.0, future-agi/traceAI#271).

## License

Apache-2.0
