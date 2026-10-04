package ai.traceai.spring;

import ai.traceai.FITracer;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.context.Context;
import io.opentelemetry.context.Scope;
import org.reactivestreams.Subscription;
import org.springframework.ai.chat.model.ChatResponse;
import reactor.core.CoreSubscriber;
import reactor.core.publisher.Operators;

import java.util.Objects;
import java.util.concurrent.atomic.AtomicInteger;

/**
 * Package-private bounded adapter that owns exactly one span for one subscription of a traced stream.
 *
 * <p>State machine: {@code OPEN -> COMPLETE | ERROR | CANCEL}, decided once by compare-and-set. The winning
 * terminal path records the span outcome and ends the span in its {@code finally}; later signals are dropped.
 * Demand is forwarded exactly; nothing is buffered, cached, collected or requested eagerly.</p>
 */
final class TracingSubscriber implements CoreSubscriber<ChatResponse>, Subscription {

    static final int OPEN = 0;
    static final int COMPLETE = 1;
    static final int ERROR = 2;
    static final int CANCEL = 3;

    static final String CANCELLED_DESCRIPTION = "cancelled";

    private final CoreSubscriber<? super ChatResponse> actual;
    private final FITracer tracer;
    private final Span span;
    private final StreamOutputCapture capture;
    private final AtomicInteger state = new AtomicInteger(OPEN);
    private volatile Subscription upstream;
    private volatile boolean downstreamSubscribed;

    TracingSubscriber(CoreSubscriber<? super ChatResponse> actual, FITracer tracer, Span span, StreamOutputCapture capture) {
        this.actual = Objects.requireNonNull(actual, "actual");
        this.tracer = Objects.requireNonNull(tracer, "tracer");
        this.span = Objects.requireNonNull(span, "span");
        this.capture = Objects.requireNonNull(capture, "capture");
    }

    boolean isDownstreamSubscribed() {
        return downstreamSubscribed;
    }

    int state() {
        return state.get();
    }

    @Override
    public reactor.util.context.Context currentContext() {
        return actual.currentContext();
    }

    @Override
    public void onSubscribe(Subscription s) {
        if (this.upstream != null) {
            s.cancel();
            return;
        }
        this.upstream = s;
        downstreamSubscribed = true;
        actual.onSubscribe(this);
    }

    @Override
    public void request(long n) {
        Subscription s = upstream;
        if (s == null || state.get() != OPEN) {
            return;
        }
        try (Scope ignored = Context.current().with(span).makeCurrent()) {
            s.request(n);
        } catch (RuntimeException e) {
            if (state.compareAndSet(OPEN, ERROR)) {
                try {
                    tracer.setError(span, e);
                } finally {
                    span.end();
                }
                try {
                    s.cancel();
                } finally {
                    actual.onError(e);
                }
            } else {
                Operators.onErrorDropped(e, actual.currentContext());
            }
        }
    }

    @Override
    public void cancel() {
        if (!state.compareAndSet(OPEN, CANCEL)) {
            return;
        }
        try {
            Subscription s = upstream;
            if (s != null) {
                try (Scope ignored = Context.current().with(span).makeCurrent()) {
                    s.cancel();
                }
            }
        } finally {
            try {
                span.setStatus(StatusCode.ERROR, CANCELLED_DESCRIPTION);
            } finally {
                span.end();
            }
        }
    }

    @Override
    public void onNext(ChatResponse response) {
        if (state.get() != OPEN) {
            Operators.onNextDropped(response, actual.currentContext());
            return;
        }
        if (response != null) {
            capture.accept(response);
        }
        actual.onNext(response);
    }

    @Override
    public void onError(Throwable t) {
        if (!state.compareAndSet(OPEN, ERROR)) {
            Operators.onErrorDropped(t, actual.currentContext());
            return;
        }
        try {
            tracer.setError(span, t);
        } finally {
            span.end();
        }
        actual.onError(t);
    }

    @Override
    public void onComplete() {
        if (!state.compareAndSet(OPEN, COMPLETE)) {
            return;
        }
        try {
            capture.complete(span);
            span.setStatus(StatusCode.OK);
        } finally {
            span.end();
        }
        actual.onComplete();
    }

    /**
     * Terminates a subscription whose upstream never delivered {@code onSubscribe} (creation/subscribe failure).
     * Records the error, ends the span once and signals the downstream subscriber.
     */
    void failBeforeSubscription(Throwable error) {
        if (!state.compareAndSet(OPEN, ERROR)) {
            Operators.onErrorDropped(error, actual.currentContext());
            return;
        }
        try {
            tracer.setError(span, error);
        } finally {
            span.end();
        }
        if (downstreamSubscribed) {
            actual.onError(error);
        } else {
            Operators.error(actual, error);
        }
    }
}
