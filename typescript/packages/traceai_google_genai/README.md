# @traceai/google-genai — legacy SDK compatibility

Trace existing Node.js applications that use **`@google/generative-ai`** with Future AGI.

> This package does **not** instrument the modern **`@google/genai`** SDK, despite its name. Google ended support for `@google/generative-ai` on November 30, 2025. Keep this adapter for existing legacy applications; use Google's [SDK migration guide](https://ai.google.dev/gemini-api/docs/migrate) when moving to the modern SDK. Replacing the Google SDK alone does not preserve tracing through this adapter.

The package name and `GoogleGenAIInstrumentation` entry point remain unchanged for compatibility. No modern-SDK tracing is implied by them.

## Supported operations

| Legacy SDK method | Tracing |
| --- | --- |
| `GenerativeModel.generateContent` | Non-streaming generation, text input/output, model, returned token usage and errors |
| `GenerativeModel.embedContent` | Embedding input and returned vector |
| `GenerativeModel.batchEmbedContents` | Batch embedding inputs and returned vectors |
| `generateContentStream`, `ChatSession.sendMessage`, `ChatSession.sendMessageStream`, `countTokens` | Not instrumented |
| Modern `@google/genai` APIs | Not supported |

Model availability is controlled by Google, not this adapter. Choose a model available to your account rather than copying a retired model ID.

## Install

For an existing legacy application using Node.js 20 or later:

```bash
npm install @traceai/google-genai @traceai/fi-core @google/generative-ai@0.24.1
```

## Trace a generation

Set `FI_API_KEY` and `FI_SECRET_KEY` from your Future AGI project, `GOOGLE_API_KEY` from Google, and `GOOGLE_MODEL` to a model available to your account. Supply credentials through your environment or secret manager; do not commit them.

Save this as `trace.cjs`:

```javascript
const { register, ProjectType } = require("@traceai/fi-core");
const { GoogleGenAIInstrumentation } = require("@traceai/google-genai");
const google = require("@google/generative-ai");

async function main() {
  const provider = register({
    projectName: "gemini-legacy-app",
    projectType: ProjectType.OBSERVE,
  });
  const instrumentation = new GoogleGenAIInstrumentation();
  instrumentation.setTracerProvider(provider);
  instrumentation.manuallyInstrument(google);

  try {
    const client = new google.GoogleGenerativeAI(process.env.GOOGLE_API_KEY);
    const model = client.getGenerativeModel({ model: process.env.GOOGLE_MODEL });
    const result = await model.generateContent("Explain quantum computing in one sentence.");
    console.log(result.response.text());
  } finally {
    await provider.shutdown();
  }
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
```

Run `node trace.cjs`, then open the **gemini-legacy-app** project in Future AGI and look for the **Google GenAI generateContent** span. Shutdown flushes queued spans before the process exits. Manual instrumentation also works when the SDK has already been imported; use one instrumentation instance per process.

### Use an existing OpenTelemetry collector

If your application already sends OTLP to a collector, use that tracer provider instead of creating a second one. For a standalone OpenTelemetry 2.x setup, install:

```bash
npm install @opentelemetry/sdk-trace-node@^2 @opentelemetry/sdk-trace-base@^2 @opentelemetry/exporter-trace-otlp-http@^0.202
```

Replace the `register(...)` provider declaration above with:

```javascript
const { NodeTracerProvider } = require("@opentelemetry/sdk-trace-node");
const { SimpleSpanProcessor } = require("@opentelemetry/sdk-trace-base");
const { OTLPTraceExporter } = require("@opentelemetry/exporter-trace-otlp-http");

const provider = new NodeTracerProvider({
  spanProcessors: [new SimpleSpanProcessor(new OTLPTraceExporter())],
});
provider.register();
```

Set `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` to your collector's full HTTP traces URL, for example `http://localhost:4318/v1/traces`. Keep `setTracerProvider`, manual instrumentation and shutdown from the example. Do not register both providers for the same application.

## Protect prompt and response content

Content is captured by default. To hide inputs and outputs, replace the instrumentation constructor in the example with:

```javascript
const instrumentation = new GoogleGenAIInstrumentation({
  traceConfig: { hideInputs: true, hideOutputs: true },
});
```

See [trace configuration](../fi-core/README.md) for embedding-vector and error-detail controls. These settings affect telemetry, not your application's own logs.

## Troubleshooting

- **No spans with `@google/genai`:** this is the legacy adapter. It cannot instrument `GoogleGenAI` or `client.models`. Do not pass that module to `manuallyInstrument`.
- **No spans for streaming or chat-session calls:** use the support table above; those legacy methods are not wrapped. A direct non-streaming `generateContent` call can include conversation history in `contents`.
- **`provider.addSpanProcessor is not a function`:** OpenTelemetry 2.x takes `spanProcessors` in its constructor, as shown above.
- **Model not found:** check Google's current model availability and update `GOOGLE_MODEL`.

## License

Apache-2.0
