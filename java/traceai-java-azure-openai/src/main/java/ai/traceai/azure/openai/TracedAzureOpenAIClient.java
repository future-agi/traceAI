package ai.traceai.azure.openai;

import ai.traceai.FISpanKind;
import ai.traceai.FITracer;
import ai.traceai.TraceAI;
import com.azure.ai.openai.OpenAIClient;
import com.azure.ai.openai.models.*;
import com.azure.core.util.IterableStream;
import io.opentelemetry.context.Context;
import io.opentelemetry.context.Scope;

import java.util.function.Consumer;
import java.util.function.Supplier;

/**
 * Semantic tracing for blocking chat, embeddings, and text completions, and incremental
 * chat/text streams on azure-ai-openai 1.0.0-beta.16. For reactive applications use
 * {@link TracedAzureOpenAIAsyncClient}.
 *
 * <p>Close streams when stopping early, preferably with try-with-resources. This wrapper
 * does not trace Responses, audio, images, files, or calls through {@link #unwrap()}.</p>
 *
 * <p>Microsoft has stopped updating this SDK in its current form and recommends official
 * OpenAI Java for new features; it is not a drop-in replacement for this client.</p>
 */
public class TracedAzureOpenAIClient {
    private final OpenAIClient client;
    private final FITracer tracer;

    /**
     * Creates a traced wrapper without changing the underlying SDK client.
     *
     * @param client the Azure SDK client
     * @param tracer the tracer and content-capture configuration
     */
    public TracedAzureOpenAIClient(OpenAIClient client, FITracer tracer) {
        this.client = client;
        this.tracer = tracer;
    }

    /**
     * Creates a traced wrapper without changing the underlying SDK client.
     *
     * @param client the Azure SDK client; uses the global TraceAI tracer
     */
    public TracedAzureOpenAIClient(OpenAIClient client) {
        this(client, TraceAI.getTracer());
    }

    /**
     * Executes the corresponding SDK operation with semantic tracing.
     *
     * @param deployment the Azure deployment name
     * @param options the SDK request options (forwarded unchanged)
     * @return the SDK response
     */
    public ChatCompletions getChatCompletions(String deployment, ChatCompletionsOptions options) {
        AzureOpenAITrace trace = trace("Chat Completion", FISpanKind.LLM);
        trace.capture(() -> trace.request(deployment, options));
        return call(trace, () -> client.getChatCompletions(deployment, options), trace::response);
    }

    /**
     * Executes the corresponding SDK operation with semantic tracing.
     *
     * @param deployment the Azure deployment name
     * @param options the SDK request options (forwarded unchanged)
     * @return the SDK response
     */
    public Embeddings getEmbeddings(String deployment, EmbeddingsOptions options) {
        AzureOpenAITrace trace = trace("Embedding", FISpanKind.EMBEDDING);
        trace.capture(() -> trace.request(deployment, options));
        return call(trace, () -> client.getEmbeddings(deployment, options), trace::response);
    }

    /**
     * Executes the corresponding SDK operation with semantic tracing.
     *
     * @param deployment the Azure deployment name
     * @param options the SDK request options (forwarded unchanged)
     * @return the SDK response
     */
    public Completions getCompletions(String deployment, CompletionsOptions options) {
        AzureOpenAITrace trace = trace("Completion", FISpanKind.LLM);
        trace.capture(() -> trace.request(deployment, options));
        return call(trace, () -> client.getCompletions(deployment, options), trace::response);
    }

    /**
     * Streams chat or text completion chunks incrementally, without collecting them. Close the result or its Java stream on early exit.
     *
     * @param deployment the Azure deployment name
     * @param options the SDK request options (forwarded unchanged)
     * @return an incremental, closeable SDK-compatible result
     */
    public TracedAzureOpenAIStream<ChatCompletions> getChatCompletionsStream(
            String deployment, ChatCompletionsOptions options) {
        AzureOpenAITrace trace = trace("Chat Completion", FISpanKind.LLM);
        trace.capture(() -> trace.request(deployment, options));
        return stream(trace, () -> client.getChatCompletionsStream(deployment, options), trace::chunk);
    }

    /**
     * Streams chat or text completion chunks incrementally, without collecting them. Close the result or its Java stream on early exit.
     *
     * @param deployment the Azure deployment name
     * @param options the SDK request options (forwarded unchanged)
     * @param streamOptions the SDK streaming options, including opt-in usage reporting
     * @return an incremental, closeable SDK-compatible result
     */
    public TracedAzureOpenAIStream<ChatCompletions> getChatCompletionsStream(
            String deployment, ChatCompletionsOptions options, ChatCompletionStreamOptions streamOptions) {
        AzureOpenAITrace trace = trace("Chat Completion", FISpanKind.LLM);
        trace.capture(() -> trace.request(deployment, options));
        return stream(trace, () -> client.getChatCompletionsStream(deployment, options, streamOptions), trace::chunk);
    }

    /**
     * Streams chat or text completion chunks incrementally, without collecting them. Close the result or its Java stream on early exit.
     *
     * @param deployment the Azure deployment name
     * @param options the SDK request options (forwarded unchanged)
     * @return an incremental, closeable SDK-compatible result
     */
    public TracedAzureOpenAIStream<Completions> getCompletionsStream(
            String deployment, CompletionsOptions options) {
        AzureOpenAITrace trace = trace("Completion", FISpanKind.LLM);
        trace.capture(() -> trace.request(deployment, options));
        return stream(trace, () -> client.getCompletionsStream(deployment, options), trace::chunk);
    }

    /**
     * Streams chat or text completion chunks incrementally, without collecting them. Close the result or its Java stream on early exit.
     *
     * @param deployment the Azure deployment name
     * @param options the SDK request options (forwarded unchanged)
     * @param streamOptions the SDK streaming options, including opt-in usage reporting
     * @return an incremental, closeable SDK-compatible result
     */
    public TracedAzureOpenAIStream<Completions> getCompletionsStream(
            String deployment, CompletionsOptions options, ChatCompletionStreamOptions streamOptions) {
        AzureOpenAITrace trace = trace("Completion", FISpanKind.LLM);
        trace.capture(() -> trace.request(deployment, options));
        return stream(trace, () -> client.getCompletionsStream(deployment, options, streamOptions), trace::chunk);
    }

    /**
     * Returns the SDK client; calls on it bypass this wrapper's semantic tracing.
     *
     * @return the original SDK client
     */
    public OpenAIClient unwrap() { return client; }

    private AzureOpenAITrace trace(String operation, FISpanKind kind) {
        return new AzureOpenAITrace(tracer, operation, kind, Context.current());
    }

    private <T> T call(AzureOpenAITrace trace, Supplier<T> request, Consumer<T> response) {
        try (Scope ignored = trace.span.makeCurrent()) {
            T result = request.get();
            trace.capture(() -> response.accept(result));
            trace.end(null, false);
            return result;
        } catch (RuntimeException | Error error) {
            trace.end(error, false);
            throw error;
        }
    }

    private <T> TracedAzureOpenAIStream<T> stream(
            AzureOpenAITrace trace, Supplier<IterableStream<T>> request, Consumer<T> chunk) {
        try (Scope ignored = trace.span.makeCurrent()) {
            return new TracedAzureOpenAIStream<>(request.get(), trace, chunk);
        } catch (RuntimeException | Error error) {
            trace.end(error, false);
            throw error;
        }
    }
}
