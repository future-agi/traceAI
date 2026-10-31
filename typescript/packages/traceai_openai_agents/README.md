# @traceai/openai-agents

OpenTelemetry instrumentation for the [OpenAI Agents SDK for TypeScript](https://github.com/openai/openai-agents-js) (`@openai/agents`). It registers a trace processor with the SDK and turns every agent run into OpenTelemetry spans that you can send to [Future AGI](https://futureagi.com) or any OTLP backend.

## Supported versions

Supported `@openai/agents` versions: `>=0.4.0 <0.22.0`.

The package works with both CommonJS and ES modules.

## Installation

```bash
npm install @traceai/openai-agents @traceai/fi-core '@openai/agents@>=0.4.0 <0.22.0'
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

// Stop receiving SDK callbacks, then export remaining spans before exiting.
instrumentation.disable();
await tracerProvider.shutdown();
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
| `guardrail` | GUARDRAIL | guardrail name and `guardrail.triggered` |
| `mcp_tools` | TOOL | `tool.name = list_tools`, `mcp.server.name`, tool names as JSON output |
| `custom` | CHAIN | user data as JSON output |
| `task`, `turn` and other SDK spans | CHAIN | SDK span name and parentage |

Child spans use the SDK's `startedAt` and `endedAt` timestamps. Workflow roots use callback times because SDK traces do not expose timestamps. Span data is not copied wholesale into input attributes; input and output fields are captured separately so output hiding also covers MCP results.

## Sessions and metadata

Set `groupId` in the SDK's `Runner` configuration to associate workflows with a session (`session.id`). Trace metadata is excluded by default. To export selected top-level keys from `traceMetadata`, approve them in the instrumentation configuration:

```typescript
const instrumentation = new OpenAIAgentsInstrumentation({
  traceMetadataKeys: ["release", "region"],
});
instrumentation.setTracerProvider(tracerProvider);
instrumentation.manuallyInstrument(agents);

const runner = new agents.Runner({
  groupId: "conversation-42",
  traceMetadata: { release: "canary", region: "eu" },
});
await runner.run(agent, "What is the capital of France?");
```

Use this configuration instead of the earlier instrumentation instance. Approved values, including nested fields, are stored in the root span's JSON `metadata`. Input/output hiding does **not** remove session identifiers, approved metadata, agent/tool names or MCP server names. Only approve metadata whose entire value is safe to export; use opaque session identifiers and keep secrets out of names.

## Privacy

Use `traceConfig: { hideInputs: true, hideOutputs: true }` or the `FI_HIDE_INPUTS` / `FI_HIDE_OUTPUTS` environment variables to redact content attributes. With `@traceai/fi-core` 1.1.0 or later, this covers both standard values and raw request/response attributes. Keep `traceMetadataKeys` empty when you do not need metadata.

## Shutdown and flushing

Wait for active runs to finish, call `instrumentation.disable()`, then `await tracerProvider.shutdown()`. Disabling closes outstanding spans with an error status and makes callbacks retained by the SDK inert; it does not remove other processors or restore the SDK's default exporter. To resume tracing, call `instrumentation.enable()` and `instrumentation.manuallyInstrument(agents)` again.

For a long-running service, call `await tracerProvider.forceFlush()` when needed. The processor's own `forceFlush()` has no export queue to drain and does not flush the OTel provider. If you register `instrumentation.getProcessor()` yourself, call its `shutdown()` before shutting down your provider.

## License

Apache-2.0
