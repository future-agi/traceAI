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
import software.amazon.awssdk.services.bedrockruntime.model.ConverseStreamRequest;
import software.amazon.awssdk.services.bedrockruntime.model.ConverseStreamResponseHandler;
import software.amazon.awssdk.services.bedrockruntime.model.InvokeModelRequest;
import software.amazon.awssdk.services.bedrockruntime.model.InvokeModelResponse;

import java.util.Objects;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.CompletionException;
import java.util.function.Supplier;

/**
 * Traces non-streaming async Converse and InvokeModel through completion,
 * failure or cancellation. The returned future completes after tracing finishes
 * and forwards cancellation and caller completion (including successful and exceptional
 * timeouts) to the SDK future. ConverseStream is supported through an event-stream adapter;
 * InvokeModelWithResponseStream is not supported.
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
     * @return a future completed after tracing, with cancellation forwarded to the SDK
     */
    public CompletableFuture<ConverseResponse> converse(ConverseRequest request) {
        return execute(request, () -> client.converse(request));
    }

    /**
     * @param request the request
     * @return a future completed after tracing, with cancellation forwarded to the SDK
     */
    public CompletableFuture<InvokeModelResponse> invokeModel(InvokeModelRequest request) {
        return execute(request, () -> client.invokeModel(request));
    }

    /**
     * Traces a Converse event stream through completion, failure or cancellation.
     * Original response/events and downstream demand are forwarded unchanged. Headers
     * alone never finish the span. Closing the subscription ends the span and cancels
     * the SDK future if still pending; AWS may complete it normally during cancellation.
     *
     * @param request the streaming request
     * @param handler the application's event-stream response handler
     * @return a cancellation-forwarding future completed after the span ends
     */
    public CompletableFuture<Void> converseStream(ConverseStreamRequest request, ConverseStreamResponseHandler handler) {
        return new BedrockConverseStream(tracing, request, handler).execute(client);
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
        class TracedFuture extends CompletableFuture<T> {
            @Override
            public boolean cancel(boolean mayInterruptIfRunning) {
                // Let the SDK decide which outcome wins. Its single completion callback
                // ends the span before publishing the outcome to application callbacks.
                return future.cancel(mayInterruptIfRunning);
            }

            @Override
            public boolean complete(T response) {
                // CompletableFuture.completeOnTimeout uses this method too. The SDK's
                // completion callback finalizes tracing before publishing the fallback.
                return future.complete(response);
            }

            @Override
            public boolean completeExceptionally(Throwable error) {
                // CompletableFuture.orTimeout uses this method too. Forward first, so
                // SDK cleanup and tracing cannot be bypassed by a caller-side timeout.
                return future.completeExceptionally(error);
            }

            void publish(T response, Throwable error) {
                // Bypass forwarding: only the SDK completion callback publishes here.
                if (error == null) super.complete(response);
                else super.completeExceptionally(error);
            }
        }
        TracedFuture result = new TracedFuture();
        future.whenComplete((response, error) -> {
            try {
                if (error == null) tracing.complete(span, response);
                else tracing.fail(span, error instanceof CompletionException && error.getCause() != null ? error.getCause() : error);
            } finally {
                try {
                    span.end();
                } finally {
                    // Preserve the SDK outcome even if telemetry finalization throws.
                    result.publish(response, error);
                }
            }
        });
        return result;
    }
}
