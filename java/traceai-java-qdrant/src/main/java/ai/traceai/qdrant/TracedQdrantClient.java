package ai.traceai.qdrant;

import ai.traceai.*;
import io.qdrant.client.QdrantClient;
import io.qdrant.client.grpc.Points.*;
import io.qdrant.client.grpc.Collections.*;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.context.Scope;

import java.util.List;
import java.util.concurrent.ExecutionException;

/**
 * Instrumentation wrapper for Qdrant Java client.
 * Wraps the QdrantClient to provide automatic tracing of all vector operations.
 *
 * <p>Usage:</p>
 * <pre>
 * QdrantClient client = new QdrantClient(
 *     QdrantGrpcClient.newBuilder("localhost", 6334, false).build());
 * TracedQdrantClient traced = new TracedQdrantClient(client);
 *
 * List&lt;ScoredPoint&gt; results = traced.search("my-collection", queryVector, 10);
 * </pre>
 */
public class TracedQdrantClient {

    private final QdrantClient client;
    private final FITracer tracer;

    /**
     * Creates a new traced Qdrant client with the given client and tracer.
     *
     * @param client the QdrantClient to wrap
     * @param tracer the FITracer for instrumentation
     */
    public TracedQdrantClient(QdrantClient client, FITracer tracer) {
        this.client = client;
        this.tracer = tracer;
    }

    /**
     * Creates a new traced Qdrant client using the global TraceAI tracer.
     *
     * @param client the QdrantClient to wrap
     */
    public TracedQdrantClient(QdrantClient client) {
        this(client, TraceAI.getTracer());
    }

    /**
     * Searches for similar vectors with tracing.
     *
     * @param collectionName the collection to search
     * @param queryVector    the query vector
     * @param limit          maximum number of results
     * @return list of scored points
     * @throws ExecutionException   if search fails
     * @throws InterruptedException if interrupted
     */
    public List<ScoredPoint> search(String collectionName, List<Float> queryVector, int limit)
            throws ExecutionException, InterruptedException {
        Span span = tracer.startSpan("Qdrant Search", FISpanKind.RETRIEVER);

        try (Scope scope = span.makeCurrent()) {
            // Set attributes
            span.setAttribute(SemanticConventions.LLM_SYSTEM, "qdrant");
            span.setAttribute("qdrant.collection", collectionName);
            span.setAttribute(SemanticConventions.RETRIEVER_TOP_K, (long) limit);
            span.setAttribute(SemanticConventions.EMBEDDING_DIMENSIONS, (long) queryVector.size());

            // Execute search
            List<ScoredPoint> results = client.searchAsync(
                collectionName,
                queryVector,
                limit
            ).get();

            // Capture results
            span.setAttribute("qdrant.results_count", (long) results.size());
            if (!results.isEmpty()) {
                span.setAttribute("qdrant.top_score", results.get(0).getScore());
            }

            span.setStatus(StatusCode.OK);
            return results;

        } catch (Exception e) {
            tracer.setError(span, e);
            throw e;
        } finally {
            span.end();
        }
    }

    /**
     * Searches with filter and additional options with tracing.
     *
     * @param searchRequest the search request
     * @return list of scored points
     * @throws ExecutionException   if search fails
     * @throws InterruptedException if interrupted
     */
    public List<ScoredPoint> search(SearchPoints searchRequest)
            throws ExecutionException, InterruptedException {
        Span span = tracer.startSpan("Qdrant Search", FISpanKind.RETRIEVER);

        try (Scope scope = span.makeCurrent()) {
            // Set attributes
            span.setAttribute(SemanticConventions.LLM_SYSTEM, "qdrant");
            span.setAttribute("qdrant.collection", searchRequest.getCollectionName());
            span.setAttribute(SemanticConventions.RETRIEVER_TOP_K, searchRequest.getLimit());

            if (searchRequest.hasVector()) {
                span.setAttribute(SemanticConventions.EMBEDDING_DIMENSIONS,
                    (long) searchRequest.getVector().getDataCount());
            }

            if (searchRequest.hasFilter()) {
                span.setAttribute("qdrant.has_filter", true);
            }

            // Execute search
            List<ScoredPoint> results = client.searchAsync(searchRequest).get();

            // Capture results
            span.setAttribute("qdrant.results_count", (long) results.size());
            if (!results.isEmpty()) {
                span.setAttribute("qdrant.top_score", results.get(0).getScore());
            }

            span.setStatus(StatusCode.OK);
            return results;

        } catch (Exception e) {
            tracer.setError(span, e);
            throw e;
        } finally {
            span.end();
        }
    }

    /**
     * Upserts points with tracing.
     *
     * @param collectionName the collection name
     * @param points         the points to upsert
     * @return the update result
     * @throws ExecutionException   if upsert fails
     * @throws InterruptedException if interrupted
     */
    public UpdateResult upsert(String collectionName, List<PointStruct> points)
            throws ExecutionException, InterruptedException {
        Span span = tracer.startSpan("Qdrant Upsert", FISpanKind.EMBEDDING);

        try (Scope scope = span.makeCurrent()) {
            // Set attributes
            span.setAttribute(SemanticConventions.LLM_SYSTEM, "qdrant");
            span.setAttribute("qdrant.collection", collectionName);
            span.setAttribute("qdrant.points_count", (long) points.size());

            if (!points.isEmpty() && points.get(0).hasVectors()) {
                VectorsSelector vectors = points.get(0).getVectors();
                if (vectors.hasVector()) {
                    span.setAttribute(SemanticConventions.EMBEDDING_DIMENSIONS,
                        (long) vectors.getVector().getDataCount());
                }
            }

            // Execute upsert
            UpdateResult result = client.upsertAsync(collectionName, points).get();

            // Capture result
            span.setAttribute("qdrant.status", result.getStatus().name());

            span.setStatus(StatusCode.OK);
            return result;

        } catch (Exception e) {
            tracer.setError(span, e);
            throw e;
        } finally {
            span.end();
        }
    }

    /**
     * Deletes points by IDs with tracing.
     *
     * @param collectionName the collection name
     * @param pointIds       the point IDs to delete
     * @return the update result
     * @throws ExecutionException   if delete fails
     * @throws InterruptedException if interrupted
     */
    public UpdateResult delete(String collectionName, List<PointId> pointIds)
            throws ExecutionException, InterruptedException {
        Span span = tracer.startSpan("Qdrant Delete", FISpanKind.RETRIEVER);

        try (Scope scope = span.makeCurrent()) {
            // Set attributes
            span.setAttribute(SemanticConventions.LLM_SYSTEM, "qdrant");
            span.setAttribute("qdrant.collection", collectionName);
            span.setAttribute("qdrant.delete_count", (long) pointIds.size());

            // Execute delete
            UpdateResult result = client.deleteAsync(collectionName, pointIds).get();

            span.setAttribute("qdrant.status", result.getStatus().name());
            span.setStatus(StatusCode.OK);
            return result;

        } catch (Exception e) {
            tracer.setError(span, e);
            throw e;
        } finally {
            span.end();
        }
    }

    /**
     * Gets points by IDs with tracing.
     *
     * @param collectionName the collection name
     * @param pointIds       the point IDs to retrieve
     * @param withPayload    whether to include payload
     * @param withVectors    whether to include vectors
     * @return list of retrieved points
     * @throws ExecutionException   if retrieval fails
     * @throws InterruptedException if interrupted
     */
    public List<RetrievedPoint> get(String collectionName, List<PointId> pointIds,
                                     boolean withPayload, boolean withVectors)
            throws ExecutionException, InterruptedException {
        Span span = tracer.startSpan("Qdrant Get", FISpanKind.RETRIEVER);

        try (Scope scope = span.makeCurrent()) {
            // Set attributes
            span.setAttribute(SemanticConventions.LLM_SYSTEM, "qdrant");
            span.setAttribute("qdrant.collection", collectionName);
            span.setAttribute("qdrant.requested_count", (long) pointIds.size());

            // Execute get
            List<RetrievedPoint> results = client.retrieveAsync(
                collectionName,
                pointIds,
                withPayload,
                withVectors,
                null  // readConsistency
            ).get();

            span.setAttribute("qdrant.retrieved_count", (long) results.size());
            span.setStatus(StatusCode.OK);
            return results;

        } catch (Exception e) {
            tracer.setError(span, e);
            throw e;
        } finally {
            span.end();
        }
    }

    /**
     * Creates a collection with tracing.
     *
     * @param collectionName the collection name
     * @param vectorSize     the vector dimensions
     * @param distance       the distance metric
     * @throws ExecutionException   if creation fails
     * @throws InterruptedException if interrupted
     */
    public void createCollection(String collectionName, int vectorSize, Distance distance)
            throws ExecutionException, InterruptedException {
        Span span = tracer.startSpan("Qdrant Create Collection", FISpanKind.RETRIEVER);

        try (Scope scope = span.makeCurrent()) {
            // Set attributes
            span.setAttribute(SemanticConventions.LLM_SYSTEM, "qdrant");
            span.setAttribute("qdrant.collection", collectionName);
            span.setAttribute("qdrant.vector_size", (long) vectorSize);
            span.setAttribute("qdrant.distance", distance.name());

            // Execute create
            client.createCollectionAsync(
                collectionName,
                VectorParams.newBuilder()
                    .setSize(vectorSize)
                    .setDistance(distance)
                    .build()
            ).get();

            span.setStatus(StatusCode.OK);

        } catch (Exception e) {
            tracer.setError(span, e);
            throw e;
        } finally {
            span.end();
        }
    }

    /**
     * Lists collections with tracing.
     *
     * @return list of collection names
     * @throws ExecutionException   if listing fails
     * @throws InterruptedException if interrupted
     */
    public List<String> listCollections() throws ExecutionException, InterruptedException {
        Span span = tracer.startSpan("Qdrant List Collections", FISpanKind.RETRIEVER);

        try (Scope scope = span.makeCurrent()) {
            span.setAttribute(SemanticConventions.LLM_SYSTEM, "qdrant");

            List<String> collections = client.listCollectionsAsync().get();

            span.setAttribute("qdrant.collections_count", (long) collections.size());
            span.setStatus(StatusCode.OK);
            return collections;

        } catch (Exception e) {
            tracer.setError(span, e);
            throw e;
        } finally {
            span.end();
        }
    }

    /**
     * Gets the underlying QdrantClient.
     *
     * @return the wrapped QdrantClient
     */
    public QdrantClient unwrap() {
        return client;
    }
}
