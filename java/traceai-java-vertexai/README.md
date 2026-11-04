# Vertex AI Java tracing

Trace existing applications that use `com.google.cloud.vertexai.generativeai.GenerativeModel` with the maintained `com.futureagi:traceai-java-vertexai` wrapper.

Google deprecated the generative AI modules of the Vertex AI SDK. For new applications, use the Google Gen AI SDK instead; see [Google's migration guide](https://cloud.google.com/vertex-ai/generative-ai/docs/deprecations/genai-vertexai-sdk). This package supports the legacy `google-cloud-vertexai` API from 1.7.0 through 1.52.0, with Java 17 or later. The Vertex AI hosted service is separate from the deprecated SDK modules.

## Configure tracing

Add `com.futureagi:traceai-java-vertexai` at the same version as your other TraceAI Java modules, and add `com.google.cloud:google-cloud-vertexai` to your application. The Google dependency is provided by the application, not bundled by the wrapper.

Set `FI_API_KEY`, `FI_SECRET_KEY`, `GOOGLE_CLOUD_PROJECT`, `GOOGLE_CLOUD_LOCATION`, and `GOOGLE_GENAI_MODEL`. Use [Application Default Credentials](https://cloud.google.com/docs/authentication/provide-credentials-adc) for Google Cloud. Choose a model available in your project and location.

```java
import ai.traceai.TraceAI;
import ai.traceai.TraceConfig;
import ai.traceai.vertexai.TracedGenerativeModel;
import com.google.cloud.vertexai.VertexAI;
import com.google.cloud.vertexai.generativeai.GenerativeModel;

TraceAI.init(TraceConfig.builder()
    .baseUrl("https://api.futureagi.com")
    .apiKey(System.getenv("FI_API_KEY"))
    .secretKey(System.getenv("FI_SECRET_KEY"))
    .projectName("vertex-java-app")
    .build());

try (VertexAI vertex = new VertexAI(
        System.getenv("GOOGLE_CLOUD_PROJECT"),
        System.getenv("GOOGLE_CLOUD_LOCATION"))) {
    GenerativeModel model = new GenerativeModel(System.getenv("GOOGLE_GENAI_MODEL"), vertex);
    TracedGenerativeModel traced = new TracedGenerativeModel(model);
    try (var responses = traced.generateContentStream("Explain how rainbows form.").stream()) {
        responses.forEach(response -> System.out.println(response));
    }
} finally {
    TraceAI.shutdown();
}
```

Initialize TraceAI once per application. Its HTTP exporter sends OTLP to `https://api.futureagi.com/tracer/v1/traces`. In a service, flush with `TraceAI.shutdown()` during application shutdown, not after each request. Open the `vertex-java-app` project in Future AGI to inspect the model call.

## Streaming lifecycle

The span stays open until iteration observes the end of the stream or fails. It records the first candidate's combined text, finish reason and final token usage when the SDK supplies them. Returned response objects are unchanged. Once fully consumed, the SDK's response-history replay still works and does not emit another span.

Use the Java `Stream` API in try-with-resources when you may stop early, including `findFirst()` and `limit()`. Closing records the partial answer and marks `fi.stream.incomplete` without reporting a successful completion. The legacy SDK does not expose RPC cancellation through `ResponseStream`; closing the Java stream ends tracing, not the underlying request. A plain iterator must be consumed to exhaustion to finish its span.

Streaming telemetry retains at most 32,000 characters of answer text and 32,000 characters of raw chunk JSON. Omitted raw chunks set `fi.stream.raw_output_truncated`. These bounds do not change the SDK's own response history or the responses your application receives.

## Privacy

Request and response content is captured by default. To hide it, configure `.hideInputs(true)` and `.hideOutputs(true)` on `TraceConfig.builder()` before initialization. `.hideErrorDetails(true)` removes error messages and stack traces while retaining exception types and error status. Token usage and model/provider metadata remain available. Do not add sensitive content to custom span attributes.

Unary `generateContent(String)`, `generateContent(List<Content>)`, and `countTokens(String)` remain supported. Use one wrapper per call path rather than wrapping an already traced call again.
