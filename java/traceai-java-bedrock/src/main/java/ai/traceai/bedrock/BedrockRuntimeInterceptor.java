package ai.traceai.bedrock;

import ai.traceai.FITracer;
import ai.traceai.TraceAI;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.context.Context;
import software.amazon.awssdk.core.ClientType;
import software.amazon.awssdk.core.SdkRequest;
import software.amazon.awssdk.core.interceptor.SdkExecutionAttribute;
import software.amazon.awssdk.core.interceptor.ExecutionAttribute;
import software.amazon.awssdk.core.interceptor.ExecutionAttributes;
import software.amazon.awssdk.core.interceptor.ExecutionInterceptor;
import software.amazon.awssdk.services.bedrockruntime.model.ConverseRequest;
import software.amazon.awssdk.services.bedrockruntime.model.InvokeModelRequest;

import java.util.Objects;
import java.util.concurrent.atomic.AtomicBoolean;

/**
 * AWS SDK execution hook for synchronous non-streaming
 * {@code converse} and {@code invokeModel} operations. Retries share one span.
 * Streaming operations are deliberately not traced: afterExecution is not a
 * complete stream lifecycle and must not be reported as one.
 *
 * <pre>{@code
 * BedrockRuntimeClient client = BedrockRuntimeClient.builder()
 *     .overrideConfiguration(c -> c.addExecutionInterceptor(new BedrockRuntimeInterceptor(tracer)))
 *     .build();
 * }</pre>
 *
 * <p>Async clients are ignored: AWS does not reliably call execution interceptors
 * when a returned future is cancelled. Use {@link TracedBedrockRuntimeAsyncClient}
 * for cancellation-safe async tracing. The LLM span inherits the calling context;
 * this hook does not reparent SDK HTTP spans.</p>
 */
public final class BedrockRuntimeInterceptor implements ExecutionInterceptor {
    private static final ExecutionAttribute<State> STATE = new ExecutionAttribute<>("traceai-bedrock-span");
    private final BedrockTracing tracing;

    /** @param tracer the tracer used by this hook */
    public BedrockRuntimeInterceptor(FITracer tracer) {
        tracing = new BedrockTracing(Objects.requireNonNull(tracer, "tracer"));
    }

    /** Uses the global TraceAI tracer. */
    public BedrockRuntimeInterceptor() {
        this(TraceAI.getTracer());
    }

    @Override
    public void beforeExecution(software.amazon.awssdk.core.interceptor.Context.BeforeExecution context,
                                ExecutionAttributes attributes) {
        if (attributes.getAttribute(SdkExecutionAttribute.CLIENT_TYPE) == ClientType.ASYNC) return;
        SdkRequest request = context.request();
        if (!(request instanceof ConverseRequest) && !(request instanceof InvokeModelRequest)) return;
        if (attributes.getAttribute(STATE) != null || Context.current().get(BedrockTracing.WRAPPED_REQUEST) == request) return;
        attributes.putAttribute(STATE, new State(tracing.start(request)));
    }

    @Override
    public void afterExecution(software.amazon.awssdk.core.interceptor.Context.AfterExecution context,
                               ExecutionAttributes attributes) {
        State state = attributes.getAttribute(STATE);
        if (state == null || !state.finished.compareAndSet(false, true)) return;
        try {
            tracing.complete(state.span, context.response());
        } finally {
            state.span.end();
        }
    }

    @Override
    public void onExecutionFailure(software.amazon.awssdk.core.interceptor.Context.FailedExecution context,
                                   ExecutionAttributes attributes) {
        State state = attributes.getAttribute(STATE);
        if (state == null || !state.finished.compareAndSet(false, true)) return;
        try {
            tracing.fail(state.span, context.exception());
        } finally {
            state.span.end();
        }
    }

    private static final class State {
        final Span span;
        final AtomicBoolean finished = new AtomicBoolean();

        State(Span span) {
            this.span = span;
        }
    }
}
