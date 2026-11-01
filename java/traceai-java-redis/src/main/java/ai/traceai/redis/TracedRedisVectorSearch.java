package ai.traceai.redis;

import ai.traceai.FISpanKind;
import ai.traceai.FITracer;
import ai.traceai.SemanticConventions;
import ai.traceai.TraceAI;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.context.Scope;
import redis.clients.jedis.JedisPooled;
import redis.clients.jedis.search.Document;
import redis.clients.jedis.search.FTCreateParams;
import redis.clients.jedis.search.Query;
import redis.clients.jedis.search.RediSearchUtil;
import redis.clients.jedis.search.SearchResult;
import redis.clients.jedis.search.schemafields.VectorField;

import java.nio.charset.StandardCharsets;
import java.util.HashMap;
import java.util.HashSet;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Objects;
import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;

/**
 * Application-level tracing for Redis Search using Jedis 5.1.x or 5.2.x.
 * Jedis is a provided dependency: applications must supply a supported 5.x version.
 * Jedis 8 is not supported because it removes {@code JedisPooled}.
 *
 * <p>The convenience methods use FLOAT32 vectors and HASH indexes with the
 * {@code doc:} key prefix. Use {@link #search(String, Query)} for an existing
 * Jedis query, including queries with other schema or return-field requirements.</p>
 */
public class TracedRedisVectorSearch {
    private static final String DEFAULT_VECTOR_FIELD = "embedding";

    private final JedisPooled jedis;
    private final FITracer tracer;
    // Only indexes successfully created by this wrapper are known locally.
    private final Map<String, String> indexVectorFields = new ConcurrentHashMap<>();

    /**
     * @param jedis the Jedis 5.1.x or 5.2.x pooled client to wrap
     * @param tracer the FITracer for instrumentation
     */
    public TracedRedisVectorSearch(JedisPooled jedis, FITracer tracer) {
        this.jedis = Objects.requireNonNull(jedis, "jedis");
        this.tracer = Objects.requireNonNull(tracer, "tracer");
    }

    /**
     * Creates a wrapper using the global TraceAI tracer.
     * @param jedis the Jedis 5.1.x or 5.2.x pooled client to wrap
     */
    public TracedRedisVectorSearch(JedisPooled jedis) {
        this(jedis, TraceAI.getTracer());
    }

    /**
     * Creates a FLOAT32 vector index for HASH documents with the {@code doc:} prefix.
     * The field is remembered for subsequent convenience searches on this index.
     *
     * @param indexName the index name
     * @param vectorField the vector field name
     * @param dimensions the positive vector dimension
     * @param distanceMetric COSINE, L2 or IP (case insensitive)
     * @param algorithm FLAT or HNSW (case insensitive)
     */
    public void createIndex(String indexName, String vectorField, int dimensions,
                            String distanceMetric, String algorithm) {
        requireName(indexName, "indexName");
        requireName(vectorField, "vectorField");
        if (dimensions <= 0) {
            throw new IllegalArgumentException("dimensions must be positive");
        }
        String metric = normalizeChoice(distanceMetric, "distanceMetric", Set.of("COSINE", "L2", "IP"));
        String method = normalizeChoice(algorithm, "algorithm", Set.of("FLAT", "HNSW"));
        Span span = tracer.startSpan("Redis Create Index", FISpanKind.VECTOR_DB);
        try (Scope scope = span.makeCurrent()) {
            setIndexAttributes(span, indexName);
            span.setAttribute("redis.vector_field", vectorField);
            span.setAttribute(SemanticConventions.EMBEDDING_DIMENSIONS, (long) dimensions);
            span.setAttribute("redis.distance_metric", metric);
            span.setAttribute("redis.algorithm", method);

            VectorField field = VectorField.builder()
                    .fieldName(vectorField)
                    .algorithm(VectorField.VectorAlgorithm.valueOf(method))
                    .attributes(Map.of("TYPE", "FLOAT32", "DIM", dimensions, "DISTANCE_METRIC", metric))
                    .build();
            jedis.ftCreate(indexName, FTCreateParams.createParams().prefix("doc:"), List.of(field));
            indexVectorFields.put(indexName, vectorField);
            span.setStatus(StatusCode.OK);
        } catch (Exception e) {
            tracer.setError(span, e);
            throw new RuntimeException("Redis create index failed", e);
        } finally {
            span.end();
        }
    }

    /**
     * Searches the field recorded by {@link #createIndex}, or {@code embedding}
     * for indexes created outside this wrapper.
     * @param indexName the index name
     * @param queryVector the FLOAT32 query vector
     * @param topK positive maximum number of returned documents
     * @return the original Jedis result
     */
    public SearchResult vectorSearch(String indexName, float[] queryVector, int topK) {
        return vectorSearch(indexName, queryVector, topK, null);
    }

    /**
     * Searches with an optional Redis Search filter expression.
     * @param indexName the index name
     * @param queryVector the FLOAT32 query vector
     * @param topK positive maximum number of returned documents
     * @param filter native Redis Search expression, or null/blank for no filter
     * @return the original Jedis result
     */
    public SearchResult vectorSearch(String indexName, float[] queryVector, int topK, String filter) {
        requireName(indexName, "indexName");
        return vectorSearch(indexName, indexVectorFields.getOrDefault(indexName, DEFAULT_VECTOR_FIELD),
                queryVector, topK, filter);
    }

    /**
     * Searches an explicit vector field, including on an externally created index.
     * @param indexName the index name
     * @param vectorField the vector field name
     * @param queryVector the FLOAT32 query vector
     * @param topK positive maximum number of returned documents
     * @return the original Jedis result
     */
    public SearchResult vectorSearch(String indexName, String vectorField, float[] queryVector, int topK) {
        return vectorSearch(indexName, vectorField, queryVector, topK, null);
    }

    /**
     * Searches an explicit vector field with an optional native filter expression.
     * @param indexName the index name
     * @param vectorField the vector field name
     * @param queryVector the FLOAT32 query vector
     * @param topK positive maximum number of returned documents
     * @param filter native Redis Search expression, or null/blank for no filter
     * @return the original Jedis result
     */
    public SearchResult vectorSearch(String indexName, String vectorField, float[] queryVector,
                                     int topK, String filter) {
        requireName(indexName, "indexName");
        requireName(vectorField, "vectorField");
        requireVector(queryVector);
        if (topK <= 0) {
            throw new IllegalArgumentException("topK must be positive");
        }
        String expression = filter == null || filter.isBlank() ? "*" : "(" + filter + ")";
        String escapedField = RediSearchUtil.escapeQuery(vectorField.replace("\\", "\\\\"));
        Query query = new Query(expression + "=>[KNN " + topK + " @" + escapedField + " $BLOB AS score]")
                .addParam("BLOB", RediSearchUtil.toByteArray(queryVector))
                .setSortBy("score", true)
                .returnFields("score")
                .limit(0, topK)
                .dialect(2);
        return executeSearch(indexName, query, vectorField, queryVector.length, topK, filter);
    }

    /**
     * Traces an existing Jedis query without rewriting its fields, filters, paging,
     * parameters or dialect. The query text and parameters are not added to spans.
     * @param indexName the index name
     * @param query the caller-owned Jedis query
     * @return the original Jedis result
     */
    public SearchResult search(String indexName, Query query) {
        requireName(indexName, "indexName");
        Objects.requireNonNull(query, "query");
        return executeSearch(indexName, query, null, 0, 0, null);
    }

    private SearchResult executeSearch(String indexName, Query query, String vectorField,
                                        int dimensions, int topK, String filter) {
        boolean vectorSearch = vectorField != null;
        Span span = tracer.startSpan(vectorSearch ? "Redis Vector Search" : "Redis Search",
                vectorSearch ? FISpanKind.RETRIEVER : FISpanKind.UNKNOWN);
        try (Scope scope = span.makeCurrent()) {
            if (vectorSearch) {
                setIndexAttributes(span, indexName);
                span.setAttribute("redis.vector_field", vectorField);
                span.setAttribute(SemanticConventions.RETRIEVER_TOP_K, (long) topK);
                span.setAttribute(SemanticConventions.EMBEDDING_DIMENSIONS, (long) dimensions);
                if (filter != null && !tracer.getConfig().isHideInputs()) {
                    span.setAttribute("redis.filter", filter);
                }
            } else {
                span.setAttribute(SemanticConventions.DB_SYSTEM, "redis");
                span.setAttribute("redis.index_name", indexName);
            }
            SearchResult result = jedis.ftSearch(indexName, query);
            span.setAttribute(vectorSearch ? SemanticConventions.DB_VECTOR_RESULTS_COUNT : "redis.results.count",
                    (long) result.getDocuments().size());
            if (vectorSearch && !result.getDocuments().isEmpty()) {
                Document firstDoc = result.getDocuments().get(0);
                Object score = firstDoc.get("score");
                if (score != null) {
                    try {
                        span.setAttribute("redis.top_score", Double.parseDouble(score.toString()));
                    } catch (NumberFormatException ignored) {
                        // Record the telemetry failure without exposing the score's contents.
                        span.addEvent("redis.invalid_score");
                    }
                }
            }
            span.setStatus(StatusCode.OK);
            return result;
        } catch (Exception e) {
            tracer.setError(span, e);
            throw new RuntimeException(vectorSearch ? "Redis vector search failed" : "Redis search failed", e);
        } finally {
            span.end();
        }
    }

    /**
     * Adds a HASH document using the sole vector field known to this wrapper, or
     * {@code embedding} if no fields are known or different indexes use different fields.
     * This preserves the legacy embedding write when the index is ambiguous; use the
     * explicit-field overload to write to a particular custom index.
     * @param key the document key (use the {@code doc:} prefix for convenience indexes)
     * @param vector the FLOAT32 vector
     * @param metadata additional HASH fields; must not overwrite the vector field
     */
    public void addDocument(String key, float[] vector, Map<String, String> metadata) {
        Set<String> fields = new HashSet<>(indexVectorFields.values());
        addDocument(key, fields.size() == 1 ? fields.iterator().next() : DEFAULT_VECTOR_FIELD,
                vector, metadata);
    }

    /**
     * Adds a HASH document with an explicit vector field.
     * @param key the document key
     * @param vectorField the vector field name
     * @param vector the FLOAT32 vector
     * @param metadata additional HASH fields; must not overwrite the vector field
     */
    public void addDocument(String key, String vectorField, float[] vector, Map<String, String> metadata) {
        requireName(key, "key");
        requireName(vectorField, "vectorField");
        requireVector(vector);
        if (metadata != null && metadata.containsKey(vectorField)) {
            throw new IllegalArgumentException("metadata must not overwrite vector field " + vectorField);
        }
        Span span = tracer.startSpan("Redis Add Document", FISpanKind.VECTOR_DB);
        try (Scope scope = span.makeCurrent()) {
            span.setAttribute(SemanticConventions.DB_SYSTEM, "redis");
            if (!tracer.getConfig().isHideInputs()) {
                span.setAttribute("redis.key", key);
            }
            span.setAttribute("redis.vector_field", vectorField);
            span.setAttribute(SemanticConventions.EMBEDDING_DIMENSIONS, (long) vector.length);
            Map<byte[], byte[]> hash = new HashMap<>();
            hash.put(vectorField.getBytes(StandardCharsets.UTF_8), RediSearchUtil.toByteArray(vector));
            if (metadata != null) {
                for (Map.Entry<String, String> entry : metadata.entrySet()) {
                    hash.put(entry.getKey().getBytes(StandardCharsets.UTF_8),
                            entry.getValue().getBytes(StandardCharsets.UTF_8));
                }
            }
            jedis.hset(key.getBytes(StandardCharsets.UTF_8), hash);
            span.setStatus(StatusCode.OK);
        } catch (Exception e) {
            tracer.setError(span, e);
            throw new RuntimeException("Redis add document failed", e);
        } finally {
            span.end();
        }
    }

    /**
     * Deletes a document with tracing.
     * @param key the document key
     */
    public void deleteDocument(String key) {
        Span span = tracer.startSpan("Redis Delete Document", FISpanKind.VECTOR_DB);
        try (Scope scope = span.makeCurrent()) {
            span.setAttribute(SemanticConventions.DB_SYSTEM, "redis");
            if (!tracer.getConfig().isHideInputs()) {
                span.setAttribute("redis.key", key);
            }
            jedis.del(key);
            span.setStatus(StatusCode.OK);
        } catch (Exception e) {
            tracer.setError(span, e);
            throw new RuntimeException("Redis delete document failed", e);
        } finally {
            span.end();
        }
    }

    /**
     * @return the wrapped JedisPooled client; the application owns its lifecycle
     */
    public JedisPooled unwrap() {
        return jedis;
    }

    private void setIndexAttributes(Span span, String indexName) {
        span.setAttribute(SemanticConventions.DB_SYSTEM, "redis");
        span.setAttribute(SemanticConventions.DB_VECTOR_INDEX_NAME, indexName);
    }

    private static void requireName(String name, String label) {
        if (name == null || name.isBlank()) {
            throw new IllegalArgumentException(label + " must not be blank");
        }
    }

    private static void requireVector(float[] vector) {
        if (vector == null || vector.length == 0) {
            throw new IllegalArgumentException("vector must not be null or empty");
        }
        for (float value : vector) {
            if (!Float.isFinite(value)) {
                throw new IllegalArgumentException("vector must contain finite values");
            }
        }
    }

    private static String normalizeChoice(String value, String label, Set<String> choices) {
        if (value == null || !choices.contains(value.toUpperCase(Locale.ROOT))) {
            throw new IllegalArgumentException(label + " must be one of " + choices);
        }
        return value.toUpperCase(Locale.ROOT);
    }
}
