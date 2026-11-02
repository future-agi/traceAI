# @traceai/fi-instrumentation-vertexai

Trace generation, chat, streaming responses and token-count requests from the legacy `@google-cloud/vertexai` SDK in Future AGI.

Google has deprecated this SDK in favor of [`@google/genai`](https://github.com/googleapis/js-genai). Use this adapter for existing Vertex AI SDK applications; it does not instrument the modern Google Gen AI SDK. See Google's [migration guide](https://cloud.google.com/vertex-ai/generative-ai/docs/deprecations/genai-vertexai-sdk) when moving an application.

## Install

Use Node.js 18.19+ on the 18.x line, or Node.js 20.6+ (a maintained Node.js release is recommended).

```sh
npm install @traceai/fi-instrumentation-vertexai @traceai/fi-core @google-cloud/vertexai@"^1.0.0" @opentelemetry/api@"^1.9.0" @opentelemetry/context-async-hooks@"^2.0.1"
```

Configure Google Application Default Credentials and set `GOOGLE_CLOUD_PROJECT` for the Google project where Vertex AI is enabled. Set `FI_API_KEY` and `FI_SECRET_KEY` for Future AGI. Keep credentials outside your source code.

## Trace a request

Save this as `vertex.cjs`. Manual instrumentation works when the SDK is already loaded, so the example does not depend on import order.

```javascript
const { context } = require("@opentelemetry/api");
const { AsyncLocalStorageContextManager } = require("@opentelemetry/context-async-hooks");
const { register, ProjectType } = require("@traceai/fi-core");
const { VertexAIInstrumentation } = require("@traceai/fi-instrumentation-vertexai");
const vertexai = require("@google-cloud/vertexai");

// In an application with an existing OTel SDK, reuse its context manager.
context.setGlobalContextManager(new AsyncLocalStorageContextManager().enable());
const provider = register({ projectName: "vertexai-app", projectType: ProjectType.OBSERVE });
const instrumentation = new VertexAIInstrumentation();
instrumentation.setTracerProvider(provider);
instrumentation.manuallyInstrument(vertexai);

async function main() {
  try {
    const vertex = new vertexai.VertexAI({
      project: process.env.GOOGLE_CLOUD_PROJECT,
      location: process.env.GOOGLE_CLOUD_LOCATION || "us-central1",
    });
    const model = vertex.getGenerativeModel({ model: "gemini-2.5-flash" });
    const result = await model.generateContent("Explain why the sky looks blue in one sentence.");
    console.log(result.response.candidates?.[0]?.content?.parts);
  } finally {
    instrumentation.disable();
    await provider.shutdown();
  }
}

main().catch(console.error);
```

Run `node vertex.cjs`, then open the **vertexai-app** project in Future AGI to inspect the request and response. `register()` exports standard OTLP to `https://api.futureagi.com/tracer/v1/traces` using your Future AGI credentials.

CommonJS applications can alternatively register the instrumentation before requiring the SDK to use automatic module hooks. For native ESM, import the SDK namespace and pass it to `manuallyInstrument()`; do not rely on static import ordering for automatic hooks.

## Capture and privacy

The adapter covers the stable and preview `GenerativeModel` and `ChatSession` APIs in SDK versions `>=1.0.0 <2.0.0`. It records model, generation settings, chat history, text and tool content, finish reasons, and usage returned by the SDK. It does not add embedding or cached-content management APIs that these generation hooks do not implement.

Streaming spans finish when the SDK's existing `response` promise settles. You can consume `result.stream`, await `result.response`, or do both; the adapter does not consume your iterator or replace either object. Breaking out of iteration does not cancel the SDK's independent aggregation. Configure the SDK's request timeout when you need to limit request duration.

Content capture is bounded. Raw request/response bodies, media bytes, file URIs, and provider error messages or stacks are not exported. Text, function arguments and function results may still contain sensitive information. To disable content capture, construct the instrumentation with:

```javascript
const instrumentation = new VertexAIInstrumentation({
  traceConfig: { hideInputs: true, hideOutputs: true },
});
```

The shared FI controls also support input/output message hiding, text hiding, PII redaction, and the corresponding `FI_HIDE_*` environment variables. Content is kept in the standard `gen_ai.input.messages` and `gen_ai.output.messages` attributes so granular text controls also apply to function data.

Calling `disable()` restores this instance's automatic and manual hooks. Requests already in progress still complete their spans. Do not register an additional instrumentor for the same SDK methods.

## Troubleshooting

- **No spans:** pass the provider returned by `register()` to `setTracerProvider()` and pass the actual SDK module to `manuallyInstrument()` before making requests.
- **Authentication or model errors:** check your Google credentials, project, regional model availability and SDK response. Provider errors retain their original behavior in your application; spans record failure without copying provider error text.
- **Missing stream spans at shutdown:** await the SDK's `result.response` before shutting down the tracer provider.

## License

Apache-2.0.
