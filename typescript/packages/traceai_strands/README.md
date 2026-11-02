# @traceai/strands

Send native Strands agent, model, tool, graph and swarm traces to Future AGI without wrapping agent methods or creating duplicate spans.

## Install

Use **Node.js 22 or later** and `@strands-agents/sdk` **1.23.x**. For the OpenAI example below, use your own OpenAI API key as well as your Future AGI API and secret keys.

```sh
npm install @traceai/strands @strands-agents/sdk@^1.23.0 openai@^6.45.0 \
  @opentelemetry/api@^1.9.0 @opentelemetry/sdk-trace-node@^2.8.0 \
  @opentelemetry/sdk-trace-base@^2.8.0 @opentelemetry/resources@^2.8.0 \
  @opentelemetry/exporter-trace-otlp-http@^0.219.0
```

Set `FI_API_KEY`, `FI_SECRET_KEY` and `OPENAI_API_KEY` in your environment. Do not put keys in source code. Optionally set `OPENAI_MODEL` to a model available to your account.

## Configure tracing

Save this as `tracing.mjs`. `NodeTracerProvider.register()` installs asynchronous context propagation so model and tool spans remain under their agent across `await` boundaries.

```javascript
import { NodeTracerProvider } from "@opentelemetry/sdk-trace-node";
import { BatchSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { resourceFromAttributes } from "@opentelemetry/resources";
import { OTLPTraceExporter } from "@opentelemetry/exporter-trace-otlp-http";
import { StrandsSpanExporter, StrandsSpanProcessor } from "@traceai/strands/native";

if (!process.env.FI_API_KEY || !process.env.FI_SECRET_KEY) {
  throw new Error("Set FI_API_KEY and FI_SECRET_KEY before starting the app");
}

export const strandsProcessor = new StrandsSpanProcessor(
  new BatchSpanProcessor(new StrandsSpanExporter(
    new OTLPTraceExporter({
      url: process.env.OTEL_EXPORTER_OTLP_TRACES_ENDPOINT
        ?? "https://api.futureagi.com/tracer/v1/traces",
      headers: {
        "x-api-key": process.env.FI_API_KEY,
        "x-secret-key": process.env.FI_SECRET_KEY,
      },
    }),
    { modelProvider: "openai" },
  )),
);
export const provider = new NodeTracerProvider({
  resource: resourceFromAttributes({
    "service.name": "strands-app",
    project_name: "strands-app",
    project_type: "observe",
  }),
  spanProcessors: [strandsProcessor],
});
provider.register();
```

Register **one** provider before creating agents. If your application already owns an OpenTelemetry provider, add the wrapped exporter to that provider's span processors at construction instead of registering another one. Do not also enable Strands' OTLP exporter or register the legacy `StrandsInstrumentation` wrapper for native SDK versions. No meter provider is required.

`modelProvider` is optional and applies to model spans. Set it only when all agents exported through this adapter use that provider; omit it for mixed-provider applications. Strands reports its service name in the native provider field, so the adapter retains that as `strands.service.name` instead of treating it as an inference provider.

## Run an agent

Save this as `app.mjs` and run `node app.mjs`.

```javascript
import { provider } from "./tracing.mjs";
import { Agent } from "@strands-agents/sdk";
import { OpenAIModel } from "@strands-agents/sdk/models/openai";

try {
  const agent = new Agent({
    name: "assistant",
    model: new OpenAIModel({
      api: "chat",
      modelId: process.env.OPENAI_MODEL ?? "gpt-4o-mini",
    }),
    printer: false,
  });
  const result = await agent.invoke("Explain what an API is in one sentence.");
  console.log(result.lastMessage);
} finally {
  await provider.shutdown();
}
```

In Future AGI, open the **strands-app** Observe project to inspect the agent and its child model spans. Tools and multi-agent orchestrators use the same provider. Token usage is exported only on model spans, not repeated on aggregate agent, node, graph or swarm spans.

For streaming, consume `agent.stream()` normally. The SDK owns span completion. The adapter preserves completion reasons and marks cancellations, failed tool results and output-limit failures as errors. If an iterator returns early or completion telemetry is missing, it reports an unknown outcome (`UNSET`), not success. In a server, call `provider.shutdown()` at process shutdown, not after every request.

## Trace graph and swarm outcomes

For each graph or swarm call, use `strandsProcessor.invoke(() => graph.invoke(input))` or `strandsProcessor.stream(() => graph.stream(input))`. Use the same processor installed on your provider. These methods call the public SDK API and preserve the original result, events and native span IDs; they do not patch methods or create extra spans.

Save this as `graph.mjs` and run `node graph.mjs`:

```javascript
import { provider, strandsProcessor } from "./tracing.mjs";
import { Agent, Graph } from "@strands-agents/sdk";
import { OpenAIModel } from "@strands-agents/sdk/models/openai";

try {
  const graph = new Graph({
    nodes: [new Agent({
      id: "explainer",
      printer: false,
      model: new OpenAIModel({
        api: "chat",
        modelId: process.env.OPENAI_MODEL ?? "gpt-4o-mini",
      }),
    })],
    edges: [],
    maxSteps: 2,
  });
  for await (const event of strandsProcessor.stream(
    () => graph.stream("Explain what an API is in one sentence."),
  )) {
    if (event.type === "multiAgentResultEvent") console.log(event.result.status);
  }
} finally {
  await provider.shutdown();
}
```

Strands ends its orchestration span before returning the final result. The processor holds that existing root until the call finishes: `COMPLETED` becomes success, while `FAILED`, `CANCELLED` and `INTERRUPTED` become errors. Returning a stream early without a result remains unknown, even when some nodes completed. The adapter reads only the result status, not its content or error message.

Without these call methods, the exporter cannot recover a graph or swarm's final result and reports unknown unless native error evidence is present. Nested orchestrators also need their own tracked calls to report their final results; the outer result does not establish the nested outcome. Flush or shut down after calls finish. An explicit mid-call flush, shutdown, or overflow of the 4,096 pending-root limit forwards ended roots once as unknown rather than waiting indefinitely.

## Control captured data

By default, the native adapter exports only an allowlist of operational attributes. It omits prompts, responses, system instructions, tool arguments/results, custom span attributes, event payloads, link attributes and error details. Status codes, model identifiers, agent/tool names and IDs, timings and model usage remain available.

To include conversation and tool content, set `captureContent: true` in `StrandsSpanExporter` options. This also includes custom span attributes. Error messages and exception events require the separate `captureErrorDetails: true` option. Content can contain sensitive data; enable either option only when appropriate for your application.

Deployment privacy settings still apply when capture is enabled. `FI_HIDE_INPUTS`, `FI_HIDE_INPUT_MESSAGES`, `FI_HIDE_INPUT_TEXT` and `FI_HIDE_INPUT_IMAGES` suppress native input payloads; the corresponding output settings suppress output payloads. Because native messages can contain nested tool arguments and alternate copies, these flags suppress the entire affected direction and unclassified custom/link payloads. `FI_HIDE_ERROR_DETAILS=true` always overrides error capture; when unset, content-hiding flags derive it from the shared FI trace configuration. `FI_PII_REDACTION` and `FI_HIDE_EMBEDDING_VECTORS` also apply.

Without error capture, failed tool results are redacted before being mapped into output or conversation history. The latest OpenTelemetry message format omits result status in history, so those ambiguous tool responses are also redacted. Successful tool spans retain their own result when content capture is enabled.

These options apply only to this exporter's Strands scope. They do not sanitize resources, span names, other instrumentation, application logs, or another exporter attached to your provider. Keep sensitive values out of agent/tool names, IDs and resource metadata. Strands' printer is separate from trace export; the example disables it.

The scope defaults to `OTEL_SERVICE_NAME` or `strands-agents`. If that environment variable changes after constructing the exporter, set `instrumentationScope` explicitly on both `StrandsSpanExporter` and `StrandsSpanProcessor` to the value used when creating agents.

## Existing SDK 0.1 applications

The `StrandsInstrumentation`, `StrandsInstrumentationConfig`, `isPatched` and `VERSION` exports remain available for SDK 0.1.x applications. That legacy path records outer invocations, not native model/tool/orchestration spans. When upgrading to SDK 1.23.x, replace the wrapper with `StrandsSpanExporter` and the provider setup above. The manual instrumentation entry point leaves native SDK prototypes untouched.

## Troubleshooting

- **No spans:** create the provider before the agents, check both Future AGI headers and the full `/tracer/v1/traces` endpoint, and flush or shut down the provider before a short-lived process exits.
- **Missing prompt/response:** content capture is off by default. Enable `captureContent` only if you want that data exported.
- **Duplicate spans or usage:** keep one wrapped exporter for Future AGI. Remove any raw Strands exporter or manual application-span fallback that describes the same invocation.
- **Missing parents:** use `NodeTracerProvider.register()` or your existing async context manager. Merely setting a global tracer provider does not install context propagation.

## License

Apache-2.0.
