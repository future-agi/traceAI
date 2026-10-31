package ai.traceai.qdrant;

import ai.traceai.FISpanKind;
import ai.traceai.FITracer;
import ai.traceai.SemanticConventions;
import ai.traceai.TraceAI;
import com.google.common.util.concurrent.ForwardingListenableFuture;
import com.google.common.util.concurrent.SettableFuture;
import com.google.common.util.concurrent.FutureCallback;
import com.google.common.util.concurrent.Futures;
import com.google.common.util.concurrent.ListenableFuture;
import com.google.common.util.concurrent.MoreExecutors;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.context.Context;
import io.opentelemetry.context.Scope;
import io.qdrant.client.QdrantClient;
import io.qdrant.client.grpc.Collections.CollectionOperationResponse;
import io.qdrant.client.grpc.Collections.Distance;
import io.qdrant.client.grpc.Collections.VectorParams;
import io.qdrant.client.grpc.Common.Filter;
import io.qdrant.client.grpc.Common.PointId;
import io.qdrant.client.grpc.Points.BatchResult;
import io.qdrant.client.grpc.Points.PointGroup;
import io.qdrant.client.grpc.Points.PointStruct;
import io.qdrant.client.grpc.Points.QueryPointGroups;
import io.qdrant.client.grpc.Points.QueryPoints;
import io.qdrant.client.grpc.Points.ReadConsistency;
import io.qdrant.client.grpc.Points.RetrievedPoint;
import io.qdrant.client.grpc.Points.ScoredPoint;
import io.qdrant.client.grpc.Points.SearchPoints;
import io.qdrant.client.grpc.Points.UpdateResult;
import io.qdrant.client.grpc.Points.Vectors;

import java.time.Duration;
import java.util.List;
import java.util.concurrent.ExecutionException;
import java.util.function.BiConsumer;
import java.util.function.Consumer;
import java.util.function.Supplier;

/**
 * Semantic tracing for Qdrant search, universal queries (including batch and groups),
 * upsert, delete, retrieval, collection creation and collection listing.
 *
 * <p>Async methods preserve the SDK's result, failure cause and cancellation behavior.
 * Spans finish on SDK completion, before the returned future exposes the outcome.
 * The calling context is used as the parent and the operation span is current when
 * invoking the SDK. No scope is held open while waiting. As with the SDK itself,
 * applications must propagate context when scheduling their own future listeners.</p>
 *
 * <p>The existing blocking methods remain available and delegate to the async methods.
 * Operations performed via {@link #unwrap()} are not traced by this wrapper.</p>
 */
public class TracedQdrantClient {
    private final QdrantClient client;
    private final FITracer tracer;

    public TracedQdrantClient(QdrantClient client, FITracer tracer) {
        this.client = client;
        this.tracer = tracer;
    }

    public TracedQdrantClient(QdrantClient client) {
        this(client, TraceAI.getTracer());
    }

    public List<ScoredPoint> search(String collectionName, List<Float> queryVector, int limit)
            throws ExecutionException, InterruptedException {
        return searchAsync(collectionName, queryVector, limit).get();
    }

    public List<ScoredPoint> search(SearchPoints request)
            throws ExecutionException, InterruptedException {
        return searchAsync(request).get();
    }

    public ListenableFuture<List<ScoredPoint>> searchAsync(
            String collectionName, List<Float> queryVector, int limit) {
        return searchAsync(SearchPoints.newBuilder().setCollectionName(collectionName)
                .addAllVector(queryVector).setLimit(limit).build());
    }

    public ListenableFuture<List<ScoredPoint>> searchAsync(SearchPoints request) {
        return searchAsync(request, null);
    }

    public ListenableFuture<List<ScoredPoint>> searchAsync(SearchPoints request, Duration timeout) {
        return traceAsync("Search", FISpanKind.RETRIEVER, request.getCollectionName(), span -> {
            span.setAttribute(SemanticConventions.RETRIEVER_TOP_K, request.getLimit());
            if (request.getVectorCount() > 0) {
                span.setAttribute(SemanticConventions.EMBEDDING_DIMENSIONS, (long) request.getVectorCount());
            }
            if (request.hasFilter()) {
                span.setAttribute("qdrant.has_filter", true);
            }
        }, () -> client.searchAsync(request, timeout), this::recordScoredPoints);
    }

    /** Universal query, including hybrid and multi-stage requests. */
    public List<ScoredPoint> query(QueryPoints request) throws ExecutionException, InterruptedException {
        return queryAsync(request).get();
    }

    public ListenableFuture<List<ScoredPoint>> queryAsync(QueryPoints request) {
        return queryAsync(request, null);
    }

    /** Passes the request and optional SDK timeout through unchanged. */
    public ListenableFuture<List<ScoredPoint>> queryAsync(QueryPoints request, Duration timeout) {
        return traceAsync("Query", FISpanKind.RETRIEVER, request.getCollectionName(), span -> {
            if (request.hasLimit()) {
                span.setAttribute(SemanticConventions.RETRIEVER_TOP_K, request.getLimit());
            }
            if (request.hasFilter()) {
                span.setAttribute("qdrant.has_filter", true);
            }
            span.setAttribute("qdrant.prefetch_count", (long) request.getPrefetchCount());
        }, () -> client.queryAsync(request, timeout), this::recordScoredPoints);
    }

    public List<BatchResult> queryBatch(String collectionName, List<QueryPoints> queries)
            throws ExecutionException, InterruptedException {
        return queryBatchAsync(collectionName, queries).get();
    }

    public ListenableFuture<List<BatchResult>> queryBatchAsync(
            String collectionName, List<QueryPoints> queries) {
        return queryBatchAsync(collectionName, queries, null, null);
    }

    public ListenableFuture<List<BatchResult>> queryBatchAsync(
            String collectionName, List<QueryPoints> queries, ReadConsistency readConsistency) {
        return queryBatchAsync(collectionName, queries, readConsistency, null);
    }

    public ListenableFuture<List<BatchResult>> queryBatchAsync(
            String collectionName, List<QueryPoints> queries,
            ReadConsistency readConsistency, Duration timeout) {
        return traceAsync("Query Batch", FISpanKind.RETRIEVER, collectionName,
                span -> span.setAttribute("qdrant.query_count", (long) queries.size()),
                () -> client.queryBatchAsync(collectionName, queries, readConsistency, timeout),
                (span, results) -> {
                    span.setAttribute("qdrant.batch_count", (long) results.size());
                    span.setAttribute(SemanticConventions.DB_VECTOR_RESULTS_COUNT,
                            results.stream().mapToLong(BatchResult::getResultCount).sum());
                });
    }

    public List<PointGroup> queryGroups(QueryPointGroups request)
            throws ExecutionException, InterruptedException {
        return queryGroupsAsync(request).get();
    }

    public ListenableFuture<List<PointGroup>> queryGroupsAsync(QueryPointGroups request) {
        return queryGroupsAsync(request, null);
    }

    public ListenableFuture<List<PointGroup>> queryGroupsAsync(QueryPointGroups request, Duration timeout) {
        return traceAsync("Query Groups", FISpanKind.RETRIEVER, request.getCollectionName(), span -> {
            if (request.hasLimit()) {
                span.setAttribute(SemanticConventions.RETRIEVER_TOP_K, request.getLimit());
            }
            if (request.hasFilter()) {
                span.setAttribute("qdrant.has_filter", true);
            }
        }, () -> client.queryGroupsAsync(request, timeout), (span, groups) -> {
            span.setAttribute("qdrant.groups_count", (long) groups.size());
            span.setAttribute(SemanticConventions.DB_VECTOR_RESULTS_COUNT,
                    groups.stream().mapToLong(PointGroup::getHitsCount).sum());
        });
    }

    public UpdateResult upsert(String collectionName, List<PointStruct> points)
            throws ExecutionException, InterruptedException {
        return upsertAsync(collectionName, points).get();
    }

    public ListenableFuture<UpdateResult> upsertAsync(String collectionName, List<PointStruct> points) {
        return traceAsync("Upsert", FISpanKind.VECTOR_DB, collectionName, span -> {
            span.setAttribute(SemanticConventions.DB_VECTOR_UPSERT_COUNT, (long) points.size());
            if (!points.isEmpty() && points.get(0).hasVectors()) {
                Vectors vectors = points.get(0).getVectors();
                if (vectors.hasVector()) {
                    span.setAttribute(SemanticConventions.EMBEDDING_DIMENSIONS,
                            (long) vectors.getVector().getDataCount());
                }
            }
        }, () -> client.upsertAsync(collectionName, points), this::recordUpdate);
    }

    public UpdateResult delete(String collectionName, Filter filter)
            throws ExecutionException, InterruptedException {
        return deleteAsync(collectionName, filter).get();
    }

    public ListenableFuture<UpdateResult> deleteAsync(String collectionName, Filter filter) {
        return traceAsync("Delete", FISpanKind.VECTOR_DB, collectionName,
                span -> span.setAttribute("qdrant.has_filter", true),
                () -> client.deleteAsync(collectionName, filter), this::recordUpdate);
    }

    public List<RetrievedPoint> get(String collectionName, List<PointId> pointIds,
                                    boolean withPayload, boolean withVectors)
            throws ExecutionException, InterruptedException {
        return getAsync(collectionName, pointIds, withPayload, withVectors).get();
    }

    public ListenableFuture<List<RetrievedPoint>> getAsync(String collectionName, List<PointId> pointIds,
                                                           boolean withPayload, boolean withVectors) {
        return traceAsync("Get", FISpanKind.VECTOR_DB, collectionName,
                span -> span.setAttribute("qdrant.requested_count", (long) pointIds.size()),
                () -> client.retrieveAsync(collectionName, pointIds, withPayload, withVectors, null),
                (span, results) -> span.setAttribute("qdrant.retrieved_count", (long) results.size()));
    }

    public void createCollection(String collectionName, int vectorSize, Distance distance)
            throws ExecutionException, InterruptedException {
        createCollectionAsync(collectionName, vectorSize, distance).get();
    }

    public ListenableFuture<CollectionOperationResponse> createCollectionAsync(
            String collectionName, int vectorSize, Distance distance) {
        return traceAsync("Create Collection", FISpanKind.VECTOR_DB, collectionName, span -> {
            span.setAttribute("qdrant.vector_size", (long) vectorSize);
            span.setAttribute("qdrant.distance", distance.name());
        }, () -> client.createCollectionAsync(collectionName,
                VectorParams.newBuilder().setSize(vectorSize).setDistance(distance).build()),
                (span, result) -> { });
    }

    public List<String> listCollections() throws ExecutionException, InterruptedException {
        return listCollectionsAsync().get();
    }

    public ListenableFuture<List<String>> listCollectionsAsync() {
        return traceAsync("List Collections", FISpanKind.VECTOR_DB, null, span -> { },
                client::listCollectionsAsync,
                (span, results) -> span.setAttribute("qdrant.collections_count", (long) results.size()));
    }

    private void recordScoredPoints(Span span, List<ScoredPoint> results) {
        span.setAttribute(SemanticConventions.DB_VECTOR_RESULTS_COUNT, (long) results.size());
        if (!results.isEmpty()) {
            span.setAttribute("qdrant.top_score", results.get(0).getScore());
        }
    }

    private void recordUpdate(Span span, UpdateResult result) {
        span.setAttribute("qdrant.status", result.getStatus().name());
    }

    private <T> ListenableFuture<T> traceAsync(String operation, FISpanKind kind, String collection,
            Consumer<Span> attributes, Supplier<ListenableFuture<T>> call, BiConsumer<Span, T> resultAttributes) {
        Span span = tracer.startSpan("Qdrant " + operation, kind);
        Context context = Context.current().with(span);
        ListenableFuture<T> future;
        try (Scope scope = context.makeCurrent()) {
            span.setAttribute(SemanticConventions.DB_SYSTEM, "qdrant");
            if (collection != null) {
                span.setAttribute(SemanticConventions.DB_VECTOR_COLLECTION_NAME, collection);
            }
            attributes.accept(span);
            future = call.get();
        } catch (RuntimeException | Error failure) {
            try {
                tracer.setError(span, failure);
            } finally {
                span.end();
            }
            throw failure;
        }
        // Publish the outcome only after ending the span. Returning the SDK future
        // directly lets get() race its completion callback and an immediate tracer shutdown.
        SettableFuture<T> completion = SettableFuture.create();
        Futures.addCallback(future, new FutureCallback<T>() {
            @Override
            public void onSuccess(T result) {
                finish(span, context, () -> {
                    resultAttributes.accept(span, result);
                    span.setStatus(StatusCode.OK);
                });
                completion.set(result);
            }

            @Override
            public void onFailure(Throwable failure) {
                finish(span, context, () -> tracer.setError(span, failure));
                if (future.isCancelled()) {
                    completion.cancel(false);
                } else {
                    completion.setException(failure);
                }
            }
        }, MoreExecutors.directExecutor());
        return new ForwardingListenableFuture.SimpleForwardingListenableFuture<T>(completion) {
            @Override
            public boolean cancel(boolean mayInterruptIfRunning) {
                // Preserve whether the SDK accepts cancellation, including the interrupt
                // flag. Its completion callback transfers cancellation after ending the span.
                return future.cancel(mayInterruptIfRunning);
            }
        };
    }

    private void finish(Span span, Context context, Runnable attributes) {
        try {
            try (Scope scope = context.makeCurrent()) {
                attributes.run();
            } finally {
                span.end();
            }
        } catch (RuntimeException ignored) {
            // Optional telemetry must not change or prevent delivery of the SDK outcome.
        }
    }

    /** Returns the original client; operations on it bypass this wrapper. */
    public QdrantClient unwrap() {
        return client;
    }
}
