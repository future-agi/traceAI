package ai.traceai.bedrock;

import ai.traceai.FITracer;
import ai.traceai.TraceAI;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.context.Context;
import io.opentelemetry.context.Scope;
import software.amazon.awssdk.core.SdkRequest;
import software.amazon.awssdk.core.SdkResponse;
import software.amazon.awssdk.services.bedrockruntime.BedrockRuntimeAsyncClient;
import software.amazon.awssdk.services.bedrockruntime.model.ConverseRequest;
import software.amazon.awssdk.services.bedrockruntime.model.ConverseResponse;
import software.amazon.awssdk.services.bedrockruntime.model.InvokeModelRequest;
import software.amazon.awssdk.services.bedrockruntime.model.InvokeModelResponse;

import java.util.Objects;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.CompletionException;
import java.util.function.Supplier;

/**
 * Traces non-streaming async Converse and InvokeModel through completion,
 * failure or cancellation. Returns the SDK's original future, preserving its
 * cancellation semantics. Streaming operations are not supported.
 */
public final class TracedBedrockRuntimeAsyncClient {
    private final BedrockRuntimeAsyncClient client;
    private final BedrockTracing tracing;

    /**
     * @param client the existing async client
     * @param tracer the tracer used for this client's calls
     */
    public TracedBedrockRuntimeAsyncClient(BedrockRuntimeAsyncClient client, FITracer tracer) {
        this.client = Objects.requireNonNull(client, "client");
        tracing = new BedrockTracing(Objects.requireNonNull(tracer, "tracer"));
    }

    /** @param client the client to trace with the global TraceAI tracer */
    public TracedBedrockRuntimeAsyncClient(BedrockRuntimeAsyncClient client) {
        this(client, TraceAI.getTracer());
    }

    /**
     * @param request the request
     * @return the unchanged SDK future
     */
    public CompletableFuture<ConverseResponse> converse(ConverseRequest request) {
        return execute(request, () -> client.converse(request));
    }

    /**
     * @param request the request
     * @return the unchanged SDK future
     */
    public CompletableFuture<InvokeModelResponse> invokeModel(InvokeModelRequest request) {
        return execute(request, () -> client.invokeModel(request));
    }

    /** @return the original SDK client; calls through it are not traced */
    public BedrockRuntimeAsyncClient unwrap() {
        return client;
    }

    private <T extends SdkResponse> CompletableFuture<T> execute(SdkRequest request, Supplier<CompletableFuture<T>> operation) {
        Span span = tracing.start(request);
        CompletableFuture<T> future;
        try (Scope ignored = Context.current().with(span).with(BedrockTracing.WRAPPED_REQUEST, request).makeCurrent()) {
            future = operation.get();
        } catch (RuntimeException | Error error) {
            try {
                tracing.fail(span, error);
            } finally {
                span.end();
            }
            throw error;
        }
        // Observe the original future, not a dependent stage (which does not propagate cancellation).
        future.whenComplete((response, error) -> {
            try {
                if (error == null) tracing.complete(span, response);
                else tracing.fail(span, error instanceof CompletionException && error.getCause() != null ? error.getCause() : error);
            } finally {
                span.end();
            }
        });
        return future;
    }
}
