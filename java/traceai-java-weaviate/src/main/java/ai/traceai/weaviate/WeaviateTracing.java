package ai.traceai.weaviate;

import ai.traceai.FISpanKind;
import ai.traceai.FITracer;
import ai.traceai.SemanticConventions;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.context.Scope;
import io.weaviate.client6.v1.api.collections.data.DeleteManyResponse;
import io.weaviate.client6.v1.api.collections.data.InsertManyResponse;
import io.weaviate.client6.v1.api.collections.query.QueryResponse;
import io.weaviate.client6.v1.api.collections.query.QueryResponseGrouped;

import java.io.IOException;
import java.util.Objects;
import java.util.Optional;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.CompletionException;
import java.util.function.Supplier;

/** Shared lifecycle for the v6 adapters. Never serializes queries, vectors or documents. */
final class WeaviateTracing {
    private static final org.slf4j.Logger LOGGER = org.slf4j.LoggerFactory.getLogger(WeaviateTracing.class);
    private final String collectionName;
    private final FITracer tracer;

    WeaviateTracing(String collectionName, FITracer tracer) {
        this.collectionName = Objects.requireNonNull(collectionName, "collectionName");
        this.tracer = Objects.requireNonNull(tracer, "tracer");
    }

    private Span start(String operation, FISpanKind kind, Integer limit) {
        Span span;
        try {
            span = tracer.startSpan("Weaviate " + operation, kind);
        } catch (RuntimeException error) {
            warn(error);
            return Span.wrap(Span.current().getSpanContext());
        }
        record(() -> {
            span.setAttribute(SemanticConventions.DB_SYSTEM, "weaviate");
            span.setAttribute(SemanticConventions.DB_OPERATION_NAME, operation);
            span.setAttribute(SemanticConventions.DB_VECTOR_COLLECTION_NAME, collectionName);
            if (limit != null) {
                span.setAttribute(SemanticConventions.RETRIEVER_TOP_K, limit.longValue());
            }
        });
        return span;
    }

    <T> T call(String operation, FISpanKind kind, Integer limit, Supplier<T> action) {
        Span span = start(operation, kind, limit);
        try (Scope ignored = span.makeCurrent()) {
            T result = action.get();
            finish(span, result);
            return result;
        } catch (RuntimeException | Error error) {
            record(() -> tracer.setError(span, error));
            throw error;
        } finally {
            record(span::end);
        }
    }

    @FunctionalInterface
    interface IOAction<T> {
        T get() throws IOException;
    }

    <T> T callIO(String operation, IOAction<T> action) throws IOException {
        Span span = start(operation, FISpanKind.VECTOR_DB, null);
        try (Scope ignored = span.makeCurrent()) {
            T result = action.get();
            finish(span, result);
            return result;
        } catch (IOException | RuntimeException | Error error) {
            record(() -> tracer.setError(span, error));
            throw error;
        } finally {
            record(span::end);
        }
    }

    <T> CompletableFuture<T> callAsync(String operation, FISpanKind kind, Integer limit,
                                     Supplier<CompletableFuture<T>> action) {
        Span span = start(operation, kind, limit);
        try (Scope ignored = span.makeCurrent()) {
            CompletableFuture<T> future = action.get();
            future.whenComplete((result, error) -> {
                try {
                    if (error == null) {
                        finish(span, result);
                    } else {
                        Throwable cause = error;
                        while (cause instanceof CompletionException && cause.getCause() != null) {
                            cause = cause.getCause();
                        }
                        Throwable failure = cause;
                        record(() -> tracer.setError(span, failure));
                    }
                } finally {
                    record(span::end);
                }
            });
            // Do not return the dependent future: cancellation and exception identity
            // must remain those of the SDK's original future.
            return future;
        } catch (RuntimeException | Error error) {
            record(() -> tracer.setError(span, error));
            record(span::end);
            throw error;
        }
    }

    // Telemetry is best effort; recording failures must not change SDK results or errors.
    private static void record(Runnable update) {
        try {
            update.run();
        } catch (RuntimeException error) {
            warn(error);
        }
    }

    private static void warn(RuntimeException error) {
        // Exception messages and stack traces can contain the customer payload.
        LOGGER.warn("Weaviate telemetry recording failed ({})", error.getClass().getName());
    }

    private void finish(Span span, Object result) {
        record(() -> recordResult(span, result));
    }

    private void recordResult(Span span, Object result) {
        boolean failed = false;
        if (result instanceof QueryResponse<?> query) {
            span.setAttribute(SemanticConventions.DB_VECTOR_RESULTS_COUNT, (long) query.objects().size());
        } else if (result instanceof QueryResponseGrouped<?> query) {
            span.setAttribute(SemanticConventions.DB_VECTOR_RESULTS_COUNT, (long) query.objects().size());
        } else if (result instanceof Optional<?> object) {
            span.setAttribute(SemanticConventions.DB_VECTOR_RESULTS_COUNT, object.isPresent() ? 1L : 0L);
        } else if (result instanceof InsertManyResponse batch) {
            long failures = batch.responses().stream().filter(item -> !item.successful()).count();
            span.setAttribute("weaviate.imported_count", batch.responses().size() - failures);
            span.setAttribute("weaviate.failed_count", failures);
            failed = failures > 0 || !batch.errors().isEmpty();
        } else if (result instanceof DeleteManyResponse batch) {
            span.setAttribute("weaviate.deleted_count", batch.successful());
            span.setAttribute("weaviate.failed_count", batch.failed());
            failed = batch.failed() > 0;
        }
        // Batch errors can contain customer documents; report counts/status only.
        span.setStatus(failed ? StatusCode.ERROR : StatusCode.OK);
    }
}
