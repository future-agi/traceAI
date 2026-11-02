# Semantic Kernel tracing

Trace Java Semantic Kernel function invocations, model calls, streams and embeddings with the existing `TracedKernel`, `TracedChatCompletionService` and `TracedTextEmbeddingGenerationService` entry points.

## Requirements

Use Java 17 or later and matching versions of `semantickernel-api` and `semantickernel-aiservices-openai`. The supported versions are **1.3.0, 1.4.5 and 1.5.0**; use **1.5.0** for new applications.

Existing 1.3.0 applications can keep using the same adapter constructors and public invocation methods. On 1.3.0, the adapter traces subscription-scoped function, chat, streaming and embedding boundaries, but cannot route Semantic Kernel's own static global telemetry into the invocation. If you configure a global OpenTelemetry SDK separately, Semantic Kernel 1.3.0 may emit additional model spans outside the adapter's privacy controls; upgrade Semantic Kernel to 1.4.5 or 1.5.0 to use invocation-scoped native model telemetry, deduplicated token attribution and privacy filtering. Do not enable the 1.3.0 global telemetry for sensitive model data.

Install `com.futureagi:traceai-java-semantic-kernel` with the same traceAI release as your Java core dependency. See the [Java SDK setup](../README.md) for Maven Central coordinates and authentication. Semantic Kernel dependencies are provided by your application, not bundled by this artifact.

## Trace a kernel

After initializing traceAI and creating your application's `Kernel`:

```java
import ai.traceai.semantickernel.TracedKernel;

TracedKernel traced = new TracedKernel(kernel);
var result = traced.invokePromptAsync("Summarize the benefits of retrieval-augmented generation.")
    .block();
System.out.println(result.getResult());
```

The root invocation is an `AGENT` span. The adapter routes Semantic Kernel's native telemetry through your traceAI tracer: model calls become `LLM` children and nested function calls become `TOOL` spans with `gen_ai.tool.name` set to `plugin-function`. The native span for the root function shares the root observation instead of exporting a second function span. Token usage belongs only to model spans, never the root invocation.

On 1.4.5 and 1.5.0, nested calls include functions called from prompt templates (`{{plugin.function}}` and Handlebars helpers) and functions that native code calls through the `Kernel` it receives. To route these, the invocation runs on a view of your kernel with the same plugins, services and hooks. Code inside a function therefore receives that view, not your original `Kernel` instance, so do not compare kernels by identity.

You can also supply an `FITracer` explicitly with `new TracedKernel(kernel, tracer)`. The adapter does not replace the global OpenTelemetry instance or install another exporter. When the maintained OpenTelemetry Reactor context bridge (`opentelemetry-reactor-3.1`) is on the classpath, which Semantic Kernel 1.4.5 and later bring, the adapter registers it so context propagates through Reactor operators and scheduler hops. This artifact does not add the bridge to your dependencies. Semantic Kernel 1.3.0 does not bring it; there, the adapter's spans still nest correctly, but `Span.current()` in your own code after a scheduler hop is not set unless you add the bridge yourself.

## Capture service-level content, streaming and embeddings

Keep the service wrappers when you need their additional coverage:

```java
import ai.traceai.semantickernel.TracedChatCompletionService;
import ai.traceai.semantickernel.TracedTextEmbeddingGenerationService;

var tracedChat = new TracedChatCompletionService(chatService, modelId, "openai");
var tracedEmbeddings = new TracedTextEmbeddingGenerationService(embeddingService, embeddingModelId, "openai");
```

- For native unary chat, the service boundary is a `CHAIN` span containing input/output content. Semantic Kernel's native `LLM` spans describe each model round, including rounds before an automatic tool call. The chain does not repeat their token counts.
- On 1.4.5 and 1.5.0, functions that Semantic Kernel calls automatically for the model (`ToolCallBehavior` auto-invoke) become `TOOL` spans in the same trace, with the same privacy settings. The adapter does this with a pre-tool-call hook that runs after your own pre-tool-call hooks, so it traces the function your hooks choose.
- For custom chat services without native telemetry, the boundary remains an `LLM` span and reads usage from returned message metadata when available.
- Streaming uses one `LLM` span for the subscription. Chunks pass through unchanged. Captured text is bounded to 32,000 characters, with `semantic_kernel.output_truncated=true` when truncated. Token counts are not inferred for streaming.
- Embedding calls use `EMBEDDING` spans with input count, vector count and dimensions when available. Vector values are not recorded.

Use only one wrapper at each service boundary. Do not also instrument the same model operation with another semantic LLM wrapper; independently configured provider instrumentation is outside this adapter's control.

## Privacy

Configure the tracer before creating wrappers:

```java
import ai.traceai.TraceAI;
import ai.traceai.TraceConfig;

TraceConfig config = TraceConfig.builder()
    .apiKey(System.getenv("FI_API_KEY"))
    .secretKey(System.getenv("FI_SECRET_KEY"))
    .projectName(System.getenv("FI_PROJECT_NAME"))
    .hideInputs(true)
    .hideOutputs(true)
    .hideErrorDetails(true)
    .build();
TraceAI.init(config);
```

`hideInputs` covers values, messages, raw inputs and indexed embedding text. `hideOutputs` covers values, messages and raw outputs; streaming text is not accumulated when outputs are hidden. `hideErrorDetails` removes exception messages, stack traces and error status descriptions from both wrapper spans and the native Semantic Kernel spans routed by the adapter. Error types and error status remain available. These settings do not sanitize independently configured instrumentation or attributes your application adds itself.

## Reactive lifecycle

No span starts and no delegate operation runs until subscription. Every subscription creates new invocation state, including retries and concurrent subscriptions. Spans end on success, error or cancellation; cancellation sets `semantic_kernel.cancelled=true` without reporting success. Partial stream text follows the same output privacy settings.

Cancellation is forwarded to the delegate publisher, and the adapter promptly ends native spans left open by cancellation. Aborting an underlying network request still depends on the Semantic Kernel/Azure SDK transport. Semantic Kernel's own telemetry watchdog can remain scheduled until its upstream timeout; it is not the adapter's span-completion mechanism.

Do not rely on creating a `Mono` or `Flux` to run a request: subscribe, compose it into your application's reactive flow, or use `block()`/`blockLast()` at an appropriate synchronous boundary.
