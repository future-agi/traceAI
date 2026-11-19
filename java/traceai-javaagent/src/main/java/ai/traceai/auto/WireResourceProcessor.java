package ai.traceai.auto;

import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.api.common.Attributes;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.context.Context;
import io.opentelemetry.sdk.common.CompletableResultCode;
import io.opentelemetry.sdk.resources.Resource;
import io.opentelemetry.sdk.trace.*;
import io.opentelemetry.sdk.trace.data.*;
import java.lang.reflect.*;
import java.util.concurrent.TimeUnit;

/** Only the owned wire provider uses this adapter; application SDK processors are unchanged. */
final class WireResourceProcessor implements SpanProcessor {
    private record EndState(Resource resource, boolean omitAttributes) {}
    private static final ThreadLocal<EndState> END_STATE = new ThreadLocal<>();
    private final SpanProcessor delegate;
    WireResourceProcessor(SpanProcessor delegate) { this.delegate = delegate; }
    static void end(Span span, long endEpochNanos, Resource resource, int attributeLimit) {
        EndState previous = END_STATE.get();
        try {
            END_STATE.set(new EndState(resource, attributeLimit == 0));
            // SDK onEnd callbacks are synchronous. Snapshot the resource and zero budget
            // before a batch processor queues the immutable view; no cross-thread side table exists.
            span.end(endEpochNanos, TimeUnit.NANOSECONDS);
        } finally { if (previous == null) END_STATE.remove(); else END_STATE.set(previous); }
    }
    @Override public void onStart(Context parent, ReadWriteSpan span) { delegate.onStart(parent, span); }
    @Override public boolean isStartRequired() { return delegate.isStartRequired(); }
    @Override public boolean isEndRequired() { return delegate.isEndRequired(); }
    @Override public void onEnd(ReadableSpan span) {
        EndState state = END_STATE.get();
        if (state == null) { delegate.onEnd(span); return; }
        SpanData snapshot = new DelegatingSpanData(span.toSpanData()) {
            @Override public Resource getResource() { return state.resource(); }
            // The sampler installs metadata before projection. The SDK cannot remove it;
            // omit it from a zero-budget export without changing the decision or tracestate.
            @Override public Attributes getAttributes() { return state.omitAttributes() ? Attributes.empty() : super.getAttributes(); }
        };
        ReadableSpan view = (ReadableSpan)Proxy.newProxyInstance(WireResourceProcessor.class.getClassLoader(), new Class<?>[]{ReadableSpan.class}, (proxy, method, args) -> {
            if (method.getName().equals("toSpanData")) return snapshot;
            if (method.getName().equals("getAttribute")) return snapshot.getAttributes().get((AttributeKey<?>)args[0]);
            try { return method.invoke(span, args); }
            catch (InvocationTargetException error) { throw error.getCause(); }
        });
        delegate.onEnd(view);
    }
    @Override public CompletableResultCode forceFlush() { return delegate.forceFlush(); }
    @Override public CompletableResultCode shutdown() { return delegate.shutdown(); }
}
