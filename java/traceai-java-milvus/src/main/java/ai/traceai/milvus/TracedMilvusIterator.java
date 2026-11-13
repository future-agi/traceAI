package ai.traceai.milvus;

import ai.traceai.FISpanKind;
import ai.traceai.FITracer;
import ai.traceai.SemanticConventions;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.context.Context;
import io.opentelemetry.context.Scope;

import java.util.List;
import java.util.function.Supplier;

/**
 * Lazy, closeable Milvus batch iterator. Each next/close call gets a short span
 * parented to the context captured at creation, even when consumed on another thread.
 * No rows are copied, retained or exported. There is no lifetime span to leak if
 * iteration is abandoned. Close explicitly (or use try-with-resources) to release
 * the SDK's iterator resources. Thread safety is the same as the underlying SDK.
 *
 * @param <T> the SDK row type
 */
public final class TracedMilvusIterator<T> implements AutoCloseable {
    private final FITracer tracer;
    private final String name;
    private final String operation;
    private final String collection;
    private final Context parent;
    private final Supplier<List<T>> next;
    private final Runnable close;

    TracedMilvusIterator(FITracer tracer, String name, String operation, String collection,
                        Context parent, Supplier<List<T>> next, Runnable close) {
        this.tracer = tracer;
        this.name = name;
        this.operation = operation;
        this.collection = collection;
        this.parent = parent;
        this.next = next;
        this.close = close;
    }

    /** @return the SDK's next batch, unchanged; an empty batch signals exhaustion */
    public List<T> next() {
        return trace("next", next);
    }

    /** Closes the underlying iterator; SDK exceptions are propagated unchanged. */
    @Override
    public void close() {
        trace("close", () -> {
            close.run();
            return null;
        });
    }

    private List<T> trace(String action, Supplier<List<T>> call) {
        Span span = tracer.startSpan("Milvus " + name + " " + action, FISpanKind.RETRIEVER, parent);
        try (Scope ignored = span.makeCurrent()) {
            span.setAttribute(SemanticConventions.DB_SYSTEM, "milvus");
            span.setAttribute(SemanticConventions.DB_OPERATION_NAME, operation + "." + action);
            span.setAttribute(SemanticConventions.DB_VECTOR_COLLECTION_NAME, collection);
            List<T> batch = call.get();
            if (batch != null) {
                span.setAttribute(SemanticConventions.DB_VECTOR_RESULTS_COUNT, (long) batch.size());
            }
            span.setStatus(StatusCode.OK);
            return batch;
        } catch (RuntimeException | Error failure) {
            tracer.setError(span, failure);
            throw failure;
        } finally {
            span.end();
        }
    }
}
