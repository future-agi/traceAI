package ai.traceai.elasticsearch;

import ai.traceai.*;
import co.elastic.clients.elasticsearch.ElasticsearchClient;
import co.elastic.clients.elasticsearch._types.mapping.Property;
import co.elastic.clients.elasticsearch._types.query_dsl.Query;
import co.elastic.clients.elasticsearch.core.*;
import co.elastic.clients.elasticsearch.core.bulk.BulkOperation;
import co.elastic.clients.elasticsearch.core.bulk.IndexOperation;
import co.elastic.clients.elasticsearch.core.search.Hit;
import co.elastic.clients.elasticsearch.indices.CreateIndexRequest;
import co.elastic.clients.elasticsearch.indices.CreateIndexResponse;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.context.Scope;

import java.io.IOException;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;

/**
 * Instrumentation wrapper for Elasticsearch Java client.
 * Wraps the ElasticsearchClient to provide automatic tracing of vector search operations.
 *
 * <p>Usage:</p>
 * <pre>
 * ElasticsearchClient client = new ElasticsearchClient(transport);
 * TracedElasticsearchClient traced = new TracedElasticsearchClient(client);
 *
 * SearchResponse&lt;Map&gt; results = traced.knnSearch("my-index", queryVector, 10, 100, "embedding");
 * </pre>
 */
public class TracedElasticsearchClient {

    private final ElasticsearchClient client;
    private final FITracer tracer;

    /**
     * Creates a new traced Elasticsearch client with the given client and tracer.
     *
     * @param client the ElasticsearchClient to wrap
     * @param tracer the FITracer for instrumentation
     */
    public TracedElasticsearchClient(ElasticsearchClient client, FITracer tracer) {
        this.client = client;
        this.tracer = tracer;
    }

    /**
     * Creates a new traced Elasticsearch client using the global TraceAI tracer.
     *
     * @param client the ElasticsearchClient to wrap
     */
    public TracedElasticsearchClient(ElasticsearchClient client) {
        this(client, TraceAI.getTracer());
    }

    /**
     * Performs a k-NN vector search with tracing.
     *
     * @param index         the index to search
     * @param queryVector   the query vector
     * @param k             the number of nearest neighbors to return
     * @param numCandidates the number of candidates to consider
     * @param field         the vector field name
     * @return the search response
     * @throws IOException if the search fails
     */
    @SuppressWarnings("unchecked")
    public SearchResponse<Map<String, Object>> knnSearch(String index, float[] queryVector, int k,
                                                          int numCandidates, String field) throws IOException {
        Span span = tracer.startSpan("Elasticsearch KNN Search", FISpanKind.RETRIEVER);

        try (Scope scope = span.makeCurrent()) {
            // Set attributes
            span.setAttribute(SemanticConventions.LLM_SYSTEM, "elasticsearch");
            span.setAttribute("db.system", "elasticsearch");
            span.setAttribute("elasticsearch.index", index);
            span.setAttribute(SemanticConventions.RETRIEVER_TOP_K, (long) k);
            span.setAttribute(SemanticConventions.EMBEDDING_DIMENSIONS, (long) queryVector.length);
            span.setAttribute("elasticsearch.num_candidates", (long) numCandidates);
            span.setAttribute("elasticsearch.field", field);

            // Convert float[] to List<Float>
            List<Float> vectorList = new ArrayList<>(queryVector.length);
            for (float v : queryVector) {
                vectorList.add(v);
            }

            // Execute k-NN search
            SearchResponse<Map<String, Object>> response = client.search(s -> s
                .index(index)
                .knn(knn -> knn
                    .field(field)
                    .queryVector(vectorList)
                    .k(k)
                    .numCandidates(numCandidates)
                ),
                (Class<Map<String, Object>>) (Class<?>) Map.class
            );

            // Capture results
            List<Hit<Map<String, Object>>> hits = response.hits().hits();
            span.setAttribute("elasticsearch.results_count", (long) hits.size());
            if (!hits.isEmpty() && hits.get(0).score() != null) {
                span.setAttribute("elasticsearch.top_score", hits.get(0).score());
            }
            if (response.hits().total() != null) {
                span.setAttribute("elasticsearch.total_hits", response.hits().total().value());
            }

            span.setStatus(StatusCode.OK);
            return response;

        } catch (Exception e) {
            tracer.setError(span, e);
            throw e;
        } finally {
            span.end();
        }
    }

    /**
     * Performs a k-NN vector search with filter with tracing.
     *
     * @param index         the index to search
     * @param queryVector   the query vector
     * @param k             the number of nearest neighbors to return
     * @param numCandidates the number of candidates to consider
     * @param field         the vector field name
     * @param filter        the query filter
     * @return the search response
     * @throws IOException if the search fails
     */
    @SuppressWarnings("unchecked")
    public SearchResponse<Map<String, Object>> knnSearchWithFilter(String index, float[] queryVector, int k,
                                                                    int numCandidates, String field,
                                                                    Query filter) throws IOException {
        Span span = tracer.startSpan("Elasticsearch KNN Search with Filter", FISpanKind.RETRIEVER);

        try (Scope scope = span.makeCurrent()) {
            // Set attributes
            span.setAttribute(SemanticConventions.LLM_SYSTEM, "elasticsearch");
            span.setAttribute("db.system", "elasticsearch");
            span.setAttribute("elasticsearch.index", index);
            span.setAttribute(SemanticConventions.RETRIEVER_TOP_K, (long) k);
            span.setAttribute(SemanticConventions.EMBEDDING_DIMENSIONS, (long) queryVector.length);
            span.setAttribute("elasticsearch.num_candidates", (long) numCandidates);
            span.setAttribute("elasticsearch.field", field);
            span.setAttribute("elasticsearch.has_filter", true);

            // Convert float[] to List<Float>
            List<Float> vectorList = new ArrayList<>(queryVector.length);
            for (float v : queryVector) {
                vectorList.add(v);
            }

            // Execute k-NN search with filter
            SearchResponse<Map<String, Object>> response = client.search(s -> s
                .index(index)
                .knn(knn -> knn
                    .field(field)
                    .queryVector(vectorList)
                    .k(k)
                    .numCandidates(numCandidates)
                    .filter(filter)
                ),
                (Class<Map<String, Object>>) (Class<?>) Map.class
            );

            // Capture results
            List<Hit<Map<String, Object>>> hits = response.hits().hits();
            span.setAttribute("elasticsearch.results_count", (long) hits.size());
            if (!hits.isEmpty() && hits.get(0).score() != null) {
                span.setAttribute("elasticsearch.top_score", hits.get(0).score());
            }
            if (response.hits().total() != null) {
                span.setAttribute("elasticsearch.total_hits", response.hits().total().value());
            }

            span.setStatus(StatusCode.OK);
            return response;

        } catch (Exception e) {
            tracer.setError(span, e);
            throw e;
        } finally {
            span.end();
        }
    }

    /**
     * Indexes a document with tracing.
     *
     * @param indexName the index name
     * @param id        the document ID
     * @param document  the document to index (should contain vector field)
     * @return the index response
     * @throws IOException if indexing fails
     */
    public IndexResponse index(String indexName, String id, Map<String, Object> document) throws IOException {
        Span span = tracer.startSpan("Elasticsearch Index Document", FISpanKind.EMBEDDING);

        try (Scope scope = span.makeCurrent()) {
            // Set attributes
            span.setAttribute(SemanticConventions.LLM_SYSTEM, "elasticsearch");
            span.setAttribute("db.system", "elasticsearch");
            span.setAttribute("elasticsearch.index", indexName);
            span.setAttribute("elasticsearch.document_id", id);

            // Try to capture vector dimensions if present
            for (Object value : document.values()) {
                if (value instanceof float[]) {
                    span.setAttribute(SemanticConventions.EMBEDDING_DIMENSIONS, (long) ((float[]) value).length);
                    break;
                } else if (value instanceof List && !((List<?>) value).isEmpty()) {
                    Object first = ((List<?>) value).get(0);
                    if (first instanceof Number) {
                        span.setAttribute(SemanticConventions.EMBEDDING_DIMENSIONS, (long) ((List<?>) value).size());
                        break;
                    }
                }
            }

            // Execute index
            IndexResponse response = client.index(i -> i
                .index(indexName)
                .id(id)
                .document(document)
            );

            // Capture result
            span.setAttribute("elasticsearch.result", response.result().jsonValue());
            span.setAttribute("elasticsearch.version", response.version());

            span.setStatus(StatusCode.OK);
            return response;

        } catch (Exception e) {
            tracer.setError(span, e);
            throw e;
        } finally {
            span.end();
        }
    }

    /**
     * Bulk indexes documents with tracing.
     *
     * @param indexName the index name
     * @param documents the documents to index (each should have an "_id" field or use generated IDs)
     * @return the bulk response
     * @throws IOException if bulk indexing fails
     */
    public BulkResponse bulkIndex(String indexName, List<Map<String, Object>> documents) throws IOException {
        Span span = tracer.startSpan("Elasticsearch Bulk Index", FISpanKind.EMBEDDING);

        try (Scope scope = span.makeCurrent()) {
            // Set attributes
            span.setAttribute(SemanticConventions.LLM_SYSTEM, "elasticsearch");
            span.setAttribute("db.system", "elasticsearch");
            span.setAttribute("elasticsearch.index", indexName);
            span.setAttribute("elasticsearch.bulk_count", (long) documents.size());

            // Try to capture vector dimensions from first document
            if (!documents.isEmpty()) {
                for (Object value : documents.get(0).values()) {
                    if (value instanceof float[]) {
                        span.setAttribute(SemanticConventions.EMBEDDING_DIMENSIONS, (long) ((float[]) value).length);
                        break;
                    } else if (value instanceof List && !((List<?>) value).isEmpty()) {
                        Object first = ((List<?>) value).get(0);
                        if (first instanceof Number) {
                            span.setAttribute(SemanticConventions.EMBEDDING_DIMENSIONS, (long) ((List<?>) value).size());
                            break;
                        }
                    }
                }
            }

            // Build bulk operations
            List<BulkOperation> operations = new ArrayList<>();
            for (Map<String, Object> doc : documents) {
                String id = null;
                Map<String, Object> docToIndex = doc;

                // Extract _id if present
                if (doc.containsKey("_id")) {
                    id = String.valueOf(doc.get("_id"));
                    // Create a copy without _id
                    docToIndex = new java.util.HashMap<>(doc);
                    docToIndex.remove("_id");
                }

                final String docId = id;
                final Map<String, Object> finalDoc = docToIndex;

                operations.add(BulkOperation.of(op -> op
                    .index(idx -> {
                        IndexOperation.Builder<Map<String, Object>> builder = idx
                            .index(indexName)
                            .document(finalDoc);
                        if (docId != null) {
                            builder.id(docId);
                        }
                        return builder;
                    })
                ));
            }

            // Execute bulk
            BulkResponse response = client.bulk(b -> b.operations(operations));

            // Capture result
            span.setAttribute("elasticsearch.took_ms", response.took());
            span.setAttribute("elasticsearch.has_errors", response.errors());
            if (!response.errors()) {
                span.setAttribute("elasticsearch.indexed_count", (long) response.items().size());
            }

            span.setStatus(response.errors() ? StatusCode.ERROR : StatusCode.OK);
            return response;

        } catch (Exception e) {
            tracer.setError(span, e);
            throw e;
        } finally {
            span.end();
        }
    }

    /**
     * Deletes a document with tracing.
     *
     * @param indexName the index name
     * @param id        the document ID
     * @return the delete response
     * @throws IOException if deletion fails
     */
    public DeleteResponse delete(String indexName, String id) throws IOException {
        Span span = tracer.startSpan("Elasticsearch Delete Document", FISpanKind.RETRIEVER);

        try (Scope scope = span.makeCurrent()) {
            // Set attributes
            span.setAttribute(SemanticConventions.LLM_SYSTEM, "elasticsearch");
            span.setAttribute("db.system", "elasticsearch");
            span.setAttribute("elasticsearch.index", indexName);
            span.setAttribute("elasticsearch.document_id", id);

            // Execute delete
            DeleteResponse response = client.delete(d -> d
                .index(indexName)
                .id(id)
            );

            // Capture result
            span.setAttribute("elasticsearch.result", response.result().jsonValue());

            span.setStatus(StatusCode.OK);
            return response;

        } catch (Exception e) {
            tracer.setError(span, e);
            throw e;
        } finally {
            span.end();
        }
    }

    /**
     * Creates an index with mappings with tracing.
     *
     * @param indexName the index name
     * @param mappings  the property mappings (e.g., for dense_vector fields)
     * @return the create index response
     * @throws IOException if index creation fails
     */
    public CreateIndexResponse createIndex(String indexName, Map<String, Property> mappings) throws IOException {
        Span span = tracer.startSpan("Elasticsearch Create Index", FISpanKind.RETRIEVER);

        try (Scope scope = span.makeCurrent()) {
            // Set attributes
            span.setAttribute(SemanticConventions.LLM_SYSTEM, "elasticsearch");
            span.setAttribute("db.system", "elasticsearch");
            span.setAttribute("elasticsearch.index", indexName);
            span.setAttribute("elasticsearch.mappings_count", (long) mappings.size());

            // Try to capture vector dimensions from dense_vector mapping
            for (Map.Entry<String, Property> entry : mappings.entrySet()) {
                Property prop = entry.getValue();
                if (prop.isDenseVector() && prop.denseVector().dims() != null) {
                    span.setAttribute(SemanticConventions.EMBEDDING_DIMENSIONS, (long) prop.denseVector().dims());
                    span.setAttribute("elasticsearch.vector_field", entry.getKey());
                    break;
                }
            }

            // Execute create index
            CreateIndexResponse response = client.indices().create(c -> c
                .index(indexName)
                .mappings(m -> m.properties(mappings))
            );

            // Capture result
            span.setAttribute("elasticsearch.acknowledged", response.acknowledged());
            span.setAttribute("elasticsearch.shards_acknowledged", response.shardsAcknowledged());

            span.setStatus(StatusCode.OK);
            return response;

        } catch (Exception e) {
            tracer.setError(span, e);
            throw e;
        } finally {
            span.end();
        }
    }

    /**
     * Gets the underlying ElasticsearchClient.
     *
     * @return the wrapped ElasticsearchClient
     */
    public ElasticsearchClient unwrap() {
        return client;
    }
}
