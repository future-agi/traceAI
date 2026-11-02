package ai.traceai.weaviate;

import ai.traceai.FITracer;
import ai.traceai.SemanticConventions;
import ai.traceai.TraceConfig;
import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.sdk.testing.exporter.InMemorySpanExporter;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.data.SpanData;
import io.opentelemetry.sdk.trace.export.SimpleSpanProcessor;
import io.weaviate.client6.v1.api.collections.WeaviateObject;
import io.weaviate.client6.v1.api.collections.data.*;
import io.weaviate.client6.v1.api.collections.query.*;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;

import java.util.Map;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.TimeUnit;
import java.util.function.BooleanSupplier;

import static org.assertj.core.api.Assertions.*;

class TracedWeaviateV6Test {
    private WeaviateV6Fixture server;
    private InMemorySpanExporter exporter;
    private SdkTracerProvider provider;
    private FITracer tracer;

    @BeforeEach void setUp() throws Exception {
        server = new WeaviateV6Fixture();
        exporter = InMemorySpanExporter.create();
        provider = SdkTracerProvider.builder().addSpanProcessor(SimpleSpanProcessor.create(exporter)).build();
        tracer = new FITracer(provider.get("weaviate-test"), TraceConfig.builder().hideErrorDetails(true).build());
    }

    @AfterEach void tearDown() throws Exception {
        provider.close();
        server.close();
    }

    @ParameterizedTest
    @ValueSource(strings = {"fetchObjects", "nearVector", "nearText", "nearObject", "bm25", "hybrid"})
    void queriesPreserveNativeRequestsResultsAndParentage(String operation) throws Exception {
        try (var client = server.client()) {
            var handle = client.collections.use("Article").withDefaults(d -> d.tenant("tenant-a").consistencyLevel(ConsistencyLevel.QUORUM));
            var traced = new TracedWeaviateCollection<>("Article", handle, tracer);
            var query = NearVector.of(Target.vector("title-vector", new float[]{0.1f, 0.2f}),
                    q -> q.limit(7).offset(2).filters(Filter.property("title").eq(WeaviateV6Fixture.PRIVATE)).returnProperties("title"));
            Object nativeResult = switch (operation) {
                case "nearVector" -> handle.query.nearVector(query);
                case "nearText" -> handle.query.nearText(NearText.of(WeaviateV6Fixture.PRIVATE));
                case "nearObject" -> handle.query.nearObject(NearObject.of(WeaviateV6Fixture.UUID));
                case "bm25" -> handle.query.bm25(Bm25.of(WeaviateV6Fixture.PRIVATE));
                case "hybrid" -> handle.query.hybrid(Hybrid.of(WeaviateV6Fixture.PRIVATE));
                default -> handle.query.fetchObjects(FetchObjects.of(q -> q.limit(3)));
            };
            Span parent = provider.get("test").spanBuilder("parent").startSpan();
            Object result;
            try (var ignored = parent.makeCurrent()) {
                result = switch (operation) {
                    case "nearVector" -> traced.nearVector(query);
                    case "nearText" -> traced.nearText(NearText.of(WeaviateV6Fixture.PRIVATE));
                    case "nearObject" -> traced.nearObject(NearObject.of(WeaviateV6Fixture.UUID));
                    case "bm25" -> traced.bm25(Bm25.of(WeaviateV6Fixture.PRIVATE));
                    case "hybrid" -> traced.hybrid(Hybrid.of(WeaviateV6Fixture.PRIVATE));
                    default -> traced.fetchObjects(FetchObjects.of(q -> q.limit(3)));
                };
                assertThat(Span.current()).isSameAs(parent);
            } finally {
                parent.end();
            }
            assertThat(result).usingRecursiveComparison().isEqualTo(nativeResult);
            assertThat(server.searches).hasSize(2);
            assertThat(server.searches.get(1)).isEqualTo(server.searches.get(0));
            assertThat(server.searches.get(1).getTenant()).isEqualTo("tenant-a");
            var span = exporter.getFinishedSpanItems().get(0);
            assertThat(exporter.getFinishedSpanItems()).hasSize(2);
            assertThat(span.getParentSpanId()).isEqualTo(parent.getSpanContext().getSpanId());
            assertThat(span.getAttributes().get(AttributeKey.stringKey(SemanticConventions.FI_SPAN_KIND))).isEqualTo("RETRIEVER");
            assertThat(span.getAttributes().get(AttributeKey.longKey(SemanticConventions.DB_VECTOR_RESULTS_COUNT))).isEqualTo(1L);
            if (operation.equals("nearVector")) {
                assertThat(span.getAttributes().get(AttributeKey.longKey(SemanticConventions.RETRIEVER_TOP_K))).isEqualTo(7L);
            }
            assertPrivate(span);
            assertThat(traced.unwrap()).isSameAs(handle);
        }
    }

    @ParameterizedTest
    @ValueSource(strings = {"fetchObjects", "nearVector", "nearText", "nearObject", "bm25", "hybrid"})
    void asyncAndGroupedQueriesPreserveNativeOptions(String operation) throws Exception {
        try (var client = server.client(); var asyncClient = server.asyncClient()) {
            var sync = TracedWeaviateCollection.of(client, "Article", tracer);
            var async = TracedWeaviateCollectionAsync.of(asyncClient, "Article", tracer);
            Object request = switch (operation) {
                case "nearVector" -> NearVector.of(new float[]{1f, 2f});
                case "nearText" -> NearText.of("observability");
                case "nearObject" -> NearObject.of(WeaviateV6Fixture.UUID);
                case "bm25" -> Bm25.of("observability");
                case "hybrid" -> Hybrid.of("observability");
                default -> FetchObjects.of(q -> q.limit(3));
            };
            Object syncResult = sync.getClass().getMethod(operation, request.getClass()).invoke(sync, request);
            var asyncResult = (CompletableFuture<?>) async.getClass().getMethod(operation, request.getClass()).invoke(async, request);
            assertThat(asyncResult.get(5, TimeUnit.SECONDS)).usingRecursiveComparison().isEqualTo(syncResult);
            var group = GroupBy.property("title", 2, 3);
            Object grouped = sync.getClass().getMethod(operation, request.getClass(), GroupBy.class).invoke(sync, request, group);
            var groupedAsync = (CompletableFuture<?>) async.getClass().getMethod(operation, request.getClass(), GroupBy.class).invoke(async, request, group);
            assertThat(groupedAsync.get(5, TimeUnit.SECONDS)).usingRecursiveComparison().isEqualTo(grouped);
            assertThat(server.searches.get(0)).isEqualTo(server.searches.get(1));
            assertThat(server.searches.get(2)).isEqualTo(server.searches.get(3));
            assertThat(server.searches.get(2).hasGroupBy()).isTrue();
            await(() -> exporter.getFinishedSpanItems().size() == 4);
        }
    }

    @Test void groupedAndEmptyQueriesRetainSdkBehavior() throws Exception {
        try (var client = server.client()) {
            var handle = client.collections.use("Article");
            var traced = new TracedWeaviateCollection<>("Article", handle, tracer);
            var group = GroupBy.property("title", 2, 3);
            var request = NearVector.of(new float[]{1f, 2f});
            assertThat(traced.nearVector(request, group)).isEqualTo(handle.query.nearVector(request, group));
            assertThat(server.searches.get(0)).isEqualTo(server.searches.get(1));
            server.emptySearch = true;
            assertThat(traced.fetchObjectById(WeaviateV6Fixture.UUID)).isEmpty();
            assertThat(traced.fetchObjectById(WeaviateV6Fixture.UUID, q -> q.returnProperties("title"))).isEmpty();
            assertThat(traced.fetchObjects(FetchObjects.of())).isEqualTo(handle.query.fetchObjects());
            assertThat(exporter.getFinishedSpanItems()).hasSize(4).allSatisfy(s ->
                    assertThat(s.getAttributes().get(AttributeKey.longKey(SemanticConventions.DB_VECTOR_RESULTS_COUNT))).isZero());
        }
    }

    @Test void restWritesAndPartialBatchFailuresAreTracedWithoutPayloads() throws Exception {
        try (var client = server.client()) {
            var handle = client.collections.use("Article").withTenant("tenant-a");
            var traced = new TracedWeaviateCollection<>("Article", handle, tracer);
            var object = WeaviateObject.<Map<String, Object>>of(o -> o.uuid(WeaviateV6Fixture.UUID)
                    .properties(Map.of("title", WeaviateV6Fixture.PRIVATE)));
            var request = new InsertObjectRequest<>(object);
            assertThat(traced.insert(request)).isEqualTo(handle.data.insert(request));
            traced.update(WeaviateV6Fixture.UUID, u -> u.properties(Map.of("title", "changed")));
            traced.replace(WeaviateV6Fixture.UUID, r -> r.properties(Map.of("title", "replaced")));
            assertThat(traced.deleteById(WeaviateV6Fixture.UUID)).isTrue();
            assertThat(server.restRequests).anyMatch(r -> r.contains("tenant-a"));
            server.partialBatch = true;
            assertThat(traced.insertMany(new InsertManyRequest<>(object)).errors()).containsExactly(WeaviateV6Fixture.PRIVATE);
            assertThat(traced.deleteMany(DeleteManyRequest.of(Filter.uuid().eq(WeaviateV6Fixture.UUID))).failed()).isEqualTo(1);
            var spans = exporter.getFinishedSpanItems();
            assertThat(spans).hasSize(6).allSatisfy(TracedWeaviateV6Test::assertPrivate);
            assertThat(spans.subList(4, 6)).allSatisfy(s -> {
                assertThat(s.getStatus().getStatusCode()).isEqualTo(StatusCode.ERROR);
                assertThat(s.getAttributes().get(AttributeKey.longKey("weaviate.failed_count"))).isEqualTo(1L);
            });
        }
    }

    @Test void serverExceptionsStayNativeAndHonorHiddenErrorDetails() throws Exception {
        try (var client = server.client()) {
            var traced = TracedWeaviateCollection.of(client, "Article", tracer);
            server.failSearch = true;
            Throwable nativeError = catchThrowable(() -> client.collections.use("Article").query.fetchObjects());
            Throwable tracedError = catchThrowable(() -> traced.fetchObjects(FetchObjects.of()));
            assertThat(tracedError).isExactlyInstanceOf(nativeError.getClass()).hasMessage(nativeError.getMessage());
            server.failRest = true;
            assertThatThrownBy(() -> traced.insert(new InsertObjectRequest<>(WeaviateObject.of(o -> o.properties(Map.<String, Object>of())))))
                    .isInstanceOf(RuntimeException.class);
            assertThat(exporter.getFinishedSpanItems()).hasSize(2).allSatisfy(s -> {
                assertThat(s.getStatus().getStatusCode()).isEqualTo(StatusCode.ERROR);
                assertPrivate(s);
            });
        }
    }

    @ParameterizedTest
    @ValueSource(strings = {"success", "error", "cancel"})
    void asyncSpanLastsUntilCompletionWithoutLeakingCurrentContext(String outcome) throws Exception {
        try (var client = server.asyncClient()) {
            var traced = TracedWeaviateCollectionAsync.of(client, "Article", tracer);
            server.holdSearch = true;
            Span parent = provider.get("test").spanBuilder("parent").startSpan();
            CompletableFuture<QueryResponse<Map<String, Object>>> future;
            try (var ignored = parent.makeCurrent()) {
                future = traced.nearVector(NearVector.of(new float[]{1f, 2f}));
                assertThat(Span.current()).isSameAs(parent);
            }
            await(() -> server.pendingSearch != null);
            assertThat(exporter.getFinishedSpanItems()).isEmpty();
            if (outcome.equals("cancel")) {
                assertThat(future.cancel(true)).isTrue();
                assertThat(future).isCancelled();
            } else if (outcome.equals("error")) {
                server.pendingSearch.onError(io.grpc.Status.INTERNAL.withDescription(WeaviateV6Fixture.PRIVATE).asRuntimeException());
                assertThatThrownBy(() -> future.get(5, TimeUnit.SECONDS)).isInstanceOf(java.util.concurrent.ExecutionException.class);
            } else {
                server.pendingSearch.onNext(WeaviateV6Fixture.searchReply(false));
                server.pendingSearch.onCompleted();
                assertThat(future.get(5, TimeUnit.SECONDS).objects()).hasSize(1);
            }
            await(() -> exporter.getFinishedSpanItems().size() == 1);
            var span = exporter.getFinishedSpanItems().get(0);
            assertThat(span.getParentSpanId()).isEqualTo(parent.getSpanContext().getSpanId());
            assertThat(span.getStatus().getStatusCode()).isEqualTo(outcome.equals("success") ? StatusCode.OK : StatusCode.ERROR);
            assertPrivate(span);
            parent.end();
        }
    }

    @Test void asyncRestAndBatchOperationsUseTheRealSdk() throws Exception {
        try (var client = server.asyncClient()) {
            var traced = TracedWeaviateCollectionAsync.of(client, "Article", tracer);
            var object = WeaviateObject.<Map<String, Object>>of(o -> o.uuid(WeaviateV6Fixture.UUID)
                    .properties(Map.of("title", WeaviateV6Fixture.PRIVATE)));
            assertThat(traced.insert(new InsertObjectRequest<>(object)).get(5, TimeUnit.SECONDS).uuid()).isEqualTo(WeaviateV6Fixture.UUID);
            traced.update(WeaviateV6Fixture.UUID, u -> u.properties(Map.of("title", "changed"))).get(5, TimeUnit.SECONDS);
            traced.replace(WeaviateV6Fixture.UUID, r -> r.properties(Map.of("title", "replaced"))).get(5, TimeUnit.SECONDS);
            assertThat(traced.deleteById(WeaviateV6Fixture.UUID).get(5, TimeUnit.SECONDS)).isTrue();
            server.partialBatch = true;
            assertThat(traced.insertMany(new InsertManyRequest<>(object)).get(5, TimeUnit.SECONDS).errors()).hasSize(1);
            assertThat(traced.deleteMany(DeleteManyRequest.of(Filter.uuid().eq(WeaviateV6Fixture.UUID))).get(5, TimeUnit.SECONDS).failed()).isEqualTo(1);
            await(() -> exporter.getFinishedSpanItems().size() == 6);
            assertThat(exporter.getFinishedSpanItems()).allSatisfy(TracedWeaviateV6Test::assertPrivate);
        }
    }

    private static void assertPrivate(SpanData span) {
        assertThat(span.getAttributes().toString() + span.getEvents() + span.getStatus()).doesNotContain(WeaviateV6Fixture.PRIVATE);
    }

    static void await(BooleanSupplier condition) throws InterruptedException {
        long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(5);
        while (!condition.getAsBoolean() && System.nanoTime() < deadline) Thread.sleep(5);
        assertThat(condition.getAsBoolean()).isTrue();
    }
}
