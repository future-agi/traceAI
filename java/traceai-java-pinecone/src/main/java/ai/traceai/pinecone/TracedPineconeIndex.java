package ai.traceai.pinecone;

import ai.traceai.FISpanKind;
import ai.traceai.FITracer;
import ai.traceai.SemanticConventions;
import ai.traceai.TraceAI;
import com.google.protobuf.ListValue;
import com.google.protobuf.NullValue;
import com.google.protobuf.Struct;
import com.google.protobuf.Value;
import io.opentelemetry.api.trace.Span;
import io.pinecone.clients.Index;
import io.pinecone.proto.DeleteResponse;
import io.pinecone.proto.FetchResponse;
import io.pinecone.proto.UpsertResponse;
import io.pinecone.unsigned_indices_model.QueryResponseWithUnsignedIndices;
import io.pinecone.unsigned_indices_model.VectorWithUnsignedIndices;

import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.function.Function;

/**
 * Traces synchronous Pinecone query, upsert, delete-by-ID and fetch operations.
 * Requests and native responses are passed through without copying their contents.
 * Spans contain operation metadata and counts, not filters, IDs or vector contents.
 * Operations performed through {@link #unwrap()} are not instrumented by this wrapper.
 */
public class TracedPineconeIndex {

    private final Index index;
    private final FITracer tracer;
    private final String indexName;

    /**
     * @param index the Pinecone index to wrap
     * @param tracer the tracer to use; its provider remains owned by the caller
     * @param indexName the index name to record on spans
     */
    public TracedPineconeIndex(Index index, FITracer tracer, String indexName) {
        this.index = index;
        this.tracer = tracer;
        this.indexName = indexName;
    }

    /**
     * @param index the Pinecone index to wrap
     * @param indexName the index name to record on spans
     */
    public TracedPineconeIndex(Index index, String indexName) {
        this(index, TraceAI.getTracer(), indexName);
    }

    /**
     * Queries with the legacy convenience defaults: include values and metadata.
     * Use an explicit-flags overload to control the response payload.
     * @param queryVector the query vector
     * @param topK the number of results
     * @return the unchanged native query response
     */
    public QueryResponseWithUnsignedIndices query(List<Float> queryVector, int topK) {
        return query(queryVector, topK, null, null, true, true);
    }

    /**
     * Queries with the legacy convenience defaults: include values and metadata.
     * @param queryVector the query vector
     * @param topK the number of results
     * @param namespace the namespace, or null for the default
     * @param filter a JSON-compatible metadata filter, or null
     * @return the unchanged native query response
     */
    public QueryResponseWithUnsignedIndices query(
            List<Float> queryVector, int topK, String namespace, Map<String, Object> filter) {
        return query(queryVector, topK, namespace, filter, true, true);
    }

    /**
     * Queries with explicit response flags and a recursively converted JSON filter.
     * Nested maps require string keys; supported values are maps, lists, strings,
     * finite numbers, booleans and null. Other values are rejected, never stringified.
     * @param queryVector the query vector
     * @param topK the number of results
     * @param namespace the namespace, or null for the default
     * @param filter a JSON-compatible metadata filter, or null
     * @param includeValues whether to return vector values
     * @param includeMetadata whether to return vector metadata
     * @return the unchanged native query response
     */
    public QueryResponseWithUnsignedIndices query(
            List<Float> queryVector, int topK, String namespace, Map<String, Object> filter,
            boolean includeValues, boolean includeMetadata) {
        return query(topK, queryVector, null, null, null, namespace,
                filter == null ? null : buildFilterStruct(filter), includeValues, includeMetadata);
    }

    /**
     * Mirrors the native SDK's full query signature, including sparse and ID queries.
     * Validation, exceptions and the returned response are left to the SDK.
     * @param topK the number of results
     * @param vector the dense query vector
     * @param sparseIndices the sparse indices
     * @param sparseValues the sparse values
     * @param id the query vector ID
     * @param namespace the namespace
     * @param filter the native protobuf filter (passed through unchanged)
     * @param includeValues whether to return vector values
     * @param includeMetadata whether to return vector metadata
     * @return the unchanged native query response
     */
    public QueryResponseWithUnsignedIndices query(
            int topK, List<Float> vector, List<Long> sparseIndices, List<Float> sparseValues,
            String id, String namespace, Struct filter, boolean includeValues, boolean includeMetadata) {
        return trace("Query", FISpanKind.RETRIEVER, namespace, span -> {
            span.setAttribute(SemanticConventions.RETRIEVER_TOP_K, (long) topK);
            if (vector != null) {
                span.setAttribute(SemanticConventions.EMBEDDING_DIMENSIONS, (long) vector.size());
            }
            QueryResponseWithUnsignedIndices response = index.query(topK, vector, sparseIndices,
                    sparseValues, id, namespace, filter, includeValues, includeMetadata);
            if (response != null && response.getMatchesList() != null) {
                span.setAttribute(SemanticConventions.DB_VECTOR_RESULTS_COUNT,
                        (long) response.getMatchesList().size());
                if (!response.getMatchesList().isEmpty()) {
                    span.setAttribute("pinecone.top_score", response.getMatchesList().get(0).getScore());
                }
            }
            return response;
        });
    }

    /**
     * Legacy convenience API returning the number of submitted vectors, not the
     * server's upsert count. Use {@link #upsertResponse(List, String)} for the native result.
     * @param vectors the vectors to upsert
     * @param namespace the namespace
     * @return the number of submitted vectors
     */
    public int upsert(List<VectorWithUnsignedIndices> vectors, String namespace) {
        upsertResponse(vectors, namespace);
        return vectors == null ? 0 : vectors.size();
    }

    /**
     * @param vectors the vectors to upsert
     * @param namespace the namespace
     * @return the unchanged native upsert response
     */
    public UpsertResponse upsertResponse(List<VectorWithUnsignedIndices> vectors, String namespace) {
        return trace("Upsert", FISpanKind.VECTOR_DB, namespace, span -> {
            if (vectors != null) {
                span.setAttribute(SemanticConventions.DB_VECTOR_UPSERT_COUNT, (long) vectors.size());
            }
            UpsertResponse response = index.upsert(vectors, namespace);
            if (vectors != null && !vectors.isEmpty() && vectors.get(0) != null
                    && vectors.get(0).getValuesList() != null) {
                span.setAttribute(SemanticConventions.EMBEDDING_DIMENSIONS,
                        (long) vectors.get(0).getValuesList().size());
            }
            return response;
        });
    }

    /**
     * Deletes vectors, retaining the original void convenience API.
     * @param ids the vector IDs
     * @param namespace the namespace
     */
    public void deleteByIds(List<String> ids, String namespace) {
        deleteByIdsResponse(ids, namespace);
    }

    /**
     * @param ids the vector IDs
     * @param namespace the namespace
     * @return the unchanged native delete response
     */
    public DeleteResponse deleteByIdsResponse(List<String> ids, String namespace) {
        return trace("Delete", FISpanKind.VECTOR_DB, namespace, span -> {
            if (ids != null) {
                span.setAttribute(SemanticConventions.DB_VECTOR_DELETE_COUNT, (long) ids.size());
            }
            return index.deleteByIds(ids, namespace);
        });
    }

    /**
     * Retains the original map containing the native response under {@code "response"}.
     * @param ids the vector IDs
     * @param namespace the namespace
     * @return a mutable compatibility map containing the native response
     * @deprecated Use {@link #fetchResponse(List, String)} to access typed vectors,
     *             namespace and usage without a map wrapper.
     */
    @Deprecated
    public Map<String, Object> fetch(List<String> ids, String namespace) {
        Map<String, Object> result = new HashMap<>();
        result.put("response", fetchResponse(ids, namespace));
        return result;
    }

    /**
     * @param ids the vector IDs
     * @param namespace the namespace
     * @return the unchanged native fetch response, including vectors, namespace and usage
     */
    public FetchResponse fetchResponse(List<String> ids, String namespace) {
        return trace("Fetch", FISpanKind.VECTOR_DB, namespace, span -> {
            if (ids != null) {
                span.setAttribute("pinecone.fetch_ids_count", (long) ids.size());
            }
            FetchResponse response = index.fetch(ids, namespace);
            if (response != null) {
                span.setAttribute("pinecone.fetched_count", (long) response.getVectorsCount());
            }
            return response;
        });
    }

    /** @return the wrapped SDK index; calls on it bypass this wrapper's tracing */
    public Index unwrap() {
        return index;
    }

    private <T> T trace(String operation, FISpanKind kind, String namespace, Function<Span, T> call) {
        // FITracer owns the scope/error/finally lifecycle and rethrows the original exception.
        return tracer.trace("Pinecone " + operation, kind, () -> {
            Span span = Span.current();
            span.setAttribute(SemanticConventions.DB_SYSTEM, "pinecone");
            span.setAttribute(SemanticConventions.DB_VECTOR_INDEX_NAME, indexName);
            if (namespace != null) {
                span.setAttribute(SemanticConventions.DB_VECTOR_NAMESPACE, namespace);
            }
            return call.apply(span);
        });
    }

    private static Struct buildFilterStruct(Map<?, ?> filter) {
        Struct.Builder builder = Struct.newBuilder();
        for (Map.Entry<?, ?> entry : filter.entrySet()) {
            if (!(entry.getKey() instanceof String)) {
                throw new IllegalArgumentException("Pinecone filter keys must be strings");
            }
            builder.putFields((String) entry.getKey(), toValue(entry.getValue()));
        }
        return builder.build();
    }

    private static Value toValue(Object obj) {
        Value.Builder builder = Value.newBuilder();
        if (obj == null) {
            return builder.setNullValue(NullValue.NULL_VALUE).build();
        } else if (obj instanceof String) {
            return builder.setStringValue((String) obj).build();
        } else if (obj instanceof Number) {
            double number = ((Number) obj).doubleValue();
            if (!Double.isFinite(number)) {
                throw new IllegalArgumentException("Pinecone filter numbers must be finite");
            }
            return builder.setNumberValue(number).build();
        } else if (obj instanceof Boolean) {
            return builder.setBoolValue((Boolean) obj).build();
        } else if (obj instanceof Map) {
            return builder.setStructValue(buildFilterStruct((Map<?, ?>) obj)).build();
        } else if (obj instanceof List) {
            ListValue.Builder list = ListValue.newBuilder();
            for (Object value : (List<?>) obj) {
                list.addValues(toValue(value));
            }
            return builder.setListValue(list).build();
        }
        throw new IllegalArgumentException("Pinecone filters require JSON-compatible values");
    }
}
