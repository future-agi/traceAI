package ai.traceai.pinecone;

import ai.traceai.*;
import io.pinecone.clients.Index;
import io.pinecone.unsigned_indices_model.*;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.context.Scope;

import java.util.List;
import java.util.Map;

/**
 * Instrumentation wrapper for Pinecone Index operations.
 * Wraps the Pinecone Index to provide automatic tracing of all vector operations.
 *
 * <p>Usage:</p>
 * <pre>
 * Pinecone pinecone = new Pinecone.Builder(apiKey).build();
 * Index index = pinecone.getIndexConnection("my-index");
 * TracedPineconeIndex traced = new TracedPineconeIndex(index, "my-index");
 *
 * QueryResponseWithUnsignedIndices result = traced.query(queryVector, 10);
 * </pre>
 */
public class TracedPineconeIndex {

    private final Index index;
    private final FITracer tracer;
    private final String indexName;

    /**
     * Creates a new traced Pinecone index with the given index and tracer.
     *
     * @param index     the Pinecone Index to wrap
     * @param tracer    the FITracer for instrumentation
     * @param indexName the name of the index for tracing
     */
    public TracedPineconeIndex(Index index, FITracer tracer, String indexName) {
        this.index = index;
        this.tracer = tracer;
        this.indexName = indexName;
    }

    /**
     * Creates a new traced Pinecone index using the global TraceAI tracer.
     *
     * @param index     the Pinecone Index to wrap
     * @param indexName the name of the index
     */
    public TracedPineconeIndex(Index index, String indexName) {
        this(index, TraceAI.getTracer(), indexName);
    }

    /**
     * Queries the index with tracing.
     *
     * @param queryVector the query vector
     * @param topK        number of results to return
     * @return the query response
     */
    public QueryResponseWithUnsignedIndices query(List<Float> queryVector, int topK) {
        Span span = tracer.startSpan("Pinecone Query", FISpanKind.RETRIEVER);

        try (Scope scope = span.makeCurrent()) {
            // Set attributes
            span.setAttribute(SemanticConventions.LLM_SYSTEM, "pinecone");
            span.setAttribute("pinecone.index", indexName);
            span.setAttribute(SemanticConventions.RETRIEVER_TOP_K, (long) topK);
            span.setAttribute(SemanticConventions.EMBEDDING_DIMENSIONS, (long) queryVector.size());

            // Execute query
            QueryResponseWithUnsignedIndices response = index.query(
                topK,
                queryVector,
                null,  // sparseValues
                null,  // namespace
                null,  // filter
                true,  // includeValues
                true   // includeMetadata
            );

            // Capture results
            if (response.getMatchesList() != null) {
                span.setAttribute("pinecone.matches_count", (long) response.getMatchesList().size());

                // Capture top match score
                if (!response.getMatchesList().isEmpty()) {
                    span.setAttribute("pinecone.top_score",
                        response.getMatchesList().get(0).getScore());
                }
            }

            span.setStatus(StatusCode.OK);
            return response;

        } catch (Exception e) {
            tracer.setError(span, e);
            throw new RuntimeException("Pinecone query failed", e);
        } finally {
            span.end();
        }
    }

    /**
     * Queries the index with namespace and filter with tracing.
     *
     * @param queryVector the query vector
     * @param topK        number of results to return
     * @param namespace   the namespace to query
     * @param filter      metadata filter
     * @return the query response
     */
    public QueryResponseWithUnsignedIndices query(
            List<Float> queryVector,
            int topK,
            String namespace,
            Map<String, Object> filter) {
        Span span = tracer.startSpan("Pinecone Query", FISpanKind.RETRIEVER);

        try (Scope scope = span.makeCurrent()) {
            // Set attributes
            span.setAttribute(SemanticConventions.LLM_SYSTEM, "pinecone");
            span.setAttribute("pinecone.index", indexName);
            span.setAttribute(SemanticConventions.RETRIEVER_TOP_K, (long) topK);
            span.setAttribute(SemanticConventions.EMBEDDING_DIMENSIONS, (long) queryVector.size());

            if (namespace != null) {
                span.setAttribute("pinecone.namespace", namespace);
            }
            if (filter != null) {
                span.setAttribute("pinecone.filter", tracer.toJson(filter));
            }

            // Build filter struct if provided
            com.google.protobuf.Struct filterStruct = null;
            if (filter != null && !filter.isEmpty()) {
                filterStruct = buildFilterStruct(filter);
            }

            // Execute query
            QueryResponseWithUnsignedIndices response = index.query(
                topK,
                queryVector,
                null,         // sparseValues
                namespace,
                filterStruct,
                true,         // includeValues
                true          // includeMetadata
            );

            // Capture results
            if (response.getMatchesList() != null) {
                span.setAttribute("pinecone.matches_count", (long) response.getMatchesList().size());
            }

            span.setStatus(StatusCode.OK);
            return response;

        } catch (Exception e) {
            tracer.setError(span, e);
            throw new RuntimeException("Pinecone query failed", e);
        } finally {
            span.end();
        }
    }

    /**
     * Upserts vectors with tracing.
     *
     * @param vectors   the vectors to upsert
     * @param namespace the namespace (optional)
     * @return the upsert response
     */
    public UpsertResponse upsert(List<VectorWithUnsignedIndices> vectors, String namespace) {
        Span span = tracer.startSpan("Pinecone Upsert", FISpanKind.EMBEDDING);

        try (Scope scope = span.makeCurrent()) {
            // Set attributes
            span.setAttribute(SemanticConventions.LLM_SYSTEM, "pinecone");
            span.setAttribute("pinecone.index", indexName);
            span.setAttribute("pinecone.vectors_count", (long) vectors.size());

            if (namespace != null) {
                span.setAttribute("pinecone.namespace", namespace);
            }

            if (!vectors.isEmpty()) {
                span.setAttribute(SemanticConventions.EMBEDDING_DIMENSIONS,
                    (long) vectors.get(0).getValuesList().size());
            }

            // Execute upsert
            UpsertResponse response = index.upsert(vectors, namespace);

            // Capture result
            span.setAttribute("pinecone.upserted_count", response.getUpsertedCount());

            span.setStatus(StatusCode.OK);
            return response;

        } catch (Exception e) {
            tracer.setError(span, e);
            throw new RuntimeException("Pinecone upsert failed", e);
        } finally {
            span.end();
        }
    }

    /**
     * Deletes vectors by IDs with tracing.
     *
     * @param ids       the vector IDs to delete
     * @param namespace the namespace (optional)
     */
    public void deleteByIds(List<String> ids, String namespace) {
        Span span = tracer.startSpan("Pinecone Delete", FISpanKind.RETRIEVER);

        try (Scope scope = span.makeCurrent()) {
            // Set attributes
            span.setAttribute(SemanticConventions.LLM_SYSTEM, "pinecone");
            span.setAttribute("pinecone.index", indexName);
            span.setAttribute("pinecone.delete_count", (long) ids.size());

            if (namespace != null) {
                span.setAttribute("pinecone.namespace", namespace);
            }

            // Execute delete
            index.deleteByIds(ids, namespace);

            span.setStatus(StatusCode.OK);

        } catch (Exception e) {
            tracer.setError(span, e);
            throw new RuntimeException("Pinecone delete failed", e);
        } finally {
            span.end();
        }
    }

    /**
     * Fetches vectors by IDs with tracing.
     *
     * @param ids       the vector IDs to fetch
     * @param namespace the namespace (optional)
     * @return the fetch response
     */
    public FetchResponse fetch(List<String> ids, String namespace) {
        Span span = tracer.startSpan("Pinecone Fetch", FISpanKind.RETRIEVER);

        try (Scope scope = span.makeCurrent()) {
            // Set attributes
            span.setAttribute(SemanticConventions.LLM_SYSTEM, "pinecone");
            span.setAttribute("pinecone.index", indexName);
            span.setAttribute("pinecone.fetch_ids_count", (long) ids.size());

            if (namespace != null) {
                span.setAttribute("pinecone.namespace", namespace);
            }

            // Execute fetch
            FetchResponse response = index.fetch(ids, namespace);

            // Capture result
            if (response.getVectorsMap() != null) {
                span.setAttribute("pinecone.fetched_count", (long) response.getVectorsMap().size());
            }

            span.setStatus(StatusCode.OK);
            return response;

        } catch (Exception e) {
            tracer.setError(span, e);
            throw new RuntimeException("Pinecone fetch failed", e);
        } finally {
            span.end();
        }
    }

    /**
     * Gets the underlying Pinecone Index.
     *
     * @return the wrapped Index
     */
    public Index unwrap() {
        return index;
    }

    private com.google.protobuf.Struct buildFilterStruct(Map<String, Object> filter) {
        com.google.protobuf.Struct.Builder builder = com.google.protobuf.Struct.newBuilder();
        for (Map.Entry<String, Object> entry : filter.entrySet()) {
            builder.putFields(entry.getKey(), toValue(entry.getValue()));
        }
        return builder.build();
    }

    private com.google.protobuf.Value toValue(Object obj) {
        if (obj == null) {
            return com.google.protobuf.Value.newBuilder().setNullValue(
                com.google.protobuf.NullValue.NULL_VALUE).build();
        } else if (obj instanceof String) {
            return com.google.protobuf.Value.newBuilder().setStringValue((String) obj).build();
        } else if (obj instanceof Number) {
            return com.google.protobuf.Value.newBuilder().setNumberValue(((Number) obj).doubleValue()).build();
        } else if (obj instanceof Boolean) {
            return com.google.protobuf.Value.newBuilder().setBoolValue((Boolean) obj).build();
        } else {
            return com.google.protobuf.Value.newBuilder().setStringValue(obj.toString()).build();
        }
    }
}
