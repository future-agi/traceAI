package ai.traceai.azure.openai;

import com.azure.core.util.IterableStream;
import io.opentelemetry.context.Scope;

import java.util.Collections;
import java.util.Iterator;
import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;
import java.util.function.Consumer;
import java.util.stream.Stream;
import java.util.stream.StreamSupport;

/**
 * An SDK-compatible incremental result with explicit consumer cancellation. Consume to
 * exhaustion, or close this result (or its Java stream) on early exit to finish tracing.
 * Merely abandoning an iterator cannot signal cancellation. The beta.16 SDK caches its
 * SSE publisher: cancelling a consumer does not cancel its underlying HTTP body.
 * This wrapper preserves that SDK behavior; it does not add another cache or drain.
 */
public final class TracedAzureOpenAIStream<T> extends IterableStream<T> implements AutoCloseable {
    private final IterableStream<T> delegate;
    private final AzureOpenAITrace trace;
    private final Consumer<T> chunk;
    private final Set<Stream<T>> active = ConcurrentHashMap.newKeySet();
    private final java.util.concurrent.atomic.AtomicBoolean closed = new java.util.concurrent.atomic.AtomicBoolean();

    TracedAzureOpenAIStream(IterableStream<T> delegate, AzureOpenAITrace trace, Consumer<T> chunk) {
        super(Collections.emptyList());
        this.delegate = delegate;
        this.trace = trace;
        this.chunk = chunk;
    }

    @Override
    public synchronized Stream<T> stream() {
        if (closed.get()) throw new IllegalStateException("Stream has been closed");
        try (Scope ignored = trace.span.makeCurrent()) {
            Stream<T> source = delegate.stream();
            active.add(source);
            Iterator<T> iterator = source.iterator();
            Iterator<T> observed = new Iterator<>() {
                @Override public boolean hasNext() {
                    try (Scope scope = trace.span.makeCurrent()) {
                        boolean next = iterator.hasNext();
                        if (!next) {
                            source.close();
                            active.remove(source);
                            trace.end(null, false);
                        }
                        return next;
                    } catch (RuntimeException | Error error) {
                        trace.end(error, false);
                        throw error;
                    }
                }
                @Override public T next() {
                    try (Scope scope = trace.span.makeCurrent()) {
                        T value = iterator.next();
                        trace.capture(() -> chunk.accept(value));
                        return value;
                    } catch (RuntimeException | Error error) {
                        trace.end(error, false);
                        throw error;
                    }
                }
            };
            Iterable<T> iterable = () -> observed;
            return StreamSupport.stream(iterable.spliterator(), false).onClose(() -> {
                try { source.close(); }
                finally {
                    active.remove(source);
                    trace.end(null, true);
                }
            });
        } catch (RuntimeException | Error error) {
            trace.end(error, false);
            throw error;
        }
    }

    @Override public Iterator<T> iterator() { return stream().iterator(); }

    @Override
    public synchronized void close() {
        if (!closed.compareAndSet(false, true)) return;
        try {
            // Do not subscribe an unused SDK stream merely to close it: beta.16 caches
            // upstream, so doing so would start consuming a body that cannot be cancelled.
            for (Stream<T> stream : active) stream.close();
            active.clear();
        } finally { trace.end(null, true); }
    }
}
