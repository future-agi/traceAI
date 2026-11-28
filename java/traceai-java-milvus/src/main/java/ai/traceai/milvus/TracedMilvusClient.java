package ai.traceai.milvus;

import ai.traceai.FISpanKind;
import ai.traceai.FITracer;
import ai.traceai.SemanticConventions;
import ai.traceai.TraceAI;
import io.milvus.orm.iterator.QueryIterator;
import io.milvus.orm.iterator.SearchIterator;
import io.milvus.orm.iterator.SearchIteratorV2;
import io.milvus.response.QueryResultsWrapper.RowRecord;
import io.milvus.v2.client.MilvusClientV2;
import io.milvus.v2.service.vector.request.*;
import io.milvus.v2.service.vector.response.*;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.context.Context;
import io.opentelemetry.context.Scope;

import java.lang.reflect.InvocationTargetException;
import java.util.List;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.CompletionException;
import java.util.function.BiConsumer;
import java.util.function.Consumer;
import java.util.function.Supplier;

/**
 * Traces Milvus vector operations without changing SDK results or exceptions.
 * Synchronous and iterator operations support SDK 2.6.12; native async operations
 * require an SDK exposing those methods (such as 3.0.12). Administrative operations
 * remain available, untraced, through {@link #unwrap()}.
 */
public class TracedMilvusClient {
    private final MilvusClientV2 client;
    private final FITracer tracer;

    /** @param client the SDK client to wrap; its lifecycle remains caller-owned
     *  @param tracer the tracer used for vector operations */
    public TracedMilvusClient(MilvusClientV2 client, FITracer tracer) {
        this.client = client;
        this.tracer = tracer;
    }

    /** @param client the SDK client to wrap using the global TraceAI tracer */
    public TracedMilvusClient(MilvusClientV2 client) {
        this(client, TraceAI.getTracer());
    }

    public SearchResp search(SearchReq request) {
        return trace("Search", "search", FISpanKind.RETRIEVER, request.getCollectionName(),
                span -> searchInput(span, request), () -> client.search(request), this::searchOutput);
    }

    public SearchResp hybridSearch(HybridSearchReq request) {
        return trace("Hybrid Search", "hybrid_search", FISpanKind.RETRIEVER, request.getCollectionName(),
                span -> hybridInput(span, request), () -> client.hybridSearch(request), this::searchOutput);
    }

    public InsertResp insert(InsertReq request) {
        return trace("Insert", "insert", FISpanKind.VECTOR_DB, request.getCollectionName(),
                span -> count(span, SemanticConventions.DB_VECTOR_UPSERT_COUNT, request.getData()),
                () -> client.insert(request), (span, response) -> {
                    if (response != null) span.setAttribute("milvus.inserted_count", response.getInsertCnt());
                });
    }

    public UpsertResp upsert(UpsertReq request) {
        return trace("Upsert", "upsert", FISpanKind.VECTOR_DB, request.getCollectionName(),
                span -> count(span, SemanticConventions.DB_VECTOR_UPSERT_COUNT, request.getData()),
                () -> client.upsert(request), (span, response) -> {
                    if (response != null) span.setAttribute("milvus.upserted_count", response.getUpsertCnt());
                });
    }

    public DeleteResp delete(DeleteReq request) {
        return trace("Delete", "delete", FISpanKind.VECTOR_DB, request.getCollectionName(), span -> {
                    filter(span, request.getFilter());
                    count(span, SemanticConventions.DB_VECTOR_DELETE_COUNT, request.getIds());
                }, () -> client.delete(request), (span, response) -> {
                    if (response != null) span.setAttribute("milvus.deleted_count", response.getDeleteCnt());
                });
    }

    public GetResp get(GetReq request) {
        return trace("Get", "get", FISpanKind.VECTOR_DB, request.getCollectionName(),
                span -> count(span, "milvus.requested_ids_count", request.getIds()),
                () -> client.get(request), this::getOutput);
    }

    public QueryResp query(QueryReq request) {
        return trace("Query", "query", FISpanKind.RETRIEVER, request.getCollectionName(),
                span -> queryInput(span, request), () -> client.query(request), this::queryOutput);
    }

    /** Returns the SDK's original future; its completion, failure or cancellation ends the span. */
    public CompletableFuture<SearchResp> searchAsync(SearchReq request) {
        return traceAsync("Search", "search", FISpanKind.RETRIEVER, request.getCollectionName(),
                span -> searchInput(span, request),
                () -> invokeAsync("searchAsync", SearchReq.class, request), this::searchOutput);
    }

    /** Native async hybrid search. Requires an SDK exposing hybridSearchAsync. */
    public CompletableFuture<SearchResp> hybridSearchAsync(HybridSearchReq request) {
        return traceAsync("Hybrid Search", "hybrid_search", FISpanKind.RETRIEVER, request.getCollectionName(),
                span -> hybridInput(span, request),
                () -> invokeAsync("hybridSearchAsync", HybridSearchReq.class, request), this::searchOutput);
    }

    /** Native async query. Requires an SDK exposing queryAsync. */
    public CompletableFuture<QueryResp> queryAsync(QueryReq request) {
        return traceAsync("Query", "query", FISpanKind.RETRIEVER, request.getCollectionName(),
                span -> queryInput(span, request),
                () -> invokeAsync("queryAsync", QueryReq.class, request), this::queryOutput);
    }

    /** Native async get. Requires an SDK exposing getAsync. */
    public CompletableFuture<GetResp> getAsync(GetReq request) {
        return traceAsync("Get", "get", FISpanKind.VECTOR_DB, request.getCollectionName(),
                span -> count(span, "milvus.requested_ids_count", request.getIds()),
                () -> invokeAsync("getAsync", GetReq.class, request), this::getOutput);
    }

    /** Traces creation and each requested batch; does not prefetch or accumulate rows. */
    public TracedMilvusIterator<RowRecord> queryIterator(QueryIteratorReq request) {
        Context parent = Context.current();
        String collection = request.getCollectionName();
        QueryIterator iterator = trace("Query Iterator", "query_iterator", FISpanKind.RETRIEVER,
                collection, span -> filter(span, request.getExpr()), () -> client.queryIterator(request),
                (span, result) -> {});
        return new TracedMilvusIterator<>(tracer, "Query Iterator", "query_iterator", collection,
                parent, iterator::next, iterator::close);
    }

    /** Traces creation and each requested batch; close the iterator after use. */
    public TracedMilvusIterator<RowRecord> searchIterator(SearchIteratorReq request) {
        Context parent = Context.current();
        String collection = request.getCollectionName();
        SearchIterator iterator = trace("Search Iterator", "search_iterator", FISpanKind.RETRIEVER,
                collection, span -> filter(span, request.getExpr()), () -> client.searchIterator(request),
                (span, result) -> {});
        return new TracedMilvusIterator<>(tracer, "Search Iterator", "search_iterator", collection,
                parent, iterator::next, iterator::close);
    }

    /** Traces the SDK V2 search iterator, including its lazy next and close calls. */
    public TracedMilvusIterator<SearchResp.SearchResult> searchIteratorV2(SearchIteratorReqV2 request) {
        Context parent = Context.current();
        String collection = request.getCollectionName();
        SearchIteratorV2 iterator = trace("Search Iterator V2", "search_iterator_v2", FISpanKind.RETRIEVER,
                collection, span -> filter(span, request.getFilter()), () -> client.searchIteratorV2(request),
                (span, result) -> {});
        return new TracedMilvusIterator<>(tracer, "Search Iterator V2", "search_iterator_v2", collection,
                parent, iterator::next, iterator::close);
    }

    /** @return the original SDK client; calls on it bypass this wrapper */
    public MilvusClientV2 unwrap() {
        return client;
    }

    private <T> T trace(String name, String operation, FISpanKind kind, String collection,
                        Consumer<Span> input, Supplier<T> call, BiConsumer<Span, T> output) {
        Span span = startSpan(name, operation, kind, collection);
        try (Scope ignored = span.makeCurrent()) {
            input.accept(span);
            T response = call.get();
            output.accept(span, response);
            span.setStatus(StatusCode.OK);
            return response;
        } catch (RuntimeException | Error failure) {
            tracer.setError(span, failure);
            throw failure;
        } finally {
            span.end();
        }
    }

    private <T> CompletableFuture<T> traceAsync(String name, String operation, FISpanKind kind,
            String collection, Consumer<Span> input, Supplier<CompletableFuture<T>> call,
            BiConsumer<Span, T> output) {
        Span span = startSpan(name, operation, kind, collection);
        try (Scope ignored = span.makeCurrent()) {
            input.accept(span);
            CompletableFuture<T> future = call.get();
            future.whenComplete((response, failure) -> {
                try {
                    if (failure == null) {
                        output.accept(span, response);
                        span.setStatus(StatusCode.OK);
                    } else {
                        Throwable cause = failure;
                        while (cause instanceof CompletionException && cause.getCause() != null) {
                            cause = cause.getCause();
                        }
                        tracer.setError(span, cause);
                    }
                } finally {
                    span.end();
                }
            });
            // Do not return the dependent future: that would break SDK cancellation and identity.
            return future;
        } catch (RuntimeException | Error failure) {
            try {
                tracer.setError(span, failure);
            } finally {
                span.end();
            }
            throw failure;
        }
    }

    // Native async methods are absent from the supported 2.6.12 floor. Resolve only
    // when called, so existing synchronous customers need not upgrade their SDK.
    @SuppressWarnings("unchecked")
    private <T> CompletableFuture<T> invokeAsync(String name, Class<?> requestType, Object request) {
        try {
            return (CompletableFuture<T>) MilvusClientV2.class.getMethod(name, requestType).invoke(client, request);
        } catch (NoSuchMethodException missing) {
            throw new UnsupportedOperationException(name + " requires a Milvus SDK with native async support; "
                    + "use milvus-sdk-java 3.0.12 or a compatible version", missing);
        } catch (InvocationTargetException invocation) {
            Throwable cause = invocation.getCause();
            if (cause instanceof RuntimeException) throw (RuntimeException) cause;
            if (cause instanceof Error) throw (Error) cause;
            throw new IllegalStateException("Unexpected checked exception from Milvus " + name, cause);
        } catch (IllegalAccessException inaccessible) {
            throw new IllegalStateException("Cannot access Milvus " + name, inaccessible);
        }
    }

    private Span startSpan(String name, String operation, FISpanKind kind, String collection) {
        Span span = tracer.startSpan("Milvus " + name, kind);
        span.setAttribute(SemanticConventions.DB_SYSTEM, "milvus");
        span.setAttribute(SemanticConventions.DB_OPERATION_NAME, operation);
        span.setAttribute(SemanticConventions.DB_VECTOR_COLLECTION_NAME, collection);
        return span;
    }

    private void searchInput(Span span, SearchReq request) {
        span.setAttribute(SemanticConventions.RETRIEVER_TOP_K, (long) request.getTopK());
        List<?> data = request.getData();
        count(span, "milvus.query_vectors_count", data);
        if (data != null && !data.isEmpty() && data.get(0) instanceof List) {
            span.setAttribute(SemanticConventions.EMBEDDING_DIMENSIONS, (long) ((List<?>) data.get(0)).size());
        }
        filter(span, request.getFilter());
    }

    private void hybridInput(Span span, HybridSearchReq request) {
        span.setAttribute(SemanticConventions.RETRIEVER_TOP_K, (long) request.getTopK());
        count(span, "milvus.search_requests_count", request.getSearchRequests());
    }

    private void queryInput(Span span, QueryReq request) {
        filter(span, request.getFilter());
        if (request.getLimit() > 0) span.setAttribute("milvus.limit", request.getLimit());
    }

    private void filter(Span span, String filter) {
        if (!tracer.getConfig().isHideInputs() && filter != null && !filter.isEmpty()) {
            span.setAttribute("milvus.filter", filter);
        }
    }

    private void searchOutput(Span span, SearchResp response) {
        if (response == null || response.getSearchResults() == null) return;
        List<List<SearchResp.SearchResult>> results = response.getSearchResults();
        long total = 0;
        for (List<SearchResp.SearchResult> batch : results) {
            if (batch != null) total += batch.size();
        }
        span.setAttribute(SemanticConventions.DB_VECTOR_RESULTS_COUNT, total);
        if (!results.isEmpty() && results.get(0) != null && !results.get(0).isEmpty()
                && results.get(0).get(0) != null && results.get(0).get(0).getScore() != null) {
            span.setAttribute("milvus.top_score", results.get(0).get(0).getScore());
        }
    }

    private void queryOutput(Span span, QueryResp response) {
        if (response != null) count(span, SemanticConventions.DB_VECTOR_RESULTS_COUNT, response.getQueryResults());
    }

    private void getOutput(Span span, GetResp response) {
        if (response != null) count(span, "milvus.retrieved_count", response.getGetResults());
    }

    private void count(Span span, String key, List<?> values) {
        if (values != null) span.setAttribute(key, (long) values.size());
    }
}
