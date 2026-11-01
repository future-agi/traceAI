package ai.traceai.azure.openai;

import ai.traceai.FISpanKind;
import ai.traceai.FITracer;
import ai.traceai.TraceAI;
import com.azure.ai.openai.OpenAIAsyncClient;
import com.azure.ai.openai.models.*;
import io.opentelemetry.context.Context;
import io.opentelemetry.context.Scope;
import reactor.core.publisher.Flux;
import reactor.core.publisher.Mono;

import java.util.function.BiConsumer;
import java.util.function.Consumer;
import java.util.function.Supplier;

/**
 * Reactive chat, text completion, embedding and streaming tracing for Azure SDK beta.16.
 * Each subscription gets its own span, parented to the context at the wrapper method call.
 * Publishers remain cold: no request or span is started until subscription. Cancellation
 * is passed to the SDK publisher and ends the span without marking a partial response
 * successful. The beta.16 SSE publisher caches upstream: stream cancellation stops the
 * consumer, not the HTTP body. This wrapper preserves that SDK limitation.
 * Calls through {@link #unwrap()} and other SDK APIs are not traced by this wrapper.
 */
public final class TracedAzureOpenAIAsyncClient {
    private final OpenAIAsyncClient client;
    private final FITracer tracer;

    /**
     * Creates a traced wrapper without changing the underlying SDK client.
     *
     * @param client the Azure SDK client
     * @param tracer the tracer and content-capture configuration
     */
    public TracedAzureOpenAIAsyncClient(OpenAIAsyncClient client, FITracer tracer) {
        this.client = client;
        this.tracer = tracer;
    }

    /**
     * Creates a traced wrapper without changing the underlying SDK client.
     *
     * @param client the Azure SDK client; uses the global TraceAI tracer
     */
    public TracedAzureOpenAIAsyncClient(OpenAIAsyncClient client) {
        this(client, TraceAI.getTracer());
    }

    /**
     * Executes the corresponding SDK operation with semantic tracing. Each subscription creates a separate span.
     *
     * @param deployment the Azure deployment name
     * @param options the SDK request options (forwarded unchanged)
     * @return the cold SDK-compatible publisher
     */
    public Mono<ChatCompletions> getChatCompletions(String deployment, ChatCompletionsOptions options) {
        return mono("Chat Completion", FISpanKind.LLM, trace -> trace.request(deployment, options),
            () -> client.getChatCompletions(deployment, options), AzureOpenAITrace::response);
    }

    /**
     * Executes the corresponding SDK operation with semantic tracing. Each subscription creates a separate span.
     *
     * @param deployment the Azure deployment name
     * @param options the SDK request options (forwarded unchanged)
     * @return the cold SDK-compatible publisher
     */
    public Mono<Embeddings> getEmbeddings(String deployment, EmbeddingsOptions options) {
        return mono("Embedding", FISpanKind.EMBEDDING, trace -> trace.request(deployment, options),
            () -> client.getEmbeddings(deployment, options), AzureOpenAITrace::response);
    }

    /**
     * Executes the corresponding SDK operation with semantic tracing. Each subscription creates a separate span.
     *
     * @param deployment the Azure deployment name
     * @param options the SDK request options (forwarded unchanged)
     * @return the cold SDK-compatible publisher
     */
    public Mono<Completions> getCompletions(String deployment, CompletionsOptions options) {
        return mono("Completion", FISpanKind.LLM, trace -> trace.request(deployment, options),
            () -> client.getCompletions(deployment, options), AzureOpenAITrace::response);
    }

    /**
     * Streams chat or text completion chunks incrementally, without collecting them. Each subscription creates a separate span.
     *
     * @param deployment the Azure deployment name
     * @param options the SDK request options (forwarded unchanged)
     * @return the cold SDK-compatible publisher
     */
    public Flux<ChatCompletions> getChatCompletionsStream(String deployment, ChatCompletionsOptions options) {
        return flux("Chat Completion", trace -> trace.request(deployment, options),
            () -> client.getChatCompletionsStream(deployment, options), AzureOpenAITrace::chunk);
    }

    /**
     * Streams chat or text completion chunks incrementally, without collecting them. Each subscription creates a separate span.
     *
     * @param deployment the Azure deployment name
     * @param options the SDK request options (forwarded unchanged)
     * @param streamOptions the SDK streaming options, including opt-in usage reporting
     * @return the cold SDK-compatible publisher
     */
    public Flux<ChatCompletions> getChatCompletionsStream(String deployment, ChatCompletionsOptions options,
                                                        ChatCompletionStreamOptions streamOptions) {
        return flux("Chat Completion", trace -> trace.request(deployment, options),
            () -> client.getChatCompletionsStream(deployment, options, streamOptions), AzureOpenAITrace::chunk);
    }

    /**
     * Streams chat or text completion chunks incrementally, without collecting them. Each subscription creates a separate span.
     *
     * @param deployment the Azure deployment name
     * @param options the SDK request options (forwarded unchanged)
     * @return the cold SDK-compatible publisher
     */
    public Flux<Completions> getCompletionsStream(String deployment, CompletionsOptions options) {
        return flux("Completion", trace -> trace.request(deployment, options),
            () -> client.getCompletionsStream(deployment, options), AzureOpenAITrace::chunk);
    }

    /**
     * Streams chat or text completion chunks incrementally, without collecting them. Each subscription creates a separate span.
     *
     * @param deployment the Azure deployment name
     * @param options the SDK request options (forwarded unchanged)
     * @param streamOptions the SDK streaming options, including opt-in usage reporting
     * @return the cold SDK-compatible publisher
     */
    public Flux<Completions> getCompletionsStream(String deployment, CompletionsOptions options,
                                                ChatCompletionStreamOptions streamOptions) {
        return flux("Completion", trace -> trace.request(deployment, options),
            () -> client.getCompletionsStream(deployment, options, streamOptions), AzureOpenAITrace::chunk);
    }

    /**
     * Returns the SDK client; calls on it bypass this wrapper's semantic tracing.
     *
     * @return the original SDK client
     */
    public OpenAIAsyncClient unwrap() { return client; }

    private <T> Mono<T> mono(String operation, FISpanKind kind, Consumer<AzureOpenAITrace> input,
                             Supplier<Mono<T>> request, BiConsumer<AzureOpenAITrace, T> output) {
        Context parent = Context.current();
        return Mono.defer(() -> {
            AzureOpenAITrace trace = new AzureOpenAITrace(tracer, operation, kind, parent);
            try (Scope ignored = trace.span.makeCurrent()) {
                trace.capture(() -> input.accept(trace));
                return request.get()
                    .doOnSuccess(result -> {
                        if (result != null) trace.capture(() -> output.accept(trace, result));
                        trace.end(null, false);
                    })
                    .doOnError(error -> trace.end(error, false))
                    .doOnCancel(() -> trace.end(null, true));
            } catch (RuntimeException | Error error) {
                trace.end(error, false);
                return Mono.error(error);
            }
        });
    }

    private <T> Flux<T> flux(String operation, Consumer<AzureOpenAITrace> input,
                             Supplier<Flux<T>> request, BiConsumer<AzureOpenAITrace, T> output) {
        Context parent = Context.current();
        return Flux.defer(() -> {
            AzureOpenAITrace trace = new AzureOpenAITrace(tracer, operation, FISpanKind.LLM, parent);
            try (Scope ignored = trace.span.makeCurrent()) {
                trace.capture(() -> input.accept(trace));
                return request.get()
                    .doOnNext(result -> trace.capture(() -> output.accept(trace, result)))
                    .doOnComplete(() -> trace.end(null, false))
                    .doOnError(error -> trace.end(error, false))
                    .doOnCancel(() -> trace.end(null, true));
            } catch (RuntimeException | Error error) {
                trace.end(error, false);
                return Flux.error(error);
            }
        });
    }
}
