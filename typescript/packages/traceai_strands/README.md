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
import { StrandsSpanExporter } from "@traceai/strands/native";

if (!process.env.FI_API_KEY || !process.env.FI_SECRET_KEY) {
  throw new Error("Set FI_API_KEY and FI_SECRET_KEY before starting the app");
}

export const provider = new NodeTracerProvider({
  resource: resourceFromAttributes({
    "service.name": "strands-app",
    project_name: "strands-app",
    project_type: "observe",
  }),
  spanProcessors: [new BatchSpanProcessor(new StrandsSpanExporter(
    new OTLPTraceExporter({
      url: process.env.OTEL_EXPORTER_OTLP_TRACES_ENDPOINT
        ?? "https://api.futureagi.com/tracer/v1/traces",
      headers: {
        "x-api-key": process.env.FI_API_KEY,
        "x-secret-key": process.env.FI_SECRET_KEY,
      },
    }),
    { modelProvider: "openai" },
  ))],
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

For streaming, consume `agent.stream()` normally. The SDK owns span completion, including early iterator return and cancellation. In a server, call `provider.shutdown()` at process shutdown, not after every request.

## Control captured data

By default, the native adapter exports only an allowlist of operational attributes. It omits prompts, responses, system instructions, tool arguments/results, custom span attributes, event payloads, link attributes and error details. Status codes, model identifiers, agent/tool names and IDs, timings and model usage remain available.

To include conversation and tool content, set `captureContent: true` in `StrandsSpanExporter` options. This also includes custom span attributes. Error messages and exception events require the separate `captureErrorDetails: true` option. Content can contain sensitive data; enable either option only when appropriate for your application.

These options apply only to this exporter's Strands scope. They do not sanitize resources, span names, other instrumentation, application logs, or another exporter attached to your provider. Keep sensitive values out of agent/tool names, IDs and resource metadata. Strands' printer is separate from trace export; the example disables it.

The scope defaults to `OTEL_SERVICE_NAME` or `strands-agents`. If that environment variable changes after constructing the exporter, set `instrumentationScope` explicitly to the value used when creating agents.

## Existing SDK 0.1 applications

The `StrandsInstrumentation`, `StrandsInstrumentationConfig`, `isPatched` and `VERSION` exports remain available for SDK 0.1.x applications. That legacy path records outer invocations, not native model/tool/orchestration spans. When upgrading to SDK 1.23.x, replace the wrapper with `StrandsSpanExporter` and the provider setup above. The manual instrumentation entry point leaves native SDK prototypes untouched.

## Troubleshooting

- **No spans:** create the provider before the agents, check both Future AGI headers and the full `/tracer/v1/traces` endpoint, and flush or shut down the provider before a short-lived process exits.
- **Missing prompt/response:** content capture is off by default. Enable `captureContent` only if you want that data exported.
- **Duplicate spans or usage:** keep one wrapped exporter for Future AGI. Remove any raw Strands exporter or manual application-span fallback that describes the same invocation.
- **Missing parents:** use `NodeTracerProvider.register()` or your existing async context manager. Merely setting a global tracer provider does not install context propagation.

## License

Apache-2.0.
