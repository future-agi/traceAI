# OpenAI Java tracing

`TracingHttpClient` decorates the official SDK's HTTP transport for chat completions,
Responses creation, and embeddings. Keep using the SDK's services, typed results,
async futures, and closeable streams.

## Configure the transport

Initialize TraceAI once using your application's Future AGI configuration, or supply
an existing `FITracer` as the second argument to `wrap`. With OpenAI Java 4.95.0:

```java
import ai.traceai.TraceAI;
import ai.traceai.openai.TracingHttpClient;
import com.openai.client.OpenAIClient;
import com.openai.client.OpenAIClientImpl;
import com.openai.client.okhttp.OkHttpClient;
import com.openai.core.ClientOptions;
import com.openai.models.responses.ResponseCreateParams;

TraceAI.initFromEnvironment();
OpenAIClient client = new OpenAIClientImpl(ClientOptions.builder()
    .httpClient(TracingHttpClient.wrap(OkHttpClient.builder().build()))
    .apiKey(System.getenv("OPENAI_API_KEY"))
    .build());

try {
    var response = client.responses().create(ResponseCreateParams.builder()
        .model("gpt-4o-mini")
        .input("Suggest a short title for a guide to Java streams.")
        .build());
    System.out.println(response.id());
} finally {
    try {
        client.close();
    } finally {
        TraceAI.shutdown();
    }
}
```

The OpenAI SDK is a **provided** dependency: your application supplies its version.
The adapter uses the HTTP interfaces shared by 0.8.0 and 4.95.0, not generated model
classes. The legacy facade below still uses the 0.8 model packages.

## Streaming and asynchronous calls

Use `client.chat().completions().createStreaming(params)` or
`client.responses().createStreaming(params)` in try-with-resources. Close a stream
when stopping early; neither acquisition nor tracing drains it. Async service calls
remain available through `client.async()`, including async streaming.

Spans follow the HTTP response lifecycle and end on EOF, read failure, or close.
A stream that closes without a completion marker has
`traceai.response.closed_early=true`, rather than a successful-generation status.
Each HTTP attempt is a separate span, including retries. Wrapping the same
transport twice does not add a second tracing layer.

Cancellation passed to the HTTP adapter propagates to its transport future. The
SDK's generated service futures do not necessarily propagate cancellation to HTTP.
Close the client at shutdown: this also releases responses abandoned by cancelled
service futures and ends their spans.

Use an undecorated SDK client for Realtime/WebSocket connections. Other HTTP
endpoints pass through without an AI span.

## Content and privacy

`TraceConfig` controls content capture. Set `hideInputs(true)`, `hideOutputs(true)`,
and `hideErrorDetails(true)` when initializing tracing to retain request metadata
without prompt, response, or error details. Message hiding also excludes those
messages from raw payload attributes. Authentication headers are never captured.

Wire metadata capture is bounded to 64 KiB per JSON payload or SSE event. Oversized
payloads reach the application unchanged but may have no extracted metadata.
Streamed primary text is capped at 32,000 characters; the adapter never stores a
list of chunks. Token usage is recorded only if returned by the provider. Streaming
tool-call deltas and secondary chat choices are not aggregated into output messages.

## Existing 0.8 applications

`TracedOpenAIClient` keeps its constructor and original method descriptors. Its
`streamChatCompletion` method now returns a lazy, single-use iterable instead of a
buffered list. Complete iteration closes it. Prefer the additive closeable API for
early termination:

```java
try (var response = traced.streamChatCompletionResponse(params)) {
    response.stream().forEach(chunk -> chunk.choices().forEach(choice ->
        choice.delta().content().ifPresent(System.out::print)));
}
```

The legacy facade and the HTTP adapter suppress duplicate spans if combined.
For OpenAI Java 4.x, use `TracingHttpClient` and the SDK's current model imports
instead of the legacy facade.
