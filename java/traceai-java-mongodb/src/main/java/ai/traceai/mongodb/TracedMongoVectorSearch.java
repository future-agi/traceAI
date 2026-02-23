package ai.traceai.mongodb;

import ai.traceai.*;
import com.mongodb.client.MongoCollection;
import com.mongodb.client.AggregateIterable;
import com.mongodb.client.model.Aggregates;
import com.mongodb.client.model.search.VectorSearchOptions;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.context.Scope;
import org.bson.Document;
import org.bson.conversions.Bson;

import java.util.ArrayList;
import java.util.List;

/**
 * Instrumentation wrapper for MongoDB Atlas Vector Search.
 * Provides tracing for vector search operations using MongoDB's $vectorSearch aggregation.
 *
 * <p>Usage:</p>
 * <pre>
 * MongoCollection&lt;Document&gt; collection = database.getCollection("embeddings");
 * TracedMongoVectorSearch traced = new TracedMongoVectorSearch(collection, "embeddings");
 *
 * List&lt;Document&gt; results = traced.vectorSearch(
 *     queryVector, "embedding", "vector_index", 10, 100);
 * </pre>
 */
public class TracedMongoVectorSearch {

    private final MongoCollection<Document> collection;
    private final FITracer tracer;
    private final String collectionName;

    /**
     * Creates a new traced MongoDB vector search with the given collection and tracer.
     *
     * @param collection     the MongoCollection to wrap
     * @param tracer         the FITracer for instrumentation
     * @param collectionName the collection name for tracing
     */
    public TracedMongoVectorSearch(MongoCollection<Document> collection, FITracer tracer, String collectionName) {
        this.collection = collection;
        this.tracer = tracer;
        this.collectionName = collectionName;
    }

    /**
     * Creates a new traced MongoDB vector search using the global TraceAI tracer.
     *
     * @param collection     the MongoCollection to wrap
     * @param collectionName the collection name
     */
    public TracedMongoVectorSearch(MongoCollection<Document> collection, String collectionName) {
        this(collection, TraceAI.getTracer(), collectionName);
    }

    /**
     * Performs a vector search with tracing.
     *
     * @param queryVector  the query vector
     * @param path         the field path containing embeddings
     * @param indexName    the vector search index name
     * @param limit        maximum number of results
     * @param numCandidates number of candidates to consider
     * @return list of matching documents
     */
    public List<Document> vectorSearch(
            List<Double> queryVector,
            String path,
            String indexName,
            int limit,
            int numCandidates) {
        Span span = tracer.startSpan("MongoDB Vector Search", FISpanKind.RETRIEVER);

        try (Scope scope = span.makeCurrent()) {
            // Set attributes
            span.setAttribute(SemanticConventions.LLM_SYSTEM, "mongodb");
            span.setAttribute("mongodb.collection", collectionName);
            span.setAttribute("mongodb.index", indexName);
            span.setAttribute(SemanticConventions.RETRIEVER_TOP_K, (long) limit);
            span.setAttribute("mongodb.num_candidates", (long) numCandidates);
            span.setAttribute(SemanticConventions.EMBEDDING_DIMENSIONS, (long) queryVector.size());
            span.setAttribute("mongodb.path", path);

            // Build vector search pipeline
            List<Bson> pipeline = new ArrayList<>();

            Document vectorSearchStage = new Document("$vectorSearch",
                new Document("index", indexName)
                    .append("path", path)
                    .append("queryVector", queryVector)
                    .append("numCandidates", numCandidates)
                    .append("limit", limit)
            );
            pipeline.add(vectorSearchStage);

            // Add score to results
            Document addFieldsStage = new Document("$addFields",
                new Document("score", new Document("$meta", "vectorSearchScore"))
            );
            pipeline.add(addFieldsStage);

            // Execute search
            AggregateIterable<Document> results = collection.aggregate(pipeline);

            List<Document> documents = new ArrayList<>();
            for (Document doc : results) {
                documents.add(doc);
            }

            // Capture results
            span.setAttribute("mongodb.results_count", (long) documents.size());

            // Capture top score if available
            if (!documents.isEmpty() && documents.get(0).containsKey("score")) {
                Object score = documents.get(0).get("score");
                if (score instanceof Number) {
                    span.setAttribute("mongodb.top_score", ((Number) score).doubleValue());
                }
            }

            span.setStatus(StatusCode.OK);
            return documents;

        } catch (Exception e) {
            tracer.setError(span, e);
            throw new RuntimeException("MongoDB vector search failed", e);
        } finally {
            span.end();
        }
    }

    /**
     * Performs a vector search with filter and tracing.
     *
     * @param queryVector   the query vector
     * @param path          the field path containing embeddings
     * @param indexName     the vector search index name
     * @param limit         maximum number of results
     * @param numCandidates number of candidates to consider
     * @param filter        pre-filter document (optional)
     * @return list of matching documents
     */
    public List<Document> vectorSearch(
            List<Double> queryVector,
            String path,
            String indexName,
            int limit,
            int numCandidates,
            Document filter) {
        Span span = tracer.startSpan("MongoDB Vector Search", FISpanKind.RETRIEVER);

        try (Scope scope = span.makeCurrent()) {
            // Set attributes
            span.setAttribute(SemanticConventions.LLM_SYSTEM, "mongodb");
            span.setAttribute("mongodb.collection", collectionName);
            span.setAttribute("mongodb.index", indexName);
            span.setAttribute(SemanticConventions.RETRIEVER_TOP_K, (long) limit);
            span.setAttribute("mongodb.num_candidates", (long) numCandidates);
            span.setAttribute(SemanticConventions.EMBEDDING_DIMENSIONS, (long) queryVector.size());

            if (filter != null) {
                span.setAttribute("mongodb.has_filter", true);
            }

            // Build vector search pipeline
            List<Bson> pipeline = new ArrayList<>();

            Document vectorSearchDoc = new Document("index", indexName)
                .append("path", path)
                .append("queryVector", queryVector)
                .append("numCandidates", numCandidates)
                .append("limit", limit);

            if (filter != null) {
                vectorSearchDoc.append("filter", filter);
            }

            Document vectorSearchStage = new Document("$vectorSearch", vectorSearchDoc);
            pipeline.add(vectorSearchStage);

            // Add score to results
            Document addFieldsStage = new Document("$addFields",
                new Document("score", new Document("$meta", "vectorSearchScore"))
            );
            pipeline.add(addFieldsStage);

            // Execute search
            AggregateIterable<Document> results = collection.aggregate(pipeline);

            List<Document> documents = new ArrayList<>();
            for (Document doc : results) {
                documents.add(doc);
            }

            // Capture results
            span.setAttribute("mongodb.results_count", (long) documents.size());

            span.setStatus(StatusCode.OK);
            return documents;

        } catch (Exception e) {
            tracer.setError(span, e);
            throw new RuntimeException("MongoDB vector search failed", e);
        } finally {
            span.end();
        }
    }

    /**
     * Inserts a document with embedding with tracing.
     *
     * @param document the document to insert
     */
    public void insertOne(Document document) {
        Span span = tracer.startSpan("MongoDB Insert", FISpanKind.EMBEDDING);

        try (Scope scope = span.makeCurrent()) {
            span.setAttribute(SemanticConventions.LLM_SYSTEM, "mongodb");
            span.setAttribute("mongodb.collection", collectionName);

            collection.insertOne(document);

            span.setStatus(StatusCode.OK);

        } catch (Exception e) {
            tracer.setError(span, e);
            throw new RuntimeException("MongoDB insert failed", e);
        } finally {
            span.end();
        }
    }

    /**
     * Inserts multiple documents with tracing.
     *
     * @param documents the documents to insert
     */
    public void insertMany(List<Document> documents) {
        Span span = tracer.startSpan("MongoDB Insert Many", FISpanKind.EMBEDDING);

        try (Scope scope = span.makeCurrent()) {
            span.setAttribute(SemanticConventions.LLM_SYSTEM, "mongodb");
            span.setAttribute("mongodb.collection", collectionName);
            span.setAttribute("mongodb.insert_count", (long) documents.size());

            collection.insertMany(documents);

            span.setStatus(StatusCode.OK);

        } catch (Exception e) {
            tracer.setError(span, e);
            throw new RuntimeException("MongoDB insert many failed", e);
        } finally {
            span.end();
        }
    }

    /**
     * Deletes documents matching a filter with tracing.
     *
     * @param filter the filter document
     * @return number of deleted documents
     */
    public long deleteMany(Document filter) {
        Span span = tracer.startSpan("MongoDB Delete", FISpanKind.RETRIEVER);

        try (Scope scope = span.makeCurrent()) {
            span.setAttribute(SemanticConventions.LLM_SYSTEM, "mongodb");
            span.setAttribute("mongodb.collection", collectionName);

            long deletedCount = collection.deleteMany(filter).getDeletedCount();

            span.setAttribute("mongodb.deleted_count", deletedCount);
            span.setStatus(StatusCode.OK);
            return deletedCount;

        } catch (Exception e) {
            tracer.setError(span, e);
            throw new RuntimeException("MongoDB delete failed", e);
        } finally {
            span.end();
        }
    }

    /**
     * Gets the underlying MongoCollection.
     *
     * @return the wrapped MongoCollection
     */
    public MongoCollection<Document> unwrap() {
        return collection;
    }
}
