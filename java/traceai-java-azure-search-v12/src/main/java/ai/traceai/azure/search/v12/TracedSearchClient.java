package ai.traceai.azure.search.v12;

import ai.traceai.*;
import com.azure.search.documents.SearchClient;
import com.azure.search.documents.models.*;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.context.Scope;

import java.util.List;
import java.util.Map;

/**
 * Instrumentation wrapper for Azure AI Search (formerly Azure Cognitive Search) operations.
 * Wraps synchronous retrieval with azure-search-documents 12.0.3.
 * Use traceai-java-azure-search for SDK 11.x. SearchAsyncClient is not supported.
 * Document operations are available on the unwrapped client, without retrieval enrichment.
 *
 * <p>Searches stay lazy. Each requested page produces a RETRIEVER span, parented to the
 * context at search creation. Result counts and top scores describe that page, not the
 * whole query. No span or request is created for an unconsumed search. Like SDK 12, repeated iteration re-fetches pages; metadata is available on each page.</p>
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
        VectorizedQuery query = new VectorizedQuery(vector).setKNearestNeighbors(k).setFields(vectorField);
        return new SearchOptions().setVectorQueries(query);
    }

    private SearchPagedIterable traceSearch(String name, String searchText, SearchOptions options,
                                           int topK, int dimensions, String mode) {
        // SearchClient constructs a lazy iterable; SDK 12 does not cache pages.
        // Do not enumerate it here, even to calculate telemetry attributes.
        SearchPagedIterable results = searchClient.search(options.setSearchText(searchText));
        io.opentelemetry.context.Context parent = io.opentelemetry.context.Context.current();
        Map<String, String> callerAttributes = Map.copyOf(ContextAttributes.getAttributesFromContext());
        return new SearchPagedIterable(() -> (token, pageSize) -> {
            Span span = tracer.startSpan(name, FISpanKind.RETRIEVER, parent, callerAttributes);
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
                SearchPagedResponse page = (pageSize == null ? results.iterableByPage(token)
                    : results.iterableByPage(token, pageSize)).iterator().next();
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
        });
    }
}
