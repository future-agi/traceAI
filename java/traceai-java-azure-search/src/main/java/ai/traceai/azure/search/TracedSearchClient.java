package ai.traceai.azure.search;

import ai.traceai.*;
import com.azure.core.util.Context;
import com.azure.search.documents.SearchClient;
import com.azure.search.documents.SearchDocument;
import com.azure.search.documents.models.*;
import com.azure.search.documents.util.SearchPagedIterable;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.context.Scope;

import java.util.List;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicReference;

/**
 * Instrumentation wrapper for Azure AI Search (formerly Azure Cognitive Search) operations.
 * Wraps the Azure SearchClient to provide automatic tracing of all search and document operations.
 *
 * <p>Usage:</p>
 * <pre>
 * SearchClient searchClient = new SearchClientBuilder()
 *     .endpoint(endpoint)
 *     .credential(credential)
 *     .indexName("my-index")
 *     .buildClient();
 *
 * TracedSearchClient traced = new TracedSearchClient(searchClient, "my-index");
 *
 * // Vector search
 * SearchPagedIterable results = traced.searchWithVector("query", vectorData, "contentVector", 10);
 *
 * // Hybrid search
 * SearchPagedIterable results = traced.hybridSearch("query", vectorData, "contentVector", 10);
 * </pre>
 */
public class TracedSearchClient {

    private final SearchClient searchClient;
    private final FITracer tracer;
    private final String indexName;

    private static final String LLM_SYSTEM = "azure-search";
    private static final String DB_SYSTEM = "azure-ai-search";

    /**
     * Creates a new traced Azure Search client with the given client and tracer.
     *
     * @param searchClient the Azure SearchClient to wrap
     * @param indexName    the name of the search index
     * @param tracer       the FITracer for instrumentation
     */
    public TracedSearchClient(SearchClient searchClient, String indexName, FITracer tracer) {
        this.searchClient = searchClient;
        this.indexName = indexName;
        this.tracer = tracer;
    }

    /**
     * Creates a new traced Azure Search client using the global TraceAI tracer.
     *
     * @param searchClient the Azure SearchClient to wrap
     * @param indexName    the name of the search index
     */
    public TracedSearchClient(SearchClient searchClient, String indexName) {
        this(searchClient, indexName, TraceAI.getTracer());
    }

    /**
     * Performs a vector search with tracing.
     *
     * @param searchText  the search text (can be null for pure vector search)
     * @param vector      the query vector
     * @param vectorField the name of the vector field in the index
     * @param k           the number of results to return
     * @return the search results
     */
    public SearchPagedIterable searchWithVector(String searchText, List<Float> vector, String vectorField, int k) {
        Span span = tracer.startSpan("Azure Search Vector Query", FISpanKind.RETRIEVER);

        try (Scope scope = span.makeCurrent()) {
            setCommonAttributes(span, k, vector.size(), "vector");

            // Build vector query
            VectorizedQuery vectorQuery = new VectorizedQuery(vector)
                .setKNearestNeighborsCount(k)
                .setFields(vectorField);

            SearchOptions options = new SearchOptions()
                .setVectorSearchOptions(new VectorSearchOptions()
                    .setQueries(vectorQuery));

            // Execute search
            SearchPagedIterable results = searchClient.search(searchText, options, Context.NONE);

            captureSearchResults(span, results);

            span.setStatus(StatusCode.OK);
            return results;

        } catch (Exception e) {
            tracer.setError(span, e);
            throw new RuntimeException("Azure Search vector query failed", e);
        } finally {
            span.end();
        }
    }

    /**
     * Performs a vector search with filter and tracing.
     *
     * @param searchText  the search text (can be null for pure vector search)
     * @param vector      the query vector
     * @param vectorField the name of the vector field in the index
     * @param k           the number of results to return
     * @param filter      the OData filter expression
     * @return the search results
     */
    public SearchPagedIterable searchWithVectorAndFilter(
            String searchText,
            List<Float> vector,
            String vectorField,
            int k,
            String filter) {
        Span span = tracer.startSpan("Azure Search Filtered Vector Query", FISpanKind.RETRIEVER);

        try (Scope scope = span.makeCurrent()) {
            setCommonAttributes(span, k, vector.size(), "vector");
            span.setAttribute("azure_search.has_filter", true);
            span.setAttribute("azure_search.filter", filter);

            // Build vector query with filter
            VectorizedQuery vectorQuery = new VectorizedQuery(vector)
                .setKNearestNeighborsCount(k)
                .setFields(vectorField);

            SearchOptions options = new SearchOptions()
                .setFilter(filter)
                .setVectorSearchOptions(new VectorSearchOptions()
                    .setQueries(vectorQuery));

            // Execute search
            SearchPagedIterable results = searchClient.search(searchText, options, Context.NONE);

            captureSearchResults(span, results);

            span.setStatus(StatusCode.OK);
            return results;

        } catch (Exception e) {
            tracer.setError(span, e);
            throw new RuntimeException("Azure Search filtered vector query failed", e);
        } finally {
            span.end();
        }
    }

    /**
     * Performs a hybrid search (text + vector) with tracing.
     *
     * @param searchText  the search text for full-text search
     * @param vector      the query vector for vector search
     * @param vectorField the name of the vector field in the index
     * @param k           the number of results to return
     * @return the search results
     */
    public SearchPagedIterable hybridSearch(String searchText, List<Float> vector, String vectorField, int k) {
        Span span = tracer.startSpan("Azure Search Hybrid Query", FISpanKind.RETRIEVER);

        try (Scope scope = span.makeCurrent()) {
            setCommonAttributes(span, k, vector.size(), "hybrid");

            if (searchText != null) {
                tracer.setInputValue(span, searchText);
            }

            // Build vector query for hybrid search
            VectorizedQuery vectorQuery = new VectorizedQuery(vector)
                .setKNearestNeighborsCount(k)
                .setFields(vectorField);

            SearchOptions options = new SearchOptions()
                .setVectorSearchOptions(new VectorSearchOptions()
                    .setQueries(vectorQuery));

            // Execute search with both text and vector
            SearchPagedIterable results = searchClient.search(searchText, options, Context.NONE);

            captureSearchResults(span, results);

            span.setStatus(StatusCode.OK);
            return results;

        } catch (Exception e) {
            tracer.setError(span, e);
            throw new RuntimeException("Azure Search hybrid query failed", e);
        } finally {
            span.end();
        }
    }

    /**
     * Performs a text-only search with tracing.
     *
     * @param searchText the search text
     * @param top        the number of results to return
     * @return the search results
     */
    public SearchPagedIterable search(String searchText, int top) {
        Span span = tracer.startSpan("Azure Search Text Query", FISpanKind.RETRIEVER);

        try (Scope scope = span.makeCurrent()) {
            setCommonAttributes(span, top, 0, "text");

            if (searchText != null) {
                tracer.setInputValue(span, searchText);
            }

            SearchOptions options = new SearchOptions()
                .setTop(top);

            // Execute search
            SearchPagedIterable results = searchClient.search(searchText, options, Context.NONE);

            captureSearchResults(span, results);

            span.setStatus(StatusCode.OK);
            return results;

        } catch (Exception e) {
            tracer.setError(span, e);
            throw new RuntimeException("Azure Search text query failed", e);
        } finally {
            span.end();
        }
    }

    /**
     * Uploads documents to the index with tracing.
     *
     * @param documents the documents to upload
     * @param <T>       the document type
     * @return the index documents result
     */
    public <T> IndexDocumentsResult uploadDocuments(Iterable<T> documents) {
        Span span = tracer.startSpan("Azure Search Upload Documents", FISpanKind.EMBEDDING);

        try (Scope scope = span.makeCurrent()) {
            span.setAttribute(SemanticConventions.LLM_SYSTEM, LLM_SYSTEM);
            span.setAttribute("db.system", DB_SYSTEM);
            span.setAttribute("azure_search.index", indexName);
            span.setAttribute("azure_search.operation", "upload");

            int documentCount = countDocuments(documents);
            span.setAttribute("azure_search.documents_count", (long) documentCount);

            // Execute upload
            IndexDocumentsResult result = searchClient.uploadDocuments(documents);

            captureIndexResult(span, result);

            span.setStatus(StatusCode.OK);
            return result;

        } catch (Exception e) {
            tracer.setError(span, e);
            throw new RuntimeException("Azure Search upload documents failed", e);
        } finally {
            span.end();
        }
    }

    /**
     * Merges or uploads documents to the index with tracing.
     *
     * @param documents the documents to merge or upload
     * @param <T>       the document type
     * @return the index documents result
     */
    public <T> IndexDocumentsResult mergeOrUploadDocuments(Iterable<T> documents) {
        Span span = tracer.startSpan("Azure Search Merge or Upload Documents", FISpanKind.EMBEDDING);

        try (Scope scope = span.makeCurrent()) {
            span.setAttribute(SemanticConventions.LLM_SYSTEM, LLM_SYSTEM);
            span.setAttribute("db.system", DB_SYSTEM);
            span.setAttribute("azure_search.index", indexName);
            span.setAttribute("azure_search.operation", "merge_or_upload");

            int documentCount = countDocuments(documents);
            span.setAttribute("azure_search.documents_count", (long) documentCount);

            // Execute merge or upload
            IndexDocumentsResult result = searchClient.mergeOrUploadDocuments(documents);

            captureIndexResult(span, result);

            span.setStatus(StatusCode.OK);
            return result;

        } catch (Exception e) {
            tracer.setError(span, e);
            throw new RuntimeException("Azure Search merge or upload documents failed", e);
        } finally {
            span.end();
        }
    }

    /**
     * Deletes documents from the index with tracing.
     *
     * @param documents the documents to delete
     * @param <T>       the document type
     * @return the index documents result
     */
    public <T> IndexDocumentsResult deleteDocuments(Iterable<T> documents) {
        Span span = tracer.startSpan("Azure Search Delete Documents", FISpanKind.EMBEDDING);

        try (Scope scope = span.makeCurrent()) {
            span.setAttribute(SemanticConventions.LLM_SYSTEM, LLM_SYSTEM);
            span.setAttribute("db.system", DB_SYSTEM);
            span.setAttribute("azure_search.index", indexName);
            span.setAttribute("azure_search.operation", "delete");

            int documentCount = countDocuments(documents);
            span.setAttribute("azure_search.documents_count", (long) documentCount);

            // Execute delete
            IndexDocumentsResult result = searchClient.deleteDocuments(documents);

            captureIndexResult(span, result);

            span.setStatus(StatusCode.OK);
            return result;

        } catch (Exception e) {
            tracer.setError(span, e);
            throw new RuntimeException("Azure Search delete documents failed", e);
        } finally {
            span.end();
        }
    }

    /**
     * Retrieves a document by key with tracing.
     *
     * @param key        the document key
     * @param modelClass the class of the document model
     * @param <T>        the document type
     * @return the retrieved document
     */
    public <T> T getDocument(String key, Class<T> modelClass) {
        Span span = tracer.startSpan("Azure Search Get Document", FISpanKind.RETRIEVER);

        try (Scope scope = span.makeCurrent()) {
            span.setAttribute(SemanticConventions.LLM_SYSTEM, LLM_SYSTEM);
            span.setAttribute("db.system", DB_SYSTEM);
            span.setAttribute("azure_search.index", indexName);
            span.setAttribute("azure_search.operation", "get_document");
            span.setAttribute("azure_search.document_key", key);

            // Execute get document
            T document = searchClient.getDocument(key, modelClass);

            if (document != null) {
                span.setAttribute("azure_search.document_found", true);
            } else {
                span.setAttribute("azure_search.document_found", false);
            }

            span.setStatus(StatusCode.OK);
            return document;

        } catch (Exception e) {
            tracer.setError(span, e);
            throw new RuntimeException("Azure Search get document failed", e);
        } finally {
            span.end();
        }
    }

    /**
     * Gets the count of documents in the index with tracing.
     *
     * @return the document count
     */
    public long getDocumentCount() {
        Span span = tracer.startSpan("Azure Search Get Document Count", FISpanKind.RETRIEVER);

        try (Scope scope = span.makeCurrent()) {
            span.setAttribute(SemanticConventions.LLM_SYSTEM, LLM_SYSTEM);
            span.setAttribute("db.system", DB_SYSTEM);
            span.setAttribute("azure_search.index", indexName);
            span.setAttribute("azure_search.operation", "get_document_count");

            // Execute get document count
            long count = searchClient.getDocumentCount();

            span.setAttribute("azure_search.total_documents", count);

            span.setStatus(StatusCode.OK);
            return count;

        } catch (Exception e) {
            tracer.setError(span, e);
            throw new RuntimeException("Azure Search get document count failed", e);
        } finally {
            span.end();
        }
    }

    /**
     * Gets the underlying Azure SearchClient.
     *
     * @return the wrapped SearchClient
     */
    public SearchClient unwrap() {
        return searchClient;
    }

    /**
     * Gets the index name.
     *
     * @return the index name
     */
    public String getIndexName() {
        return indexName;
    }

    /**
     * Sets common attributes for search operations.
     */
    private void setCommonAttributes(Span span, int topK, int embeddingDimensions, String searchMode) {
        span.setAttribute(SemanticConventions.LLM_SYSTEM, LLM_SYSTEM);
        span.setAttribute("db.system", DB_SYSTEM);
        span.setAttribute("azure_search.index", indexName);
        span.setAttribute(SemanticConventions.RETRIEVER_TOP_K, (long) topK);
        span.setAttribute("azure_search.search_mode", searchMode);

        if (embeddingDimensions > 0) {
            span.setAttribute(SemanticConventions.EMBEDDING_DIMENSIONS, (long) embeddingDimensions);
        }
    }

    /**
     * Captures search results metrics on the span.
     */
    private void captureSearchResults(Span span, SearchPagedIterable results) {
        if (results == null) {
            span.setAttribute("azure_search.results_count", 0L);
            return;
        }

        AtomicInteger count = new AtomicInteger(0);
        AtomicReference<Double> topScore = new AtomicReference<>(null);

        // Iterate through results to get count and top score
        results.forEach(result -> {
            int currentCount = count.incrementAndGet();
            if (currentCount == 1) {
                topScore.set(result.getScore());
            }
        });

        span.setAttribute("azure_search.results_count", (long) count.get());
        if (topScore.get() != null) {
            span.setAttribute("azure_search.top_score", topScore.get());
        }
    }

    /**
     * Captures index operation results on the span.
     */
    private void captureIndexResult(Span span, IndexDocumentsResult result) {
        if (result == null || result.getResults() == null) {
            return;
        }

        int successCount = 0;
        int failedCount = 0;

        for (IndexingResult indexingResult : result.getResults()) {
            if (indexingResult.isSucceeded()) {
                successCount++;
            } else {
                failedCount++;
            }
        }

        span.setAttribute("azure_search.success_count", (long) successCount);
        span.setAttribute("azure_search.failed_count", (long) failedCount);
    }

    /**
     * Counts documents in an iterable.
     */
    private <T> int countDocuments(Iterable<T> documents) {
        int count = 0;
        for (T ignored : documents) {
            count++;
        }
        return count;
    }
}
