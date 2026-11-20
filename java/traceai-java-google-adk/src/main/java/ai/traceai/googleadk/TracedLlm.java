package ai.traceai.googleadk;

import ai.traceai.FISpanKind;
import ai.traceai.FITracer;
import ai.traceai.TraceAI;
import com.google.adk.models.BaseLlm;
import com.google.adk.models.BaseLlmConnection;
import com.google.adk.models.LlmRequest;
import com.google.adk.models.LlmResponse;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.context.Scope;
import io.reactivex.rxjava3.core.Flowable;

import java.util.Map;
import java.util.concurrent.atomic.AtomicReference;

/**
 * Traces every model call an ADK agent makes through the wrapped {@link BaseLlm}: one LLM span with the model, the
 * input and output messages (tool calls and tool results included), the tool definitions and the token counts.
 *
 * <pre>
 * LlmAgent agent = LlmAgent.builder().name("helper").model(new TracedLlm(new Gemini(...))).build();
 * </pre>
 *
 * The span is the current span while the model call is subscribed, so the agent's wire capture does not trace the
 * same HTTP call a second time. For a streamed call the span describes the last response ADK receives.
 */
public final class TracedLlm extends BaseLlm {
    private final BaseLlm delegate;
    private final FITracer tracer;

    public TracedLlm(BaseLlm delegate, FITracer tracer) {
        super(delegate.model());
        this.delegate = delegate;
        this.tracer = tracer;
    }

    public TracedLlm(BaseLlm delegate) {
        this(delegate, TraceAI.getTracer());
    }

    @Override
    public Flowable<LlmResponse> generateContent(LlmRequest llmRequest, boolean stream) {
        return Flowable.defer(() -> {
            Span span = tracer.startSpan("call_llm", FISpanKind.LLM);
            Map<String, Object> request = serialized(() -> AdkJson.request(llmRequest));
            AtomicReference<Map<String, Object>> last = new AtomicReference<>(Map.of());
            Flowable<LlmResponse> upstream;
            try (Scope ignored = span.makeCurrent()) {
                upstream = delegate.generateContent(llmRequest, stream);
            } catch (RuntimeException e) {
                finish(span, request, last.get(), e);
                throw e;
            }
            Flowable<LlmResponse> scoped = new Flowable<>() {
                @Override
                protected void subscribeActual(org.reactivestreams.Subscriber<? super LlmResponse> subscriber) {
                    try (Scope ignored = span.makeCurrent()) {
                        upstream.subscribe(subscriber);
                    }
                }
            };
            return scoped
                .doOnNext(response -> last.set(serialized(() -> AdkJson.response(response))))
                .doOnError(error -> finish(span, request, last.get(), error))
                .doOnComplete(() -> finish(span, request, last.get(), null))
                .doOnCancel(() -> finish(span, request, last.get(), null));
        });
    }

    /** Serializing for the span never fails the call, whatever it throws. */
    private static Map<String, Object> serialized(java.util.function.Supplier<Map<String, Object>> shape) {
        try {
            return shape.get();
        } catch (Throwable ignored) {
            return Map.of();
        }
    }

    private void finish(Span span, Map<String, Object> request, Map<String, Object> response, Throwable error) {
        if (!span.isRecording()) return;
        try {
            try {
                SpanAttributes.write(tracer, span, AdkAttributes.llm(request, response));
                String failure = error != null ? null : AdkAttributes.llmError(response);
                if (error != null) tracer.setError(span, error);
                else if (failure != null) {
                    // The model's own error text follows the same switch as every other error detail.
                    span.setStatus(StatusCode.ERROR, tracer.getConfig().isHideErrorDetails() ? "" : failure);
                } else span.setStatus(StatusCode.OK);
            } catch (Throwable tracingFailure) {
                // Tracing never fails the model call.
            }
        } finally {
            try {
                span.end();
            } catch (Throwable ignored) {
                // Nothing to do.
            }
        }
    }

    @Override
    public BaseLlmConnection connect(LlmRequest llmRequest) {
        return delegate.connect(llmRequest);
    }
}
