package ai.traceai.bedrock;

import ai.traceai.FITracer;
import ai.traceai.TraceAI;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.context.Context;
import io.opentelemetry.context.Scope;
import software.amazon.awssdk.core.SdkRequest;
import software.amazon.awssdk.core.SdkResponse;
import software.amazon.awssdk.services.bedrockruntime.BedrockRuntimeClient;
import software.amazon.awssdk.services.bedrockruntime.model.ConverseRequest;
import software.amazon.awssdk.services.bedrockruntime.model.ConverseResponse;
import software.amazon.awssdk.services.bedrockruntime.model.InvokeModelRequest;
import software.amazon.awssdk.services.bedrockruntime.model.InvokeModelResponse;

import java.util.Objects;
import java.util.function.Supplier;

/**
 * Backwards-compatible facade for synchronous InvokeModel and Converse calls.
 * For direct synchronous SDK clients, attach {@link BedrockRuntimeInterceptor}
 * through the SDK builder instead. For async calls, use {@link TracedBedrockRuntimeAsyncClient}.
 * Calls through {@link #unwrap()} are traced only if the underlying client has that interceptor.
 */
public class TracedBedrockRuntimeClient {
    private final BedrockRuntimeClient client;
    private final BedrockTracing tracing;

    /**
     * @param client the existing Bedrock client
     * @param tracer the tracer used for this client's calls
     */
    public TracedBedrockRuntimeClient(BedrockRuntimeClient client, FITracer tracer) {
        this.client = Objects.requireNonNull(client, "client");
        this.tracing = new BedrockTracing(Objects.requireNonNull(tracer, "tracer"));
    }

    /** @param client the client to trace using the global TraceAI tracer */
    public TracedBedrockRuntimeClient(BedrockRuntimeClient client) {
        this(client, TraceAI.getTracer());
    }

    /**
     * @param request the request
     * @return the unchanged SDK response
     */
    public InvokeModelResponse invokeModel(InvokeModelRequest request) {
        return execute(request, () -> client.invokeModel(request));
    }

    /**
     * @param request the request
     * @return the unchanged SDK response
     */
    public ConverseResponse converse(ConverseRequest request) {
        return execute(request, () -> client.converse(request));
    }

    /** @return the original, unmodified SDK client */
    public BedrockRuntimeClient unwrap() {
        return client;
    }

    private <T extends SdkResponse> T execute(SdkRequest request, Supplier<T> operation) {
        Span span = tracing.start(request);
        // The execution hook suppresses only this exact wrapped request, not nested calls.
        try (Scope ignored = Context.current().with(span).with(BedrockTracing.WRAPPED_REQUEST, request).makeCurrent()) {
            T response = operation.get();
            tracing.complete(span, response);
            return response;
        } catch (RuntimeException | Error error) {
            tracing.fail(span, error);
            throw error;
        } finally {
            span.end();
        }
    }
}
