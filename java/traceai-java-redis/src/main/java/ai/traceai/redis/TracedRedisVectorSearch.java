package ai.traceai.redis;

import ai.traceai.*;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.context.Scope;
import redis.clients.jedis.JedisPooled;
import redis.clients.jedis.search.*;
import redis.clients.jedis.search.schemafields.*;

import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

/**
 * Instrumentation wrapper for Redis Vector Search (RediSearch).
 * Provides tracing for vector search operations using Jedis.
 *
 * <p>Usage:</p>
 * <pre>
 * JedisPooled jedis = new JedisPooled("localhost", 6379);
 * TracedRedisVectorSearch traced = new TracedRedisVectorSearch(jedis);
 *
 * SearchResult results = traced.vectorSearch("my_index", queryVector, 10);
 * </pre>
 */
public class TracedRedisVectorSearch {

    private final JedisPooled jedis;
    private final FITracer tracer;

    /**
     * Creates a new traced Redis vector search with the given Jedis client and tracer.
     *
     * @param jedis  the JedisPooled client to wrap
     * @param tracer the FITracer for instrumentation
     */
    public TracedRedisVectorSearch(JedisPooled jedis, FITracer tracer) {
        this.jedis = jedis;
        this.tracer = tracer;
    }

    /**
     * Creates a new traced Redis vector search using the global TraceAI tracer.
     *
     * @param jedis the JedisPooled client to wrap
     */
    public TracedRedisVectorSearch(JedisPooled jedis) {
        this(jedis, TraceAI.getTracer());
    }

    /**
     * Creates a vector search index with tracing.
     *
     * @param indexName    the index name
     * @param vectorField  the vector field name
     * @param dimensions   the vector dimensions
     * @param distanceMetric the distance metric (COSINE, L2, IP)
     * @param algorithm    the index algorithm (FLAT, HNSW)
     */
    public void createIndex(
            String indexName,
            String vectorField,
            int dimensions,
            String distanceMetric,
            String algorithm) {
        Span span = tracer.startSpan("Redis Create Index", FISpanKind.RETRIEVER);

        try (Scope scope = span.makeCurrent()) {
            // Set attributes
            span.setAttribute(SemanticConventions.LLM_SYSTEM, "redis");
            span.setAttribute("redis.index", indexName);
            span.setAttribute("redis.vector_field", vectorField);
            span.setAttribute(SemanticConventions.EMBEDDING_DIMENSIONS, (long) dimensions);
            span.setAttribute("redis.distance_metric", distanceMetric);
            span.setAttribute("redis.algorithm", algorithm);

            // Build schema
            Map<String, Object> vectorAttrs = new HashMap<>();
            vectorAttrs.put("TYPE", "FLOAT32");
            vectorAttrs.put("DIM", dimensions);
            vectorAttrs.put("DISTANCE_METRIC", distanceMetric);

            Schema schema = new Schema()
                .addField(new VectorField(vectorField,
                    algorithm.equals("HNSW") ? VectorField.VectorAlgorithm.HNSW : VectorField.VectorAlgorithm.FLAT,
                    vectorAttrs));

            // Create index
            jedis.ftCreate(indexName,
                IndexOptions.defaultOptions().setPrefix("doc:"),
                schema);

            span.setStatus(StatusCode.OK);

        } catch (Exception e) {
            tracer.setError(span, e);
            throw new RuntimeException("Redis create index failed", e);
        } finally {
            span.end();
        }
    }

    /**
     * Performs a vector search with tracing.
     *
     * @param indexName   the index name
     * @param queryVector the query vector
     * @param topK        maximum number of results
     * @return the search results
     */
    public SearchResult vectorSearch(String indexName, float[] queryVector, int topK) {
        Span span = tracer.startSpan("Redis Vector Search", FISpanKind.RETRIEVER);

        try (Scope scope = span.makeCurrent()) {
            // Set attributes
            span.setAttribute(SemanticConventions.LLM_SYSTEM, "redis");
            span.setAttribute("redis.index", indexName);
            span.setAttribute(SemanticConventions.RETRIEVER_TOP_K, (long) topK);
            span.setAttribute(SemanticConventions.EMBEDDING_DIMENSIONS, (long) queryVector.length);

            // Convert vector to bytes
            byte[] vectorBytes = floatArrayToBytes(queryVector);

            // Build KNN query
            String queryStr = String.format("*=>[KNN %d @embedding $BLOB AS score]", topK);

            Query query = new Query(queryStr)
                .addParam("BLOB", vectorBytes)
                .setSortBy("score", true)
                .returnFields("score")
                .dialect(2);

            // Execute search
            SearchResult result = jedis.ftSearch(indexName, query);

            // Capture results
            span.setAttribute("redis.results_count", result.getTotalResults());

            // Capture top score if available
            if (!result.getDocuments().isEmpty()) {
                Document firstDoc = result.getDocuments().get(0);
                if (firstDoc.get("score") != null) {
                    span.setAttribute("redis.top_score",
                        Double.parseDouble(firstDoc.get("score").toString()));
                }
            }

            span.setStatus(StatusCode.OK);
            return result;

        } catch (Exception e) {
            tracer.setError(span, e);
            throw new RuntimeException("Redis vector search failed", e);
        } finally {
            span.end();
        }
    }

    /**
     * Performs a vector search with filter and tracing.
     *
     * @param indexName   the index name
     * @param queryVector the query vector
     * @param topK        maximum number of results
     * @param filter      the filter expression (e.g., "@category:{tech}")
     * @return the search results
     */
    public SearchResult vectorSearch(String indexName, float[] queryVector, int topK, String filter) {
        Span span = tracer.startSpan("Redis Vector Search", FISpanKind.RETRIEVER);

        try (Scope scope = span.makeCurrent()) {
            // Set attributes
            span.setAttribute(SemanticConventions.LLM_SYSTEM, "redis");
            span.setAttribute("redis.index", indexName);
            span.setAttribute(SemanticConventions.RETRIEVER_TOP_K, (long) topK);
            span.setAttribute(SemanticConventions.EMBEDDING_DIMENSIONS, (long) queryVector.length);

            if (filter != null) {
                span.setAttribute("redis.filter", filter);
            }

            // Convert vector to bytes
            byte[] vectorBytes = floatArrayToBytes(queryVector);

            // Build KNN query with filter
            String queryStr = filter != null
                ? String.format("(%s)=>[KNN %d @embedding $BLOB AS score]", filter, topK)
                : String.format("*=>[KNN %d @embedding $BLOB AS score]", topK);

            Query query = new Query(queryStr)
                .addParam("BLOB", vectorBytes)
                .setSortBy("score", true)
                .returnFields("score")
                .dialect(2);

            // Execute search
            SearchResult result = jedis.ftSearch(indexName, query);

            // Capture results
            span.setAttribute("redis.results_count", result.getTotalResults());

            span.setStatus(StatusCode.OK);
            return result;

        } catch (Exception e) {
            tracer.setError(span, e);
            throw new RuntimeException("Redis vector search failed", e);
        } finally {
            span.end();
        }
    }

    /**
     * Adds a document with vector to Redis with tracing.
     *
     * @param key       the document key
     * @param vector    the embedding vector
     * @param metadata  additional metadata fields
     */
    public void addDocument(String key, float[] vector, Map<String, String> metadata) {
        Span span = tracer.startSpan("Redis Add Document", FISpanKind.EMBEDDING);

        try (Scope scope = span.makeCurrent()) {
            // Set attributes
            span.setAttribute(SemanticConventions.LLM_SYSTEM, "redis");
            span.setAttribute("redis.key", key);
            span.setAttribute(SemanticConventions.EMBEDDING_DIMENSIONS, (long) vector.length);

            // Convert vector to bytes
            byte[] vectorBytes = floatArrayToBytes(vector);

            // Build hash fields
            Map<byte[], byte[]> hash = new HashMap<>();
            hash.put("embedding".getBytes(), vectorBytes);

            if (metadata != null) {
                for (Map.Entry<String, String> entry : metadata.entrySet()) {
                    hash.put(entry.getKey().getBytes(), entry.getValue().getBytes());
                }
            }

            // Store document
            jedis.hset(key.getBytes(), hash);

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
     *
     * @param key the document key
     */
    public void deleteDocument(String key) {
        Span span = tracer.startSpan("Redis Delete Document", FISpanKind.RETRIEVER);

        try (Scope scope = span.makeCurrent()) {
            span.setAttribute(SemanticConventions.LLM_SYSTEM, "redis");
            span.setAttribute("redis.key", key);

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
     * Gets the underlying JedisPooled client.
     *
     * @return the wrapped JedisPooled
     */
    public JedisPooled unwrap() {
        return jedis;
    }

    /**
     * Converts a float array to bytes for Redis vector storage.
     */
    private byte[] floatArrayToBytes(float[] vector) {
        ByteBuffer buffer = ByteBuffer.allocate(vector.length * 4);
        buffer.order(ByteOrder.LITTLE_ENDIAN);
        for (float v : vector) {
            buffer.putFloat(v);
        }
        return buffer.array();
    }
}
