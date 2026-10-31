package ai.traceai.azure.search;

import ai.traceai.*;
import com.azure.core.util.Context;
import com.azure.search.documents.SearchClient;
import com.azure.search.documents.models.*;
import com.azure.search.documents.util.SearchPagedIterable;
import com.azure.search.documents.util.SearchPagedResponse;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.context.Scope;

import java.util.List;
import java.util.function.Function;
import java.util.function.Supplier;

/**
 * Instrumentation wrapper for Azure AI Search (formerly Azure Cognitive Search) operations.
 * Wraps the synchronous Azure SearchClient from azure-search-documents 11.6.2 through 11.8.1.
 * Azure SDK 12.x and SearchAsyncClient are not supported by this wrapper.
 *
 * <p>Searches stay lazy. Each requested page produces a RETRIEVER span, parented to the
 * context at search creation. Result counts and top scores describe that page, not the
 * whole query. No span or request is created for an unconsumed search. The first page
 * is cached like the underlying SDK, including access through metadata getters.</p>
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

    private static final String DB_SYSTEM_VALUE = "azure-ai-search";

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
        return traceSearch("Azure Search Vector Query", searchText,
            vectorOptions(vector, vectorField, k), k, vector.size(), "vector");
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
        return traceSearch("Azure Search Filtered Vector Query", searchText,
            vectorOptions(vector, vectorField, k).setFilter(filter), k, vector.size(), "vector");
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
        return traceSearch("Azure Search Hybrid Query", searchText,
            vectorOptions(vector, vectorField, k), k, vector.size(), "hybrid");
    }

    /**
     * Performs a text-only search with tracing.
     *
     * @param searchText the search text
     * @param top        the number of results to return
     * @return the search results
     */
    public SearchPagedIterable search(String searchText, int top) {
        return traceSearch("Azure Search Text Query", searchText,
            new SearchOptions().setTop(top), top, 0, "text");
    }

    /**
     * Uploads documents to the index with tracing.
     *
     * @param documents the documents to upload
     * @param <T>       the document type
     * @return the index documents result
     */
    public <T> IndexDocumentsResult uploadDocuments(Iterable<T> documents) {
        Span span = tracer.startSpan("Azure Search Upload Documents", FISpanKind.VECTOR_DB);

        try (Scope scope = span.makeCurrent()) {
            span.setAttribute(SemanticConventions.DB_SYSTEM, DB_SYSTEM_VALUE);
            span.setAttribute(SemanticConventions.DB_VECTOR_INDEX_NAME, indexName);
            span.setAttribute("azure_search.operation", "upload");

            int documentCount = countDocuments(documents);
            span.setAttribute(SemanticConventions.DB_VECTOR_UPSERT_COUNT, (long) documentCount);

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
        Span span = tracer.startSpan("Azure Search Merge or Upload Documents", FISpanKind.VECTOR_DB);

        try (Scope scope = span.makeCurrent()) {
            span.setAttribute(SemanticConventions.DB_SYSTEM, DB_SYSTEM_VALUE);
            span.setAttribute(SemanticConventions.DB_VECTOR_INDEX_NAME, indexName);
            span.setAttribute("azure_search.operation", "merge_or_upload");

            int documentCount = countDocuments(documents);
            span.setAttribute(SemanticConventions.DB_VECTOR_UPSERT_COUNT, (long) documentCount);

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
        Span span = tracer.startSpan("Azure Search Delete Documents", FISpanKind.VECTOR_DB);

        try (Scope scope = span.makeCurrent()) {
            span.setAttribute(SemanticConventions.DB_SYSTEM, DB_SYSTEM_VALUE);
            span.setAttribute(SemanticConventions.DB_VECTOR_INDEX_NAME, indexName);
            span.setAttribute("azure_search.operation", "delete");

            int documentCount = countDocuments(documents);
            span.setAttribute(SemanticConventions.DB_VECTOR_UPSERT_COUNT, (long) documentCount);

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
        Span span = tracer.startSpan("Azure Search Get Document", FISpanKind.VECTOR_DB);

        try (Scope scope = span.makeCurrent()) {
            span.setAttribute(SemanticConventions.DB_SYSTEM, DB_SYSTEM_VALUE);
            span.setAttribute(SemanticConventions.DB_VECTOR_INDEX_NAME, indexName);
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
        Span span = tracer.startSpan("Azure Search Get Document Count", FISpanKind.VECTOR_DB);

        try (Scope scope = span.makeCurrent()) {
            span.setAttribute(SemanticConventions.DB_SYSTEM, DB_SYSTEM_VALUE);
            span.setAttribute(SemanticConventions.DB_VECTOR_INDEX_NAME, indexName);
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
        span.setAttribute(SemanticConventions.DB_SYSTEM, DB_SYSTEM_VALUE);
        span.setAttribute(SemanticConventions.DB_VECTOR_INDEX_NAME, indexName);
        span.setAttribute(SemanticConventions.RETRIEVER_TOP_K, (long) topK);
        span.setAttribute("azure_search.search_mode", searchMode);

        if (embeddingDimensions > 0) {
            span.setAttribute(SemanticConventions.EMBEDDING_DIMENSIONS, (long) embeddingDimensions);
        }
    }

    private SearchOptions vectorOptions(List<Float> vector, String vectorField, int k) {
        VectorizedQuery query = new VectorizedQuery(vector).setKNearestNeighborsCount(k).setFields(vectorField);
        return new SearchOptions().setVectorSearchOptions(new VectorSearchOptions().setQueries(query));
    }

    private SearchPagedIterable traceSearch(String name, String searchText, SearchOptions options,
                                           int topK, int dimensions, String mode) {
        // SearchClient constructs a lazy iterable and owns its first-page cache.
        // Do not enumerate it here, even to calculate telemetry attributes.
        SearchPagedIterable results = searchClient.search(searchText, options, Context.NONE);
        io.opentelemetry.context.Context parent = io.opentelemetry.context.Context.current();
        Function<String, SearchPagedResponse> fetch = token -> {
            Span span = tracer.startSpan(name, FISpanKind.RETRIEVER, parent);
            try (Scope scope = span.makeCurrent()) {
                setCommonAttributes(span, topK, dimensions, mode);
                span.setAttribute("azure_search.result_scope", "page");
                if (!"vector".equals(mode)) {
                    tracer.setInputValue(span, searchText);
                }
                if (options.getFilter() != null) {
                    span.setAttribute("azure_search.has_filter", true);
                    if (!tracer.getConfig().isHideInputs()) {
                        span.setAttribute("azure_search.filter", options.getFilter());
                    }
                }

                // Request exactly the page the caller asks for; return its metadata unchanged.
                SearchPagedResponse page = results.iterableByPage(token).iterator().next();
                List<SearchResult> values = page.getValue();
                span.setAttribute(SemanticConventions.DB_VECTOR_RESULTS_COUNT, (long) values.size());
                if (!values.isEmpty()) {
                    span.setAttribute("azure_search.top_score", values.get(0).getScore());
                }
                span.setStatus(StatusCode.OK);
                return page;
            } catch (RuntimeException error) {
                tracer.setError(span, error);
                throw error;
            } finally {
                // A page span always ends, even if the caller abandons iteration.
                span.end();
            }
        };
        // Metadata getters and repeated iteration share the first page in Azure's SDK.
        // Share its trace too, rather than emitting extra spans for cache reads.
        Supplier<SearchPagedResponse> firstPage = new Supplier<>() {
            private SearchPagedResponse page;

            @Override
            public synchronized SearchPagedResponse get() {
                if (page == null) {
                    page = fetch.apply(null);
                }
                return page;
            }
        };
        return new SearchPagedIterable(firstPage, fetch);
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
