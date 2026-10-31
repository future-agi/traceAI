package ai.traceai.auto;

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
    private static final ThreadLocal<Resource> END_RESOURCE = new ThreadLocal<>();
    private final SpanProcessor delegate;
    WireResourceProcessor(SpanProcessor delegate) { this.delegate = delegate; }
    static void end(Span span, long endEpochNanos, Resource resource) {
        Resource previous = END_RESOURCE.get();
        try {
            END_RESOURCE.set(resource);
            // SDK onEnd callbacks are synchronous. Snapshot the resource here, before a
            // batch processor queues the immutable view; no cross-thread side table exists.
            span.end(endEpochNanos, TimeUnit.NANOSECONDS);
        } finally { if (previous == null) END_RESOURCE.remove(); else END_RESOURCE.set(previous); }
    }
    @Override public void onStart(Context parent, ReadWriteSpan span) { delegate.onStart(parent, span); }
    @Override public boolean isStartRequired() { return delegate.isStartRequired(); }
    @Override public boolean isEndRequired() { return delegate.isEndRequired(); }
    @Override public void onEnd(ReadableSpan span) {
        Resource resource = END_RESOURCE.get();
        if (resource == null) { delegate.onEnd(span); return; }
        SpanData snapshot = new DelegatingSpanData(span.toSpanData()) {
            @Override public Resource getResource() { return resource; }
        };
        ReadableSpan view = (ReadableSpan)Proxy.newProxyInstance(WireResourceProcessor.class.getClassLoader(), new Class<?>[]{ReadableSpan.class}, (proxy, method, args) -> {
            if (method.getName().equals("toSpanData")) return snapshot;
            try { return method.invoke(span, args); }
            catch (InvocationTargetException error) { throw error.getCause(); }
        });
        delegate.onEnd(view);
    }
    @Override public CompletableResultCode forceFlush() { return delegate.forceFlush(); }
    @Override public CompletableResultCode shutdown() { return delegate.shutdown(); }
}
