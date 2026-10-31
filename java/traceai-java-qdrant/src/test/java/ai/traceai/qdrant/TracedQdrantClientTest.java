package ai.traceai.qdrant;

import ai.traceai.FITracer;
import ai.traceai.SemanticConventions;
import ai.traceai.TraceConfig;
import com.google.common.util.concurrent.Futures;
import com.google.common.util.concurrent.ListenableFuture;
import com.google.common.util.concurrent.SettableFuture;
import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.context.Scope;
import io.opentelemetry.sdk.testing.exporter.InMemorySpanExporter;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.data.SpanData;
import io.opentelemetry.sdk.trace.export.SimpleSpanProcessor;
import io.qdrant.client.QdrantClient;
import io.qdrant.client.grpc.Collections.CollectionOperationResponse;
import io.qdrant.client.grpc.Collections.Distance;
import io.qdrant.client.grpc.Collections.VectorParams;
import io.qdrant.client.grpc.Common.Filter;
import io.qdrant.client.grpc.Common.PointId;
import io.qdrant.client.grpc.Points.*;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.EnumSource;

import java.time.Duration;
import java.util.List;
import java.util.concurrent.CancellationException;
import java.util.concurrent.ExecutionException;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicReference;

import static org.assertj.core.api.Assertions.*;
import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;

class TracedQdrantClientTest {
    private InMemorySpanExporter exporter;
    private SdkTracerProvider provider;
    private FITracer tracer;
    private QdrantClient client;
    private TracedQdrantClient traced;

    private static final QueryPoints QUERY = QueryPoints.newBuilder().setCollectionName("documents")
            .setLimit(3).addPrefetch(PrefetchQuery.newBuilder().setLimit(20)).build();
    private static final SearchPoints SEARCH = SearchPoints.newBuilder().setCollectionName("documents")
            .setLimit(3).addAllVector(List.of(0.1f, 0.2f)).setFilter(Filter.getDefaultInstance()).build();
    private static final QueryPointGroups GROUPS = QueryPointGroups.newBuilder()
            .setCollectionName("documents").setGroupBy("category").setGroupSize(2).setLimit(3).build();
    private static final ScoredPoint POINT = ScoredPoint.newBuilder().setScore(0.9f).build();
    private static final UpdateResult UPDATE = UpdateResult.newBuilder().setStatus(UpdateStatus.Completed).build();
    private static final List<PointId> IDS = List.of(PointId.newBuilder().setNum(42).build());

    enum Operation { SEARCH, QUERY, BATCH, GROUPS, UPSERT, DELETE, GET, CREATE, LIST }

    @BeforeEach
    void setUp() {
        exporter = InMemorySpanExporter.create();
        provider = SdkTracerProvider.builder().addSpanProcessor(SimpleSpanProcessor.create(exporter)).build();
        tracer = new FITracer(provider.get("qdrant-test"), config());
        client = mock(QdrantClient.class);
        traced = new TracedQdrantClient(client, tracer);
    }

    static TraceConfig config() {
        return TraceConfig.builder().apiKey("synthetic").secretKey("synthetic")
                .baseUrl("http://localhost").projectName("qdrant-test").hideErrorDetails(true).build();
    }

    @AfterEach
    void tearDown() {
        provider.close();
    }

    @Test
    void exposesNonBlockingUniversalQuery() throws Exception {
        assertThat(TracedQdrantClient.class.getMethod("queryAsync", QueryPoints.class)
                .getReturnType()).isEqualTo(ListenableFuture.class);
    }

    @ParameterizedTest
    @EnumSource(Operation.class)
    void asyncOperationsRemainOpenUntilCompletionAndPreserveFuture(Operation operation) throws Exception {
        SettableFuture<Object> pending = SettableFuture.create();
        AtomicReference<Span> sdkSpan = new AtomicReference<>();
        client = mock(QdrantClient.class, invocation -> {
            sdkSpan.set(Span.current());
            return pending;
        });
        traced = new TracedQdrantClient(client, tracer);
        Span parent = provider.get("test").spanBuilder("parent").startSpan();
        ListenableFuture<?> returned;
        try (Scope ignored = parent.makeCurrent()) {
            returned = invoke(operation);
            assertThat(Span.current()).isSameAs(parent);
        }
        assertThat(returned).isSameAs(pending);
        assertThat(returned.isDone()).isFalse();
        assertThat(exporter.getFinishedSpanItems()).isEmpty();
        assertThat(sdkSpan.get().getSpanContext().isValid()).isTrue();
        assertThat(sdkSpan.get().getSpanContext()).isNotEqualTo(parent.getSpanContext());

        Object response = result(operation);
        // Complete on another thread, with an unrelated current span. Neither the
        // caller's scope nor the operation scope may leak onto this thread.
        var executor = Executors.newSingleThreadExecutor();
        try {
            executor.submit(() -> {
                Span unrelated = provider.get("test").spanBuilder("unrelated").startSpan();
                try (Scope ignored = unrelated.makeCurrent()) {
                    pending.set(response);
                    assertThat(Span.current()).isSameAs(unrelated);
                } finally {
                    unrelated.end();
                }
            }).get(5, TimeUnit.SECONDS);
        } finally {
            executor.shutdownNow();
        }
        assertThat(returned.get()).isSameAs(response);
        List<SpanData> spans = exporter.getFinishedSpanItems().stream()
                .filter(span -> span.getName().startsWith("Qdrant ")).toList();
        assertThat(spans).hasSize(1);
        SpanData span = spans.get(0);
        assertThat(span.getParentSpanId()).isEqualTo(parent.getSpanContext().getSpanId());
        assertThat(span.getSpanId()).isEqualTo(sdkSpan.get().getSpanContext().getSpanId());
        assertThat(span.getStatus().getStatusCode()).isEqualTo(StatusCode.OK);
        assertThat(span.getAttributes().get(AttributeKey.stringKey(SemanticConventions.DB_SYSTEM)))
                .isEqualTo("qdrant");
        assertThat(span.getAttributes().get(AttributeKey.stringKey(SemanticConventions.FI_SPAN_KIND)))
                .isEqualTo(switch (operation) {
                    case SEARCH, QUERY, BATCH, GROUPS -> "RETRIEVER";
                    default -> "VECTOR_DB";
                });
        parent.end();
    }

    @ParameterizedTest
    @EnumSource(Operation.class)
    void failurePreservesOriginalCauseAndRespectsErrorPrivacy(Operation operation) {
        SettableFuture<Object> pending = SettableFuture.create();
        client = mock(QdrantClient.class, invocation -> pending);
        traced = new TracedQdrantClient(client, tracer);
        ListenableFuture<?> future = invoke(operation);
        RuntimeException failure = new IllegalStateException("private-payload-sentinel");
        pending.setException(failure);
        assertThatThrownBy(future::get).isInstanceOf(ExecutionException.class).hasCause(failure);
        SpanData span = onlySpan();
        assertThat(span.getStatus().getStatusCode()).isEqualTo(StatusCode.ERROR);
        assertThat(span.getStatus().getDescription()).isEmpty();
        assertThat(span.getAttributes().get(AttributeKey.stringKey(SemanticConventions.ERROR_TYPE)))
                .isEqualTo(IllegalStateException.class.getName());
        assertThat(span.getEvents()).hasSize(1);
        assertThat(span.toString()).doesNotContain("private-payload-sentinel");
    }

    @ParameterizedTest
    @EnumSource(Operation.class)
    void cancellationFinishesOneSpanAndCancelsSdkFuture(Operation operation) {
        SettableFuture<Object> pending = SettableFuture.create();
        client = mock(QdrantClient.class, invocation -> pending);
        traced = new TracedQdrantClient(client, tracer);
        ListenableFuture<?> future = invoke(operation);
        assertThat(future.cancel(true)).isTrue();
        assertThat(pending.isCancelled()).isTrue();
        assertThatThrownBy(future::get).isInstanceOf(CancellationException.class);
        assertThat(onlySpan().getStatus().getStatusCode()).isEqualTo(StatusCode.ERROR);
        assertThat(pending.set(result(operation))).isFalse();
        assertThat(exporter.getFinishedSpanItems()).hasSize(1);
    }

    @ParameterizedTest
    @EnumSource(Operation.class)
    void synchronousSdkFailureFinishesSpanAndRestoresContext(Operation operation) {
        IllegalArgumentException failure = new IllegalArgumentException("invalid-request");
        client = mock(QdrantClient.class, invocation -> { throw failure; });
        traced = new TracedQdrantClient(client, tracer);
        Span before = Span.current();
        assertThatThrownBy(() -> invoke(operation)).isSameAs(failure);
        assertThat(Span.current()).isSameAs(before);
        assertThat(onlySpan().getStatus().getStatusCode()).isEqualTo(StatusCode.ERROR);
    }

    @Test
    void forwardsUniversalRequestTimeoutConsistencyAndCountsWithoutChangingPayloads() throws Exception {
        Duration timeout = Duration.ofSeconds(7);
        ReadConsistency consistency = ReadConsistency.newBuilder().setFactor(2).build();
        var queries = List.of(QUERY);
        var queryResult = List.of(POINT);
        var batchResult = List.of(BatchResult.newBuilder().addResult(POINT).addResult(POINT).build());
        var groupResult = List.of(PointGroup.newBuilder().addHits(POINT).addHits(POINT).build());
        when(client.queryAsync(same(QUERY), same(timeout))).thenReturn(Futures.immediateFuture(queryResult));
        when(client.queryBatchAsync(eq("documents"), same(queries), same(consistency), same(timeout)))
                .thenReturn(Futures.immediateFuture(batchResult));
        when(client.queryGroupsAsync(same(GROUPS), same(timeout))).thenReturn(Futures.immediateFuture(groupResult));
        assertThat(traced.queryAsync(QUERY, timeout).get()).isSameAs(queryResult);
        assertThat(traced.queryBatchAsync("documents", queries, consistency, timeout).get()).isSameAs(batchResult);
        assertThat(traced.queryGroupsAsync(GROUPS, timeout).get()).isSameAs(groupResult);
        verify(client).queryAsync(same(QUERY), same(timeout));
        verify(client).queryBatchAsync(eq("documents"), same(queries), same(consistency), same(timeout));
        verify(client).queryGroupsAsync(same(GROUPS), same(timeout));
        assertThat(exporter.getFinishedSpanItems()).extracting(span -> span.getAttributes()
                .get(AttributeKey.longKey(SemanticConventions.DB_VECTOR_RESULTS_COUNT)))
                .containsExactly(1L, 2L, 2L);
        assertThat(exporter.getFinishedSpanItems().get(0).getAttributes()
                .get(AttributeKey.longKey("qdrant.prefetch_count"))).isEqualTo(1L);
    }

    @Test
    void blockingEntryPointsStillReturnOriginalResultsWithOneSpanEach() throws Exception {
        when(client.searchAsync(any(SearchPoints.class), isNull()))
                .thenReturn(Futures.immediateFuture(List.of(POINT)));
        when(client.queryAsync(same(QUERY), isNull())).thenReturn(Futures.immediateFuture(List.of(POINT)));
        when(client.queryBatchAsync(eq("documents"), anyList(), isNull(), isNull()))
                .thenReturn(Futures.immediateFuture(List.of(BatchResult.getDefaultInstance())));
        when(client.queryGroupsAsync(same(GROUPS), isNull()))
                .thenReturn(Futures.immediateFuture(List.of(PointGroup.getDefaultInstance())));
        when(client.upsertAsync(eq("documents"), anyList())).thenReturn(Futures.immediateFuture(UPDATE));
        when(client.deleteAsync(eq("documents"), any(Filter.class))).thenReturn(Futures.immediateFuture(UPDATE));
        when(client.retrieveAsync(eq("documents"), same(IDS), eq(true), eq(false), isNull()))
                .thenReturn(Futures.immediateFuture(List.of(RetrievedPoint.getDefaultInstance())));
        when(client.createCollectionAsync(eq("documents"), any(VectorParams.class)))
                .thenReturn(Futures.immediateFuture(CollectionOperationResponse.getDefaultInstance()));
        when(client.listCollectionsAsync()).thenReturn(Futures.immediateFuture(List.of("documents")));
        assertThat(traced.search("documents", List.of(0.1f, 0.2f), 3)).containsExactly(POINT);
        assertThat(traced.search(SEARCH)).containsExactly(POINT);
        assertThat(traced.query(QUERY)).containsExactly(POINT);
        assertThat(traced.queryBatch("documents", List.of(QUERY))).hasSize(1);
        assertThat(traced.queryGroups(GROUPS)).hasSize(1);
        assertThat(traced.upsert("documents", List.of())).isSameAs(UPDATE);
        assertThat(traced.delete("documents", Filter.getDefaultInstance())).isSameAs(UPDATE);
        assertThat(traced.get("documents", IDS, true, false)).hasSize(1);
        traced.createCollection("documents", 2, Distance.Cosine);
        assertThat(traced.listCollections()).containsExactly("documents");
        assertThat(traced.unwrap()).isSameAs(client);
        assertThat(exporter.getFinishedSpanItems()).hasSize(10);
        assertThat(exporter.getFinishedSpanItems()).allMatch(span -> span.getStatus().getStatusCode() == StatusCode.OK);
    }

    @Test
    void blockingFailureKeepsExecutionExceptionContract() {
        RuntimeException failure = new IllegalStateException("offline failure");
        when(client.searchAsync(any(SearchPoints.class), isNull())).thenReturn(Futures.immediateFailedFuture(failure));
        assertThatThrownBy(() -> traced.search(SEARCH)).isInstanceOf(ExecutionException.class).hasCause(failure);
        assertThat(onlySpan().getStatus().getStatusCode()).isEqualTo(StatusCode.ERROR);
    }

    private SpanData onlySpan() {
        assertThat(exporter.getFinishedSpanItems()).hasSize(1);
        return exporter.getFinishedSpanItems().get(0);
    }

    private ListenableFuture<?> invoke(Operation operation) {
        return switch (operation) {
            case SEARCH -> traced.searchAsync(SEARCH);
            case QUERY -> traced.queryAsync(QUERY);
            case BATCH -> traced.queryBatchAsync("documents", List.of(QUERY));
            case GROUPS -> traced.queryGroupsAsync(GROUPS);
            case UPSERT -> traced.upsertAsync("documents", List.of(PointStruct.getDefaultInstance()));
            case DELETE -> traced.deleteAsync("documents", Filter.getDefaultInstance());
            case GET -> traced.getAsync("documents", IDS, true, false);
            case CREATE -> traced.createCollectionAsync("documents", 2, Distance.Cosine);
            case LIST -> traced.listCollectionsAsync();
        };
    }

    private Object result(Operation operation) {
        return switch (operation) {
            case SEARCH, QUERY -> List.of(POINT);
            case BATCH -> List.of(BatchResult.newBuilder().addResult(POINT).build());
            case GROUPS -> List.of(PointGroup.newBuilder().addHits(POINT).build());
            case UPSERT, DELETE -> UPDATE;
            case GET -> List.of(RetrievedPoint.getDefaultInstance());
            case CREATE -> CollectionOperationResponse.getDefaultInstance();
            case LIST -> List.of("documents");
        };
    }
}
