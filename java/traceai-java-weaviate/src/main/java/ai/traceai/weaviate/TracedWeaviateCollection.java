package ai.traceai.weaviate;

import ai.traceai.FISpanKind;
import ai.traceai.FITracer;
import ai.traceai.TraceAI;
import io.weaviate.client6.v1.api.WeaviateClient;
import io.weaviate.client6.v1.api.collections.CollectionHandle;
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
import java.io.IOException;

/**
 * Explicit synchronous tracing for Weaviate client6 collection queries and object writes.
 * Accepts SDK request objects unchanged, including filters, named vectors and return options.
 * Only calls through this adapter are traced; admin, generation and streaming batch APIs
 * remain accessible on the caller-owned client and are not instrumented here.
 * The existing {@link TracedWeaviateClient} remains the legacy v5 entry point.
 *
 * @param <P> the SDK collection's property type
 */
public final class TracedWeaviateCollection<P> {
    private final CollectionHandle<P> collection;
    private final WeaviateTracing tracing;

    /**
     * Wrap a caller-owned handle, retaining its tenant and consistency defaults.
     * @param collectionName collection name to record in telemetry
     * @param collection configured SDK handle; this adapter never closes its client
     * @param tracer tracer to use for semantic spans
     */
    public TracedWeaviateCollection(String collectionName, CollectionHandle<P> collection, FITracer tracer) {
        this.collection = Objects.requireNonNull(collection, "collection");
        this.tracing = new WeaviateTracing(collectionName, tracer);
    }

    /** Wrap a handle using the global TraceAI tracer. */
    public TracedWeaviateCollection(String collectionName, CollectionHandle<P> collection) {
        this(collectionName, collection, TraceAI.getTracer());
    }

    /** Obtain an untyped collection from an existing client, without owning its lifecycle. */
    public static TracedWeaviateCollection<java.util.Map<String, Object>> of(
            WeaviateClient client, String collectionName, FITracer tracer) {
        return new TracedWeaviateCollection<>(collectionName, client.collections.use(collectionName), tracer);
    }

    /** Return the original handle. Calls on it are not traced by this adapter. */
    public CollectionHandle<P> unwrap() {
        return collection;
    }

    /** Trace the SDK's fetchObjects query without rebuilding its options. */
    public QueryResponse<P> fetchObjects(FetchObjects request) {
        return tracing.call("fetchObjects", FISpanKind.RETRIEVER, request.common().limit(), () -> collection.query.fetchObjects(request));
    }

    /** Trace the SDK's fetchObjects grouped query without rebuilding its options. */
    public QueryResponseGrouped<P> fetchObjects(FetchObjects request, GroupBy groupBy) {
        return tracing.call("fetchObjects", FISpanKind.RETRIEVER, request.common().limit(), () -> collection.query.fetchObjects(request, groupBy));
    }

    /** Trace the SDK's nearVector query without rebuilding its options. */
    public QueryResponse<P> nearVector(NearVector request) {
        return tracing.call("nearVector", FISpanKind.RETRIEVER, request.common().limit(), () -> collection.query.nearVector(request));
    }

    /** Trace the SDK's nearVector grouped query without rebuilding its options. */
    public QueryResponseGrouped<P> nearVector(NearVector request, GroupBy groupBy) {
        return tracing.call("nearVector", FISpanKind.RETRIEVER, request.common().limit(), () -> collection.query.nearVector(request, groupBy));
    }

    /** Trace the SDK's nearText query without rebuilding its options. */
    public QueryResponse<P> nearText(NearText request) {
        return tracing.call("nearText", FISpanKind.RETRIEVER, request.common().limit(), () -> collection.query.nearText(request));
    }

    /** Trace the SDK's nearText grouped query without rebuilding its options. */
    public QueryResponseGrouped<P> nearText(NearText request, GroupBy groupBy) {
        return tracing.call("nearText", FISpanKind.RETRIEVER, request.common().limit(), () -> collection.query.nearText(request, groupBy));
    }

    /** Trace the SDK's nearObject query without rebuilding its options. */
    public QueryResponse<P> nearObject(NearObject request) {
        return tracing.call("nearObject", FISpanKind.RETRIEVER, request.common().limit(), () -> collection.query.nearObject(request));
    }

    /** Trace the SDK's nearObject grouped query without rebuilding its options. */
    public QueryResponseGrouped<P> nearObject(NearObject request, GroupBy groupBy) {
        return tracing.call("nearObject", FISpanKind.RETRIEVER, request.common().limit(), () -> collection.query.nearObject(request, groupBy));
    }

    /** Trace the SDK's bm25 query without rebuilding its options. */
    public QueryResponse<P> bm25(Bm25 request) {
        return tracing.call("bm25", FISpanKind.RETRIEVER, request.common().limit(), () -> collection.query.bm25(request));
    }

    /** Trace the SDK's bm25 grouped query without rebuilding its options. */
    public QueryResponseGrouped<P> bm25(Bm25 request, GroupBy groupBy) {
        return tracing.call("bm25", FISpanKind.RETRIEVER, request.common().limit(), () -> collection.query.bm25(request, groupBy));
    }

    /** Trace the SDK's hybrid query without rebuilding its options. */
    public QueryResponse<P> hybrid(Hybrid request) {
        return tracing.call("hybrid", FISpanKind.RETRIEVER, request.common().limit(), () -> collection.query.hybrid(request));
    }

    /** Trace the SDK's hybrid grouped query without rebuilding its options. */
    public QueryResponseGrouped<P> hybrid(Hybrid request, GroupBy groupBy) {
        return tracing.call("hybrid", FISpanKind.RETRIEVER, request.common().limit(), () -> collection.query.hybrid(request, groupBy));
    }

    /** Fetch an object by UUID, retaining the SDK's empty result semantics. */
    public Optional<WeaviateObject<P>> fetchObjectById(String uuid) {
        return tracing.call("fetchObjectById", FISpanKind.RETRIEVER, null, () -> collection.query.fetchObjectById(uuid));
    }

    /** Fetch an object with all SDK return options available. */
    public Optional<WeaviateObject<P>> fetchObjectById(String uuid,
            Function<FetchObjectById.Builder, ObjectBuilder<FetchObjectById>> options) {
        return tracing.call("fetchObjectById", FISpanKind.RETRIEVER, null, () -> collection.query.fetchObjectById(uuid, options));
    }

    /** Insert objects with per-object failure status and counts, without recording error bodies. */
    public InsertManyResponse insertMany(InsertManyRequest<P> request) {
        return tracing.call("insertMany", FISpanKind.VECTOR_DB, null, () -> collection.data.insertMany(request));
    }

    /** Delete matching objects, retaining per-object outcomes. */
    public DeleteManyResponse deleteMany(DeleteManyRequest request) {
        return tracing.call("deleteMany", FISpanKind.VECTOR_DB, null, () -> collection.data.deleteMany(request));
    }

    /** Trace the SDK's insert operation, preserving its result and exception contract. */
    public WeaviateObject<P> insert(InsertObjectRequest<P> request) throws IOException {
        return tracing.callIO("insert", () -> collection.data.insert(request));
    }

    /** Trace the SDK's deleteById operation, preserving its result and exception contract. */
    public boolean deleteById(String uuid) throws IOException {
        return tracing.callIO("deleteById", () -> collection.data.deleteById(uuid));
    }

    /** Trace the SDK's update operation, preserving its result and exception contract. */
    public void update(String uuid,
            Function<UpdateObjectRequest.Builder<P>, ObjectBuilder<UpdateObjectRequest<P>>> options) throws IOException {
        tracing.callIO("update", () -> { collection.data.update(uuid, options); return null; });
    }

    /** Trace the SDK's replace operation, preserving its result and exception contract. */
    public void replace(String uuid,
            Function<ReplaceObjectRequest.Builder<P>, ObjectBuilder<ReplaceObjectRequest<P>>> options) throws IOException {
        tracing.callIO("replace", () -> { collection.data.replace(uuid, options); return null; });
    }
}
