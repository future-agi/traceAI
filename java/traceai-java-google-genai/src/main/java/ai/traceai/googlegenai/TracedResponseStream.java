package ai.traceai.googlegenai;

import com.google.genai.ResponseStream;
import com.google.genai.types.GenerateContentResponse;
import io.opentelemetry.context.Scope;
import java.util.Iterator;
import java.util.NoSuchElementException;
import java.util.concurrent.atomic.AtomicBoolean;

/**
 * A single-use stream that finishes its span at exhaustion, failure, or close.
 * Always use try-with-resources, including when stopping iteration early.
 */
public final class TracedResponseStream implements Iterable<GenerateContentResponse>, AutoCloseable {
    private final ResponseStream<GenerateContentResponse> delegate;
    private final GenAiOperation operation;
    private final AtomicBoolean closed = new AtomicBoolean();
    private final Iterator<GenerateContentResponse> iterator;

    TracedResponseStream(ResponseStream<GenerateContentResponse> delegate, GenAiOperation operation) {
        this.delegate = delegate;
        this.operation = operation;
        Iterator<GenerateContentResponse> source;
        try {
            source = delegate.iterator();
        } catch (RuntimeException | Error error) {
            finish(error, false);
            throw error;
        }
        iterator = new Iterator<>() {
            @Override
            public boolean hasNext() {
                if (closed.get()) return false;
                try (Scope ignored = operation.context.makeCurrent()) {
                    if (source.hasNext()) return true;
                    finish(null, false);
                    return false;
                } catch (RuntimeException | Error error) {
                    finish(error, false);
                    throw error;
                }
            }

            @Override
            public GenerateContentResponse next() {
                if (!hasNext()) throw new NoSuchElementException();
                try (Scope ignored = operation.context.makeCurrent()) {
                    GenerateContentResponse response = source.next();
                    operation.response(response, true);
                    return response;
                } catch (RuntimeException | Error error) {
                    finish(error, false);
                    throw error;
                }
            }
        };
    }

    @Override
    public Iterator<GenerateContentResponse> iterator() {
        return iterator;
    }

    @Override
    public void close() {
        finish(null, true);
    }

    private void finish(Throwable error, boolean cancelled) {
        if (!closed.compareAndSet(false, true)) return;
        Throwable failure = error;
        try {
            delegate.close();
        } catch (RuntimeException | Error closeError) {
            if (failure == null) {
                failure = closeError;
                throw closeError;
            }
            if (failure != closeError) failure.addSuppressed(closeError);
        } finally {
            operation.finish(failure, true, cancelled);
        }
    }
}
