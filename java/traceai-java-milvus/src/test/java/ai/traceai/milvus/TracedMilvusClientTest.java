package ai.traceai.milvus;

import ai.traceai.FITracer;
import ai.traceai.SemanticConventions;
import ai.traceai.TraceConfig;
import io.milvus.orm.iterator.QueryIterator;
import io.milvus.orm.iterator.SearchIterator;
import io.milvus.orm.iterator.SearchIteratorV2;
import io.milvus.response.QueryResultsWrapper.RowRecord;
import io.milvus.v2.client.MilvusClientV2;
import io.milvus.v2.exception.ErrorCode;
import io.milvus.v2.exception.MilvusClientException;
import io.milvus.v2.service.vector.request.*;
import io.milvus.v2.service.vector.response.*;
import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.context.Scope;
import io.opentelemetry.sdk.testing.exporter.InMemorySpanExporter;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.data.SpanData;
import io.opentelemetry.sdk.trace.export.SimpleSpanProcessor;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;

import java.util.List;
import java.util.Map;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.CompletionException;
import java.util.concurrent.atomic.AtomicReference;

import static org.assertj.core.api.Assertions.*;
import static org.mockito.Mockito.*;

class TracedMilvusClientTest {
    private InMemorySpanExporter exporter;
    private SdkTracerProvider provider;
    private FITracer tracer;

    @BeforeEach
    void setUp() {
        exporter = InMemorySpanExporter.create();
        provider = SdkTracerProvider.builder().addSpanProcessor(SimpleSpanProcessor.create(exporter)).build();
        tracer = new FITracer(provider.get("milvus-test"), TraceConfig.builder()
                .hideInputs(true).hideOutputs(true).hideErrorDetails(true).build());
    }

    @AfterEach
    void close() {
        provider.close();
    }

    @Test
    void preservesSdkExceptionIdentity() {
        var client = mock(MilvusClientV2.class);
        var request = SearchReq.builder().collectionName("books").topK(2).build();
        var failure = failure();
        when(client.search(request)).thenThrow(failure);
        var traced = new TracedMilvusClient(client, tracer);
        assertThatThrownBy(() -> traced.search(request)).isSameAs(failure);
        assertError(MilvusClientException.class);
    }

    @ParameterizedTest
    @ValueSource(strings = {"search", "hybridSearch", "query", "get", "insert", "upsert", "delete"})
    void syncExceptionsAndErrorsAreNotReplaced(String operation) {
        for (Throwable failure : List.of(failure(), new AssertionError("synthetic error"))) {
            exporter.reset();
            var client = mock(MilvusClientV2.class, invocation -> { throw failure; });
            var traced = new TracedMilvusClient(client, tracer);
            assertThatThrownBy(() -> syncCall(operation, traced)).isSameAs(failure);
            assertError(failure.getClass());
            assertThat(Span.current().getSpanContext().isValid()).isFalse();
        }
    }

    @ParameterizedTest
    @ValueSource(strings = {"search", "hybridSearch", "query", "get", "insert", "upsert", "delete"})
    void syncDelegatesOncePreservesResponseAndParent(String operation) {
        Object response = switch (operation) {
            case "search", "hybridSearch" -> searchResponse();
            case "query" -> QueryResp.builder().queryResults(List.of()).build();
            case "get" -> GetResp.builder().getResults(List.of()).build();
            case "insert" -> InsertResp.builder().InsertCnt(2L).build();
            case "upsert" -> UpsertResp.builder().upsertCnt(2L).build();
            default -> DeleteResp.builder().deleteCnt(2L).build();
        };
        AtomicReference<String> active = new AtomicReference<>();
        var client = mock(MilvusClientV2.class, invocation -> {
            active.set(Span.current().getSpanContext().getSpanId());
            return response;
        });
        var traced = new TracedMilvusClient(client, tracer);
        Span parent = provider.get("test").spanBuilder("parent").startSpan();
        try (Scope ignored = parent.makeCurrent()) {
            assertThat(syncCall(operation, traced)).isSameAs(response);
            assertThat(Span.current()).isSameAs(parent);
        } finally {
            parent.end();
        }
        var span = exporter.getFinishedSpanItems().get(0);
        assertThat(span.getParentSpanId()).isEqualTo(parent.getSpanContext().getSpanId());
        assertThat(span.getSpanId()).isEqualTo(active.get());
        assertThat(span.getStatus().getStatusCode()).isEqualTo(StatusCode.OK);
        assertThat(span.getAttributes().get(AttributeKey.stringKey("db.system"))).isEqualTo("milvus");
        assertThat(mockingDetails(client).getInvocations()).hasSize(1);
        assertThat(traced.unwrap()).isSameAs(client);
    }

    @Test
    void searchCountsAllQueryVectorsWithoutExportingRows() {
        var client = mock(MilvusClientV2.class);
        when(client.search(any())).thenReturn(searchResponse());
        new TracedMilvusClient(client, tracer).search(SearchReq.builder().collectionName("books")
                .filter("private-filter").topK(2).build());
        var span = exporter.getFinishedSpanItems().get(0);
        assertThat(span.getAttributes().get(AttributeKey.longKey(SemanticConventions.DB_VECTOR_RESULTS_COUNT)))
                .isEqualTo(3L);
        assertThat(span.getAttributes().toString()).doesNotContain("private-filter", "private-document", "private-id");
    }

    @ParameterizedTest
    @ValueSource(strings = {"searchAsync", "hybridSearchAsync", "queryAsync", "getAsync"})
    void asyncWaitsForCompletionAndKeepsOriginalFuture(String operation) throws Exception {
        var future = new CompletableFuture<Object>();
        var client = mock(MilvusClientV2.class, invocation -> future);
        var traced = new TracedMilvusClient(client, tracer);
        if (assertUnsupportedOnFloor(operation, traced)) return;
        Object response = switch (operation) {
            case "queryAsync" -> QueryResp.builder().queryResults(List.of()).build();
            case "getAsync" -> GetResp.builder().getResults(List.of()).build();
            default -> searchResponse();
        };
        Span parent = provider.get("test").spanBuilder("parent").startSpan();
        try (Scope ignored = parent.makeCurrent()) {
            assertThat(asyncCall(operation, traced)).isSameAs(future);
            assertThat(Span.current()).isSameAs(parent);
        }
        assertThat(exporter.getFinishedSpanItems()).isEmpty();
        AtomicReference<Throwable> threadFailure = new AtomicReference<>();
        Thread worker = new Thread(() -> {
            try {
                future.complete(response);
                assertThat(Span.current().getSpanContext().isValid()).isFalse();
            } catch (Throwable failure) {
                threadFailure.set(failure);
            }
        });
        worker.start();
        worker.join(5000);
        assertThat(worker.isAlive()).isFalse();
        assertThat(threadFailure.get()).isNull();
        assertThat(future.join()).isSameAs(response);
        assertThat(exporter.getFinishedSpanItems()).hasSize(1);
        var span = exporter.getFinishedSpanItems().get(0);
        assertThat(span.getParentSpanId()).isEqualTo(parent.getSpanContext().getSpanId());
        assertThat(span.getStatus().getStatusCode()).isEqualTo(StatusCode.OK);
        assertThat(mockingDetails(client).getInvocations()).hasSize(1);
        parent.end();
    }

    @ParameterizedTest
    @ValueSource(strings = {"searchAsync", "hybridSearchAsync", "queryAsync", "getAsync"})
    void asyncFailureCancellationAndImmediateCompletionEndExactlyOnce(String operation) {
        for (int scenario = 0; scenario < 4; scenario++) {
            exporter.reset();
            var future = new CompletableFuture<Object>();
            var client = mock(MilvusClientV2.class, invocation -> future);
            var traced = new TracedMilvusClient(client, tracer);
            if (assertUnsupportedOnFloor(operation, traced)) return;
            MilvusClientException failure = failure();
            if (scenario == 2) future.completeExceptionally(failure);
            if (scenario == 3) future.complete(null);
            CompletableFuture<?> returned = asyncCall(operation, traced);
            assertThat(returned).isSameAs(future);
            if (scenario == 0) future.completeExceptionally(new CompletionException(failure));
            if (scenario == 1) {
                assertThat(returned.cancel(true)).isTrue();
                assertThat(future.isCancelled()).isTrue();
            }
            assertThat(exporter.getFinishedSpanItems()).hasSize(1);
            if (scenario == 0 || scenario == 2) {
                assertThatThrownBy(returned::join).hasCause(failure);
                assertError(MilvusClientException.class);
            }
            assertThat(exporter.getFinishedSpanItems().get(0).getStatus().getStatusCode())
                    .isEqualTo(scenario == 3 ? StatusCode.OK : StatusCode.ERROR);
        }
    }

    @ParameterizedTest
    @ValueSource(strings = {"searchAsync", "hybridSearchAsync", "queryAsync", "getAsync"})
    void asyncSynchronousSdkThrowIsPreserved(String operation) {
        var failure = failure();
        var client = mock(MilvusClientV2.class, invocation -> { throw failure; });
        var traced = new TracedMilvusClient(client, tracer);
        if (assertUnsupportedOnFloor(operation, traced)) return;
        assertThatThrownBy(() -> asyncCall(operation, traced)).isSameAs(failure);
        assertError(MilvusClientException.class);
    }

    @ParameterizedTest
    @ValueSource(strings = {"queryIterator", "searchIterator", "searchIteratorV2"})
    void iteratorIsLazyBoundedAndCloseable(String operation) throws Exception {
        var query = mock(QueryIterator.class);
        var search = mock(SearchIterator.class);
        var searchV2 = mock(SearchIteratorV2.class);
        List<RowRecord> rows = List.of(mock(RowRecord.class));
        List<SearchResp.SearchResult> results = searchResponse().getSearchResults().get(0);
        when(query.next()).thenReturn(rows, List.of());
        when(search.next()).thenReturn(rows, List.of());
        when(searchV2.next()).thenReturn(results, List.of());
        Object sdkIterator = switch (operation) {
            case "queryIterator" -> query;
            case "searchIterator" -> search;
            default -> searchV2;
        };
        var client = mock(MilvusClientV2.class, invocation -> sdkIterator);
        Span parent = provider.get("test").spanBuilder("parent").startSpan();
        TracedMilvusIterator<?> iterator;
        try (Scope ignored = parent.makeCurrent()) {
            iterator = iteratorCall(operation, new TracedMilvusClient(client, tracer));
        }
        verifyNoInteractions(query, search, searchV2);
        assertThat(exporter.getFinishedSpanItems()).hasSize(1);
        var otherParent = provider.get("test").spanBuilder("other").startSpan();
        try (Scope ignored = otherParent.makeCurrent(); iterator) {
            assertThat(iterator.next()).isSameAs(operation.equals("searchIteratorV2") ? results : rows);
            assertThat(iterator.next()).isEmpty();
            assertThat(Span.current()).isSameAs(otherParent);
        }
        List<SpanData> spans = exporter.getFinishedSpanItems();
        assertThat(spans).hasSize(4);
        assertThat(spans).allSatisfy(span -> {
            assertThat(span.getParentSpanId()).isEqualTo(parent.getSpanContext().getSpanId());
            assertThat(span.getStatus().getStatusCode()).isEqualTo(StatusCode.OK);
        });
        assertThat(mockingDetails(sdkIterator).getInvocations()).hasSize(3);
        assertThat(spans.get(2).getAttributes().get(AttributeKey.longKey(SemanticConventions.DB_VECTOR_RESULTS_COUNT)))
                .isZero();
        parent.end();
        otherParent.end();
    }

    @Test
    void iteratorCreationNextAndCloseErrorsPreserveIdentity() {
        var client = mock(MilvusClientV2.class);
        var failure = failure();
        var request = QueryIteratorReq.builder().collectionName("books").build();
        when(client.queryIterator(request)).thenThrow(failure);
        var traced = new TracedMilvusClient(client, tracer);
        assertThatThrownBy(() -> traced.queryIterator(request)).isSameAs(failure);
        assertError(MilvusClientException.class);
        var sdkIterator = mock(QueryIterator.class);
        when(sdkIterator.next()).thenThrow(failure);
        doThrow(failure).when(sdkIterator).close();
        doReturn(sdkIterator).when(client).queryIterator(request);
        var iterator = traced.queryIterator(request);
        exporter.reset();
        assertThatThrownBy(iterator::next).isSameAs(failure);
        assertError(MilvusClientException.class);
        exporter.reset();
        assertThatThrownBy(iterator::close).isSameAs(failure);
        assertError(MilvusClientException.class);
    }

    @Test
    void coversNativeVectorOperations() throws Exception {
        for (String name : new String[]{"hybridSearch", "searchAsync", "queryAsync", "getAsync",
                "hybridSearchAsync", "queryIterator", "searchIterator", "searchIteratorV2"}) {
            assertThat(TracedMilvusClient.class.getMethod(name, requestType(name))).isNotNull();
        }
    }

    private boolean assertUnsupportedOnFloor(String operation, TracedMilvusClient traced) {
        try {
            MilvusClientV2.class.getMethod(operation, requestType(operation));
            return false;
        } catch (NoSuchMethodException missing) {
            assertThatThrownBy(() -> asyncCall(operation, traced))
                    .isInstanceOf(UnsupportedOperationException.class).hasMessageContaining("3.0.12");
            assertError(UnsupportedOperationException.class);
            return true;
        }
    }

    private void assertError(Class<?> type) {
        assertThat(exporter.getFinishedSpanItems()).hasSize(1);
        var span = exporter.getFinishedSpanItems().get(0);
        assertThat(span.getStatus().getStatusCode()).isEqualTo(StatusCode.ERROR);
        assertThat(span.getStatus().getDescription()).isEmpty();
        assertThat(span.getAttributes().get(AttributeKey.stringKey("error.type"))).isEqualTo(type.getName());
        assertThat(span.getEvents()).hasSize(1);
        assertThat(span.getEvents().get(0).getAttributes().size()).isEqualTo(1);
    }

    private static MilvusClientException failure() {
        return new MilvusClientException(ErrorCode.SERVER_ERROR, "private-sdk-error");
    }

    static SearchResp searchResponse() {
        var first = SearchResp.SearchResult.builder().id("private-id").score(0.9f)
                .entity(Map.of("text", "private-document")).build();
        return SearchResp.builder().searchResults(List.of(List.of(first, first), List.of(first))).build();
    }

    private static Object syncCall(String operation, TracedMilvusClient traced) {
        return switch (operation) {
            case "search" -> traced.search(SearchReq.builder().collectionName("books").build());
            case "hybridSearch" -> traced.hybridSearch(HybridSearchReq.builder().collectionName("books").build());
            case "query" -> traced.query(QueryReq.builder().collectionName("books").filter("private-filter").build());
            case "get" -> traced.get(GetReq.builder().collectionName("books").build());
            case "insert" -> traced.insert(InsertReq.builder().collectionName("books").build());
            case "upsert" -> traced.upsert(UpsertReq.builder().collectionName("books").build());
            default -> traced.delete(DeleteReq.builder().collectionName("books").filter("private-filter").build());
        };
    }

    private static CompletableFuture<?> asyncCall(String operation, TracedMilvusClient traced) {
        return switch (operation) {
            case "searchAsync" -> traced.searchAsync(SearchReq.builder().collectionName("books").build());
            case "hybridSearchAsync" -> traced.hybridSearchAsync(HybridSearchReq.builder().collectionName("books").build());
            case "queryAsync" -> traced.queryAsync(QueryReq.builder().collectionName("books").build());
            default -> traced.getAsync(GetReq.builder().collectionName("books").build());
        };
    }

    private static TracedMilvusIterator<?> iteratorCall(String operation, TracedMilvusClient traced) {
        return switch (operation) {
            case "queryIterator" -> traced.queryIterator(QueryIteratorReq.builder().collectionName("books").build());
            case "searchIterator" -> traced.searchIterator(SearchIteratorReq.builder().collectionName("books").build());
            default -> traced.searchIteratorV2(SearchIteratorReqV2.builder().collectionName("books").build());
        };
    }

    private static Class<?> requestType(String operation) {
        return switch (operation) {
            case "hybridSearch", "hybridSearchAsync" -> HybridSearchReq.class;
            case "searchAsync" -> SearchReq.class;
            case "queryAsync" -> QueryReq.class;
            case "getAsync" -> GetReq.class;
            case "queryIterator" -> QueryIteratorReq.class;
            case "searchIterator" -> SearchIteratorReq.class;
            default -> SearchIteratorReqV2.class;
        };
    }
}
