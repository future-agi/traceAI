package ai.traceai.cohere;

import ai.traceai.FITracer;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.context.Scope;

import java.io.Closeable;
import java.io.IOException;
import java.io.UncheckedIOException;
import java.util.Iterator;
import java.util.NoSuchElementException;
import java.util.function.Function;

/**
 * Single-use, closeable Cohere event stream. Use try-with-resources, especially when
 * stopping iteration early. The original SDK events are returned unchanged.
 * The span is current only while the SDK is opening or reading the stream, not
 * while application code processes an event. Like the SDK stream, this is not thread-safe.
 *
 * @param <T> the SDK event type
 */
public final class TracedCohereStream<T> implements Iterable<T>, Closeable {
    enum EventState { CONTINUE, COMPLETE, ERROR }

    private final Iterable<T> source;
    private final FITracer tracer;
    private final Span span;
    private final Function<T, EventState> observer;
    private final Runnable captureOutput;
    private boolean iterated;
    private boolean ended;
    private boolean closed;

    TracedCohereStream(Iterable<T> source, FITracer tracer, Span span,
                      Function<T, EventState> observer, Runnable captureOutput) {
        this.source = source;
        this.tracer = tracer;
        this.span = span;
        this.observer = observer;
        this.captureOutput = captureOutput;
    }

    @Override
    public Iterator<T> iterator() {
        if (iterated) throw new IllegalStateException("Cohere streams can only be iterated once");
        iterated = true;
        if (closed) return java.util.Collections.emptyIterator();
        final Iterator<T> delegate;
        try (Scope ignored = span.makeCurrent()) {
            delegate = source.iterator();
        } catch (RuntimeException | Error error) {
            fail(error);
            throw error;
        }
        return new Iterator<>() {
            @Override
            public boolean hasNext() {
                if (closed) return false;
                try (Scope ignored = span.makeCurrent()) {
                    if (delegate.hasNext()) return true;
                    // EOF without the provider's terminal event is incomplete, not success.
                    close();
                    return false;
                } catch (IOException error) {
                    throw new UncheckedIOException(error);
                } catch (RuntimeException | Error error) {
                    fail(error);
                    throw error;
                }
            }

            @Override
            public T next() {
                if (!hasNext()) throw new NoSuchElementException();
                try (Scope ignored = span.makeCurrent()) {
                    T event = delegate.next();
                    EventState state = observer.apply(event);
                    if (state != EventState.CONTINUE) {
                        closeSource();
                        finish(state == EventState.COMPLETE ? "completed" : "error", null);
                    }
                    return event;
                } catch (IOException error) {
                    fail(error);
                    throw new UncheckedIOException(error);
                } catch (RuntimeException | Error error) {
                    fail(error);
                    throw error;
                }
            }
        };
    }

    @Override
    public void close() throws IOException {
        if (closed) return;
        try (Scope ignored = span.makeCurrent()) {
            closeSource();
            finish("incomplete", null);
        } catch (IOException | RuntimeException | Error error) {
            finish("error", error);
            throw error;
        }
    }

    private void closeSource() throws IOException {
        if (closed) return;
        closed = true;
        if (source instanceof Closeable) {
            ((Closeable) source).close();
        } else if (source instanceof AutoCloseable) {
            try {
                ((AutoCloseable) source).close();
            } catch (IOException | RuntimeException error) {
                throw error;
            } catch (Exception error) {
                throw new IOException(error);
            }
        }
    }

    private void fail(Throwable error) {
        try {
            closeSource();
        } catch (IOException | RuntimeException | Error closeError) {
            if (closeError != error) error.addSuppressed(closeError);
        } finally {
            finish("error", error);
        }
    }

    private void finish(String outcome, Throwable error) {
        if (ended) return;
        ended = true;
        try {
            captureOutput.run();
            span.setAttribute("cohere.stream.outcome", outcome);
            if (error != null) tracer.setError(span, error);
            else if ("completed".equals(outcome)) span.setStatus(StatusCode.OK);
        } finally {
            span.end();
        }
    }
}
