package ai.traceai.chromadb;

import ai.traceai.*;
import tech.amikos.chromadb.Collection;
import tech.amikos.chromadb.Client;
import tech.amikos.chromadb.embeddings.EmbeddingFunction;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.context.Scope;

import java.util.List;
import java.util.Map;

/**
 * Instrumentation wrapper for ChromaDB Collection.
 * Wraps the ChromaDB Collection to provide automatic tracing of all vector operations.
 *
 * <p>Usage:</p>
 * <pre>
 * Client client = new Client("http://localhost:8000");
 * Collection collection = client.getOrCreateCollection("my_collection", null, null, null);
 * TracedChromaCollection traced = new TracedChromaCollection(collection, "my_collection");
 *
 * Map&lt;String, Object&gt; results = traced.query(queryEmbeddings, 10, null, null, null);
 * </pre>
 */
public class TracedChromaCollection {

    private final Collection collection;
    private final FITracer tracer;
    private final String collectionName;

    /**
     * Creates a new traced ChromaDB collection with the given collection and tracer.
     *
     * @param collection     the Collection to wrap
     * @param tracer         the FITracer for instrumentation
     * @param collectionName the collection name for tracing
     */
    public TracedChromaCollection(Collection collection, FITracer tracer, String collectionName) {
        this.collection = collection;
        this.tracer = tracer;
        this.collectionName = collectionName;
    }

    /**
     * Creates a new traced ChromaDB collection using the global TraceAI tracer.
     *
     * @param collection     the Collection to wrap
     * @param collectionName the collection name
     */
    public TracedChromaCollection(Collection collection, String collectionName) {
        this(collection, TraceAI.getTracer(), collectionName);
    }

    /**
     * Queries the collection with embeddings and tracing.
     *
     * @param queryEmbeddings the query embeddings
     * @param nResults        number of results to return
     * @param where           metadata filter (optional)
     * @param whereDocument   document filter (optional)
     * @param include         fields to include (optional)
     * @return query results
     * @throws Exception if query fails
     */
    public Collection.QueryResponse query(
            List<List<Float>> queryEmbeddings,
            int nResults,
            Map<String, Object> where,
            Map<String, Object> whereDocument,
            List<String> include) throws Exception {
        Span span = tracer.startSpan("ChromaDB Query", FISpanKind.RETRIEVER);

        try (Scope scope = span.makeCurrent()) {
            // Set attributes
            span.setAttribute(SemanticConventions.LLM_SYSTEM, "chromadb");
            span.setAttribute("chromadb.collection", collectionName);
            span.setAttribute(SemanticConventions.RETRIEVER_TOP_K, (long) nResults);

            if (queryEmbeddings != null && !queryEmbeddings.isEmpty()) {
                span.setAttribute("chromadb.query_count", (long) queryEmbeddings.size());
                span.setAttribute(SemanticConventions.EMBEDDING_DIMENSIONS,
                    (long) queryEmbeddings.get(0).size());
            }

            if (where != null) {
                span.setAttribute("chromadb.has_where_filter", true);
            }
            if (whereDocument != null) {
                span.setAttribute("chromadb.has_where_document_filter", true);
            }

            // Execute query
            Collection.QueryResponse response = collection.query(
                queryEmbeddings,
                nResults,
                where,
                whereDocument,
                include
            );

            // Capture results
            if (response != null && response.getIds() != null && !response.getIds().isEmpty()) {
                span.setAttribute("chromadb.results_count", (long) response.getIds().get(0).size());

                // Capture top distance if available
                if (response.getDistances() != null && !response.getDistances().isEmpty()
                        && !response.getDistances().get(0).isEmpty()) {
                    span.setAttribute("chromadb.top_distance", response.getDistances().get(0).get(0));
                }
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
     * Queries the collection with text (using embedding function) and tracing.
     *
     * @param queryTexts the query texts
     * @param nResults   number of results to return
     * @param where      metadata filter (optional)
     * @param whereDocument document filter (optional)
     * @param include    fields to include (optional)
     * @return query results
     * @throws Exception if query fails
     */
    public Collection.QueryResponse queryWithText(
            List<String> queryTexts,
            int nResults,
            Map<String, Object> where,
            Map<String, Object> whereDocument,
            List<String> include) throws Exception {
        Span span = tracer.startSpan("ChromaDB Query (Text)", FISpanKind.RETRIEVER);

        try (Scope scope = span.makeCurrent()) {
            // Set attributes
            span.setAttribute(SemanticConventions.LLM_SYSTEM, "chromadb");
            span.setAttribute("chromadb.collection", collectionName);
            span.setAttribute(SemanticConventions.RETRIEVER_TOP_K, (long) nResults);

            if (queryTexts != null) {
                span.setAttribute("chromadb.query_count", (long) queryTexts.size());
                // Capture first query text
                if (!queryTexts.isEmpty()) {
                    tracer.setInputValue(span, queryTexts.get(0));
                }
            }

            // Execute query
            Collection.QueryResponse response = collection.query(
                queryTexts,
                nResults,
                where,
                whereDocument,
                include
            );

            // Capture results
            if (response != null && response.getIds() != null && !response.getIds().isEmpty()) {
                span.setAttribute("chromadb.results_count", (long) response.getIds().get(0).size());
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
     * Adds documents to the collection with tracing.
     *
     * @param embeddings the embeddings (optional if using embedding function)
     * @param metadatas  the metadata for each document (optional)
     * @param documents  the documents (optional)
     * @param ids        the IDs for each document
     * @throws Exception if add fails
     */
    public void add(
            List<List<Float>> embeddings,
            List<Map<String, Object>> metadatas,
            List<String> documents,
            List<String> ids) throws Exception {
        Span span = tracer.startSpan("ChromaDB Add", FISpanKind.EMBEDDING);

        try (Scope scope = span.makeCurrent()) {
            // Set attributes
            span.setAttribute(SemanticConventions.LLM_SYSTEM, "chromadb");
            span.setAttribute("chromadb.collection", collectionName);
            span.setAttribute("chromadb.add_count", (long) ids.size());

            if (embeddings != null && !embeddings.isEmpty()) {
                span.setAttribute(SemanticConventions.EMBEDDING_DIMENSIONS,
                    (long) embeddings.get(0).size());
            }

            // Execute add
            collection.add(embeddings, metadatas, documents, ids);

            span.setStatus(StatusCode.OK);

        } catch (Exception e) {
            tracer.setError(span, e);
            throw e;
        } finally {
            span.end();
        }
    }

    /**
     * Upserts documents to the collection with tracing.
     *
     * @param embeddings the embeddings (optional if using embedding function)
     * @param metadatas  the metadata for each document (optional)
     * @param documents  the documents (optional)
     * @param ids        the IDs for each document
     * @throws Exception if upsert fails
     */
    public void upsert(
            List<List<Float>> embeddings,
            List<Map<String, Object>> metadatas,
            List<String> documents,
            List<String> ids) throws Exception {
        Span span = tracer.startSpan("ChromaDB Upsert", FISpanKind.EMBEDDING);

        try (Scope scope = span.makeCurrent()) {
            // Set attributes
            span.setAttribute(SemanticConventions.LLM_SYSTEM, "chromadb");
            span.setAttribute("chromadb.collection", collectionName);
            span.setAttribute("chromadb.upsert_count", (long) ids.size());

            // Execute upsert
            collection.upsert(embeddings, metadatas, documents, ids);

            span.setStatus(StatusCode.OK);

        } catch (Exception e) {
            tracer.setError(span, e);
            throw e;
        } finally {
            span.end();
        }
    }

    /**
     * Deletes documents from the collection with tracing.
     *
     * @param ids            the IDs to delete (optional)
     * @param where          metadata filter (optional)
     * @param whereDocument  document filter (optional)
     * @throws Exception if delete fails
     */
    public void delete(
            List<String> ids,
            Map<String, Object> where,
            Map<String, Object> whereDocument) throws Exception {
        Span span = tracer.startSpan("ChromaDB Delete", FISpanKind.RETRIEVER);

        try (Scope scope = span.makeCurrent()) {
            // Set attributes
            span.setAttribute(SemanticConventions.LLM_SYSTEM, "chromadb");
            span.setAttribute("chromadb.collection", collectionName);

            if (ids != null) {
                span.setAttribute("chromadb.delete_ids_count", (long) ids.size());
            }
            if (where != null) {
                span.setAttribute("chromadb.has_where_filter", true);
            }

            // Execute delete
            collection.delete(ids, where, whereDocument);

            span.setStatus(StatusCode.OK);

        } catch (Exception e) {
            tracer.setError(span, e);
            throw e;
        } finally {
            span.end();
        }
    }

    /**
     * Gets documents from the collection with tracing.
     *
     * @param ids            the IDs to get (optional)
     * @param where          metadata filter (optional)
     * @param whereDocument  document filter (optional)
     * @param include        fields to include (optional)
     * @return get response
     * @throws Exception if get fails
     */
    public Collection.GetResponse get(
            List<String> ids,
            Map<String, Object> where,
            Map<String, Object> whereDocument,
            List<String> include) throws Exception {
        Span span = tracer.startSpan("ChromaDB Get", FISpanKind.RETRIEVER);

        try (Scope scope = span.makeCurrent()) {
            // Set attributes
            span.setAttribute(SemanticConventions.LLM_SYSTEM, "chromadb");
            span.setAttribute("chromadb.collection", collectionName);

            if (ids != null) {
                span.setAttribute("chromadb.get_ids_count", (long) ids.size());
            }

            // Execute get
            Collection.GetResponse response = collection.get(ids, where, whereDocument, include);

            // Capture result
            if (response != null && response.getIds() != null) {
                span.setAttribute("chromadb.retrieved_count", (long) response.getIds().size());
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
     * Gets the count of documents in the collection with tracing.
     *
     * @return the document count
     * @throws Exception if count fails
     */
    public int count() throws Exception {
        Span span = tracer.startSpan("ChromaDB Count", FISpanKind.RETRIEVER);

        try (Scope scope = span.makeCurrent()) {
            span.setAttribute(SemanticConventions.LLM_SYSTEM, "chromadb");
            span.setAttribute("chromadb.collection", collectionName);

            int count = collection.count();

            span.setAttribute("chromadb.count", (long) count);
            span.setStatus(StatusCode.OK);
            return count;

        } catch (Exception e) {
            tracer.setError(span, e);
            throw e;
        } finally {
            span.end();
        }
    }

    /**
     * Gets the underlying Collection.
     *
     * @return the wrapped Collection
     */
    public Collection unwrap() {
        return collection;
    }
}
