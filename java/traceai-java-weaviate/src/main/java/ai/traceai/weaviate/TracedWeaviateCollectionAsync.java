package ai.traceai.weaviate;

import ai.traceai.FISpanKind;
import ai.traceai.FITracer;
import ai.traceai.TraceAI;
import io.weaviate.client6.v1.api.WeaviateClientAsync;
import io.weaviate.client6.v1.api.collections.CollectionHandleAsync;
import io.weaviate.client6.v1.api.collections.WeaviateObject;
import io.weaviate.client6.v1.api.collections.data.DeleteManyRequest;
import io.weaviate.client6.v1.api.collections.data.DeleteManyResponse;
import io.weaviate.client6.v1.api.collections.data.InsertManyRequest;
import io.weaviate.client6.v1.api.collections.data.InsertManyResponse;
import io.weaviate.client6.v1.api.collections.data.InsertObjectRequest;
import io.weaviate.client6.v1.api.collections.data.UpdateObjectRequest;
import io.weaviate.client6.v1.api.collections.data.ReplaceObjectRequest;
import io.weaviate.client6.v1.api.collections.query.FetchObjectById;
import io.weaviate.client6.v1.api.collections.query.GroupBy;
import io.weaviate.client6.v1.api.collections.query.QueryResponse;
import io.weaviate.client6.v1.api.collections.query.QueryResponseGrouped;
import io.weaviate.client6.v1.api.collections.query.FetchObjects;
import io.weaviate.client6.v1.api.collections.query.NearVector;
import io.weaviate.client6.v1.api.collections.query.NearText;
import io.weaviate.client6.v1.api.collections.query.NearObject;
import io.weaviate.client6.v1.api.collections.query.Bm25;
import io.weaviate.client6.v1.api.collections.query.Hybrid;
import io.weaviate.client6.v1.internal.ObjectBuilder;

import java.util.Objects;
import java.util.Optional;
import java.util.function.Function;
import java.util.concurrent.CompletableFuture;

/**
 * Explicit asynchronous tracing for Weaviate client6 collection queries and object writes.
 * Accepts SDK request objects unchanged, including filters, named vectors and return options.
 * Only calls through this adapter are traced; admin, generation and streaming batch APIs
 * remain accessible on the caller-owned client and are not instrumented here.
 * The existing {@link TracedWeaviateClient} remains the legacy v5 entry point.
 *
 * @param <P> the SDK collection's property type
 */
public final class TracedWeaviateCollectionAsync<P> {
    private final CollectionHandleAsync<P> collection;
    private final WeaviateTracing tracing;

    /**
     * Wrap a caller-owned handle, retaining its tenant and consistency defaults.
     * @param collectionName collection name to record in telemetry
     * @param collection configured SDK handle; this adapter never closes its client
     * @param tracer tracer to use for semantic spans
     */
    public TracedWeaviateCollectionAsync(String collectionName, CollectionHandleAsync<P> collection, FITracer tracer) {
        this.collection = Objects.requireNonNull(collection, "collection");
        this.tracing = new WeaviateTracing(collectionName, tracer);
    }

    /** Wrap a handle using the global TraceAI tracer. */
    public TracedWeaviateCollectionAsync(String collectionName, CollectionHandleAsync<P> collection) {
        this(collectionName, collection, TraceAI.getTracer());
    }

    /** Obtain an untyped collection from an existing client, without owning its lifecycle. */
    public static TracedWeaviateCollectionAsync<java.util.Map<String, Object>> of(
            WeaviateClientAsync client, String collectionName, FITracer tracer) {
        return new TracedWeaviateCollectionAsync<>(collectionName, client.collections.use(collectionName), tracer);
    }

    /** Return the original handle. Calls on it are not traced by this adapter. */
    public CollectionHandleAsync<P> unwrap() {
        return collection;
    }

    /** Trace the SDK's fetchObjects query without rebuilding its options. */
    public CompletableFuture<QueryResponse<P>> fetchObjects(FetchObjects request) {
        return tracing.callAsync("fetchObjects", FISpanKind.RETRIEVER, request.common().limit(), () -> collection.query.fetchObjects(request));
    }

    /** Trace the SDK's fetchObjects grouped query without rebuilding its options. */
    public CompletableFuture<QueryResponseGrouped<P>> fetchObjects(FetchObjects request, GroupBy groupBy) {
        return tracing.callAsync("fetchObjects", FISpanKind.RETRIEVER, request.common().limit(), () -> collection.query.fetchObjects(request, groupBy));
    }

    /** Trace the SDK's nearVector query without rebuilding its options. */
    public CompletableFuture<QueryResponse<P>> nearVector(NearVector request) {
        return tracing.callAsync("nearVector", FISpanKind.RETRIEVER, request.common().limit(), () -> collection.query.nearVector(request));
    }

    /** Trace the SDK's nearVector grouped query without rebuilding its options. */
    public CompletableFuture<QueryResponseGrouped<P>> nearVector(NearVector request, GroupBy groupBy) {
        return tracing.callAsync("nearVector", FISpanKind.RETRIEVER, request.common().limit(), () -> collection.query.nearVector(request, groupBy));
    }

    /** Trace the SDK's nearText query without rebuilding its options. */
    public CompletableFuture<QueryResponse<P>> nearText(NearText request) {
        return tracing.callAsync("nearText", FISpanKind.RETRIEVER, request.common().limit(), () -> collection.query.nearText(request));
    }

    /** Trace the SDK's nearText grouped query without rebuilding its options. */
    public CompletableFuture<QueryResponseGrouped<P>> nearText(NearText request, GroupBy groupBy) {
        return tracing.callAsync("nearText", FISpanKind.RETRIEVER, request.common().limit(), () -> collection.query.nearText(request, groupBy));
    }

    /** Trace the SDK's nearObject query without rebuilding its options. */
    public CompletableFuture<QueryResponse<P>> nearObject(NearObject request) {
        return tracing.callAsync("nearObject", FISpanKind.RETRIEVER, request.common().limit(), () -> collection.query.nearObject(request));
    }

    /** Trace the SDK's nearObject grouped query without rebuilding its options. */
    public CompletableFuture<QueryResponseGrouped<P>> nearObject(NearObject request, GroupBy groupBy) {
        return tracing.callAsync("nearObject", FISpanKind.RETRIEVER, request.common().limit(), () -> collection.query.nearObject(request, groupBy));
    }

    /** Trace the SDK's bm25 query without rebuilding its options. */
    public CompletableFuture<QueryResponse<P>> bm25(Bm25 request) {
        return tracing.callAsync("bm25", FISpanKind.RETRIEVER, request.common().limit(), () -> collection.query.bm25(request));
    }

    /** Trace the SDK's bm25 grouped query without rebuilding its options. */
    public CompletableFuture<QueryResponseGrouped<P>> bm25(Bm25 request, GroupBy groupBy) {
        return tracing.callAsync("bm25", FISpanKind.RETRIEVER, request.common().limit(), () -> collection.query.bm25(request, groupBy));
    }

    /** Trace the SDK's hybrid query without rebuilding its options. */
    public CompletableFuture<QueryResponse<P>> hybrid(Hybrid request) {
        return tracing.callAsync("hybrid", FISpanKind.RETRIEVER, request.common().limit(), () -> collection.query.hybrid(request));
    }

    /** Trace the SDK's hybrid grouped query without rebuilding its options. */
    public CompletableFuture<QueryResponseGrouped<P>> hybrid(Hybrid request, GroupBy groupBy) {
        return tracing.callAsync("hybrid", FISpanKind.RETRIEVER, request.common().limit(), () -> collection.query.hybrid(request, groupBy));
    }

    /** Fetch an object by UUID, retaining the SDK's empty result semantics. */
    public CompletableFuture<Optional<WeaviateObject<P>>> fetchObjectById(String uuid) {
        return tracing.callAsync("fetchObjectById", FISpanKind.RETRIEVER, null, () -> collection.query.fetchObjectById(uuid));
    }

    /** Fetch an object with all SDK return options available. */
    public CompletableFuture<Optional<WeaviateObject<P>>> fetchObjectById(String uuid,
            Function<FetchObjectById.Builder, ObjectBuilder<FetchObjectById>> options) {
        return tracing.callAsync("fetchObjectById", FISpanKind.RETRIEVER, null, () -> collection.query.fetchObjectById(uuid, options));
    }

    /** Insert objects with per-object failure status and counts, without recording error bodies. */
    public CompletableFuture<InsertManyResponse> insertMany(InsertManyRequest<P> request) {
        return tracing.callAsync("insertMany", FISpanKind.VECTOR_DB, null, () -> collection.data.insertMany(request));
    }

    /** Delete matching objects, retaining per-object outcomes. */
    public CompletableFuture<DeleteManyResponse> deleteMany(DeleteManyRequest request) {
        return tracing.callAsync("deleteMany", FISpanKind.VECTOR_DB, null, () -> collection.data.deleteMany(request));
    }

    /** Trace the SDK's insert operation, preserving its result and exception contract. */
    public CompletableFuture<WeaviateObject<P>> insert(InsertObjectRequest<P> request) {
        return tracing.callAsync("insert", FISpanKind.VECTOR_DB, null, () -> collection.data.insert(request));
    }

    /** Trace the SDK's deleteById operation, preserving its result and exception contract. */
    public CompletableFuture<Boolean> deleteById(String uuid) {
        return tracing.callAsync("deleteById", FISpanKind.VECTOR_DB, null, () -> collection.data.delete(uuid));
    }

    /** Trace the SDK's update operation, preserving its result and exception contract. */
    public CompletableFuture<Void> update(String uuid,
            Function<UpdateObjectRequest.Builder<P>, ObjectBuilder<UpdateObjectRequest<P>>> options) {
        return tracing.callAsync("update", FISpanKind.VECTOR_DB, null, () -> collection.data.update(uuid, options));
    }

    /** Trace the SDK's replace operation, preserving its result and exception contract. */
    public CompletableFuture<Void> replace(String uuid,
            Function<ReplaceObjectRequest.Builder<P>, ObjectBuilder<ReplaceObjectRequest<P>>> options) {
        return tracing.callAsync("replace", FISpanKind.VECTOR_DB, null, () -> collection.data.replace(uuid, options));
    }
}
