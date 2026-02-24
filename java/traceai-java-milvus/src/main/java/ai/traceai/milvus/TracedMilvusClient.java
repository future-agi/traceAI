package ai.traceai.milvus;

import ai.traceai.*;
import io.milvus.v2.client.MilvusClientV2;
import io.milvus.v2.service.vector.request.*;
import io.milvus.v2.service.vector.response.*;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.context.Scope;

import java.util.List;
import java.util.Map;

/**
 * Instrumentation wrapper for Milvus Java client.
 * Wraps the MilvusClientV2 to provide automatic tracing of all vector operations.
 *
 * <p>Usage:</p>
 * <pre>
 * MilvusClientV2 client = new MilvusClientV2(connectConfig);
 * TracedMilvusClient traced = new TracedMilvusClient(client);
 *
 * SearchResp response = traced.search(SearchReq.builder()
 *     .collectionName("my_collection")
 *     .data(queryVectors)
 *     .topK(10)
 *     .build());
 * </pre>
 */
public class TracedMilvusClient {

    private final MilvusClientV2 client;
    private final FITracer tracer;

    /**
     * Creates a new traced Milvus client with the given client and tracer.
     *
     * @param client the MilvusClientV2 to wrap
     * @param tracer the FITracer for instrumentation
     */
    public TracedMilvusClient(MilvusClientV2 client, FITracer tracer) {
        this.client = client;
        this.tracer = tracer;
    }

    /**
     * Creates a new traced Milvus client using the global TraceAI tracer.
     *
     * @param client the MilvusClientV2 to wrap
     */
    public TracedMilvusClient(MilvusClientV2 client) {
        this(client, TraceAI.getTracer());
    }

    /**
     * Searches for similar vectors with tracing.
     *
     * @param request the search request
     * @return the search response
     */
    public SearchResp search(SearchReq request) {
        Span span = tracer.startSpan("Milvus Search", FISpanKind.RETRIEVER);

        try (Scope scope = span.makeCurrent()) {
            // Set attributes
            span.setAttribute(SemanticConventions.LLM_SYSTEM, "milvus");
            span.setAttribute("milvus.collection", request.getCollectionName());
            span.setAttribute(SemanticConventions.RETRIEVER_TOP_K, (long) request.getTopK());

            // Capture vector dimensions if available
            List<?> data = request.getData();
            if (data != null && !data.isEmpty()) {
                Object first = data.get(0);
                if (first instanceof List) {
                    span.setAttribute(SemanticConventions.EMBEDDING_DIMENSIONS, (long) ((List<?>) first).size());
                }
                span.setAttribute("milvus.query_vectors_count", (long) data.size());
            }

            // Capture filter if present
            if (request.getFilter() != null && !request.getFilter().isEmpty()) {
                span.setAttribute("milvus.filter", request.getFilter());
            }

            // Execute search
            SearchResp response = client.search(request);

            // Capture results
            List<List<SearchResp.SearchResult>> results = response.getSearchResults();
            if (results != null && !results.isEmpty()) {
                span.setAttribute("milvus.results_count", (long) results.get(0).size());
                if (!results.get(0).isEmpty()) {
                    span.setAttribute("milvus.top_score", results.get(0).get(0).getScore());
                }
            }

            span.setStatus(StatusCode.OK);
            return response;

        } catch (Exception e) {
            tracer.setError(span, e);
            throw new RuntimeException("Milvus search failed", e);
        } finally {
            span.end();
        }
    }

    /**
     * Inserts data with tracing.
     * In Milvus SDK v2.6.x, InsertReq.getData() returns List&lt;JsonObject&gt; (Gson).
     *
     * @param request the insert request
     * @return the insert response
     */
    public InsertResp insert(InsertReq request) {
        Span span = tracer.startSpan("Milvus Insert", FISpanKind.EMBEDDING);

        try (Scope scope = span.makeCurrent()) {
            // Set attributes
            span.setAttribute(SemanticConventions.LLM_SYSTEM, "milvus");
            span.setAttribute("milvus.collection", request.getCollectionName());

            // getData() returns List<JsonObject> in Milvus SDK v2.6.x
            List<?> data = request.getData();
            if (data != null) {
                span.setAttribute("milvus.insert_count", (long) data.size());
            }

            // Execute insert
            InsertResp response = client.insert(request);

            // Capture result
            span.setAttribute("milvus.inserted_count", response.getInsertCnt());

            span.setStatus(StatusCode.OK);
            return response;

        } catch (Exception e) {
            tracer.setError(span, e);
            throw new RuntimeException("Milvus insert failed", e);
        } finally {
            span.end();
        }
    }

    /**
     * Upserts data with tracing.
     * In Milvus SDK v2.6.x, UpsertReq.getData() returns List&lt;JsonObject&gt; (Gson).
     *
     * @param request the upsert request
     * @return the upsert response
     */
    public UpsertResp upsert(UpsertReq request) {
        Span span = tracer.startSpan("Milvus Upsert", FISpanKind.EMBEDDING);

        try (Scope scope = span.makeCurrent()) {
            // Set attributes
            span.setAttribute(SemanticConventions.LLM_SYSTEM, "milvus");
            span.setAttribute("milvus.collection", request.getCollectionName());

            // getData() returns List<JsonObject> in Milvus SDK v2.6.x
            List<?> data = request.getData();
            if (data != null) {
                span.setAttribute("milvus.upsert_count", (long) data.size());
            }

            // Execute upsert
            UpsertResp response = client.upsert(request);

            // Capture result
            span.setAttribute("milvus.upserted_count", response.getUpsertCnt());

            span.setStatus(StatusCode.OK);
            return response;

        } catch (Exception e) {
            tracer.setError(span, e);
            throw new RuntimeException("Milvus upsert failed", e);
        } finally {
            span.end();
        }
    }

    /**
     * Deletes data with tracing.
     *
     * @param request the delete request
     * @return the delete response
     */
    public DeleteResp delete(DeleteReq request) {
        Span span = tracer.startSpan("Milvus Delete", FISpanKind.RETRIEVER);

        try (Scope scope = span.makeCurrent()) {
            // Set attributes
            span.setAttribute(SemanticConventions.LLM_SYSTEM, "milvus");
            span.setAttribute("milvus.collection", request.getCollectionName());

            if (request.getFilter() != null) {
                span.setAttribute("milvus.filter", request.getFilter());
            }
            if (request.getIds() != null) {
                span.setAttribute("milvus.delete_ids_count", (long) request.getIds().size());
            }

            // Execute delete
            DeleteResp response = client.delete(request);

            // Capture result
            span.setAttribute("milvus.deleted_count", response.getDeleteCnt());

            span.setStatus(StatusCode.OK);
            return response;

        } catch (Exception e) {
            tracer.setError(span, e);
            throw new RuntimeException("Milvus delete failed", e);
        } finally {
            span.end();
        }
    }

    /**
     * Gets data by IDs with tracing.
     *
     * @param request the get request
     * @return the get response
     */
    public GetResp get(GetReq request) {
        Span span = tracer.startSpan("Milvus Get", FISpanKind.RETRIEVER);

        try (Scope scope = span.makeCurrent()) {
            // Set attributes
            span.setAttribute(SemanticConventions.LLM_SYSTEM, "milvus");
            span.setAttribute("milvus.collection", request.getCollectionName());

            if (request.getIds() != null) {
                span.setAttribute("milvus.requested_ids_count", (long) request.getIds().size());
            }

            // Execute get
            GetResp response = client.get(request);

            // Capture result
            if (response.getGetResults() != null) {
                span.setAttribute("milvus.retrieved_count", (long) response.getGetResults().size());
            }

            span.setStatus(StatusCode.OK);
            return response;

        } catch (Exception e) {
            tracer.setError(span, e);
            throw new RuntimeException("Milvus get failed", e);
        } finally {
            span.end();
        }
    }

    /**
     * Queries data with tracing.
     *
     * @param request the query request
     * @return the query response
     */
    public QueryResp query(QueryReq request) {
        Span span = tracer.startSpan("Milvus Query", FISpanKind.RETRIEVER);

        try (Scope scope = span.makeCurrent()) {
            // Set attributes
            span.setAttribute(SemanticConventions.LLM_SYSTEM, "milvus");
            span.setAttribute("milvus.collection", request.getCollectionName());

            if (request.getFilter() != null) {
                span.setAttribute("milvus.filter", request.getFilter());
            }
            // getLimit() returns primitive long in Milvus SDK v2.6.x
            long limit = request.getLimit();
            if (limit > 0) {
                span.setAttribute("milvus.limit", limit);
            }

            // Execute query
            QueryResp response = client.query(request);

            // Capture result
            if (response.getQueryResults() != null) {
                span.setAttribute("milvus.results_count", (long) response.getQueryResults().size());
            }

            span.setStatus(StatusCode.OK);
            return response;

        } catch (Exception e) {
            tracer.setError(span, e);
            throw new RuntimeException("Milvus query failed", e);
        } finally {
            span.end();
        }
    }

    /**
     * Gets the underlying MilvusClientV2.
     *
     * @return the wrapped MilvusClientV2
     */
    public MilvusClientV2 unwrap() {
        return client;
    }
}
