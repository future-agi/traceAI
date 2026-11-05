package ai.traceai.azure.search.v12;

import ai.traceai.FITracer;
import ai.traceai.ContextAttributes;
import java.util.Map;
import org.junit.jupiter.params.provider.CsvSource;
import ai.traceai.SemanticConventions;
import ai.traceai.TraceConfig;
import com.azure.core.credential.AzureKeyCredential;
import com.azure.core.http.HttpHeaders;
import com.azure.core.http.HttpRequest;
import com.azure.core.http.HttpResponse;
import com.azure.core.http.policy.RetryOptions;
import com.azure.core.http.policy.FixedDelayOptions;
import com.azure.search.documents.SearchClient;
import com.azure.search.documents.SearchClientBuilder;
import com.azure.search.documents.models.SearchResult;
import com.azure.search.documents.models.SearchPagedIterable;
import com.azure.search.documents.models.SearchPagedResponse;
import io.opentelemetry.api.baggage.Baggage;
import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.context.Context;
import io.opentelemetry.context.ContextKey;
import io.opentelemetry.context.Scope;
import io.opentelemetry.exporter.otlp.http.trace.OtlpHttpSpanExporter;
import io.opentelemetry.proto.collector.trace.v1.ExportTraceServiceRequest;
import com.sun.net.httpserver.HttpServer;
import io.opentelemetry.sdk.OpenTelemetrySdk;
import io.opentelemetry.sdk.testing.exporter.InMemorySpanExporter;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.data.SpanData;
import io.opentelemetry.sdk.trace.export.SimpleSpanProcessor;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;
import reactor.core.publisher.Flux;
import reactor.core.publisher.Mono;

import java.net.InetSocketAddress;
import java.nio.ByteBuffer;
import java.util.HexFormat;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.LinkedBlockingQueue;
import java.util.concurrent.TimeUnit;
import java.nio.charset.Charset;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.Iterator;
import java.util.List;
import java.util.concurrent.atomic.AtomicInteger;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

class TracedSearchClientTest {
    private static final String FIRST_PAGE = """
        {"@odata.count":2,"@search.coverage":100.0,
         "value":[{"@search.score":2.0,"id":"one"}],
         "@odata.nextLink":"https://offline.search.windows.net/indexes('index')/docs/search.post.search?api-version=2023-11-01",
         "@search.nextPageParameters":{"search":"query","skip":1}}
        """;
    private static final String LAST_PAGE = """
        {"value":[{"@search.score":1.0,"id":"two"}]}
        """;

    private final List<Context> requestContexts = new java.util.concurrent.CopyOnWriteArrayList<>();
    private final AtomicInteger requests = new AtomicInteger();
    private final List<String> requestBodies = new java.util.concurrent.CopyOnWriteArrayList<>();
    private final InMemorySpanExporter exporter = InMemorySpanExporter.create();
    private final SdkTracerProvider provider = SdkTracerProvider.builder()
        .addSpanProcessor(SimpleSpanProcessor.create(exporter)).build();
    private final OpenTelemetrySdk telemetry = OpenTelemetrySdk.builder().setTracerProvider(provider).build();
    private final FITracer tracer = new FITracer(telemetry.getTracer("test"),
        TraceConfig.builder().hideInputs(true).hideOutputs(true).hideErrorDetails(true).build());
    private RuntimeException nextFailure;
    private String firstPage = FIRST_PAGE;

    private final SearchClient client = new SearchClientBuilder()
        .endpoint("https://offline.search.windows.net")
        .indexName("index")
        .credential(new AzureKeyCredential("offline-placeholder"))
        .retryOptions(new RetryOptions(new FixedDelayOptions(0, Duration.ofMillis(1))))
        .httpClient(request -> {
            requestContexts.add(Context.current());
            requests.incrementAndGet();
            if (nextFailure != null) {
                return Mono.error(nextFailure);
            }
            requestBodies.add(request.getBodyAsBinaryData().toString());
            // A transport child must run under the retrieval span, not the later consumer's context.
            Span transport = telemetry.getTracer("transport").spanBuilder("transport").startSpan();
            transport.end();
            return Mono.just(new JsonResponse(request, request.getBodyAsBinaryData().toString().contains("\"skip\":1") ? LAST_PAGE : firstPage));
        }).buildClient();

    @AfterEach
    void close() {
        provider.close();
    }

    private SearchPagedIterable search(String mode) {
        TracedSearchClient traced = new TracedSearchClient(client, "index", tracer);
        return switch (mode) {
            case "vector" -> traced.searchWithVector("query", List.of(1.0f, 2.0f), "embedding", 2);
            case "filtered" -> traced.searchWithVectorAndFilter("query", List.of(1.0f), "embedding", 2,
                "private-filter eq 'secret'");
            case "hybrid" -> traced.hybridSearch("query", List.of(1.0f), "embedding", 2);
            default -> traced.search("query", 2);
        };
    }

    @ParameterizedTest
    @ValueSource(strings = {"text", "vector", "filtered", "hybrid"})
    void searchIsLazyAndDoesNotRetrievePagesTwice(String mode) {
        SearchPagedIterable results = search(mode);
        assertThat(requests).hasValue(0);
        assertThat(exporter.getFinishedSpanItems()).isEmpty();

        Iterator<SearchResult> iterator = results.iterator();
        assertThat(iterator.next().getScore()).isEqualTo(2.0);
        assertThat(requests).hasValue(1);
        var body = com.google.gson.JsonParser.parseString(requestBodies.get(0)).getAsJsonObject();
        assertThat(body.get("search").getAsString()).isEqualTo("query");
        if (!mode.equals("text")) {
            var vector = body.getAsJsonArray("vectorQueries").get(0).getAsJsonObject();
            assertThat(vector.get("fields").getAsString()).isEqualTo("embedding");
            assertThat(vector.get("k").getAsInt()).isEqualTo(2);
            assertThat(vector.getAsJsonArray("vector").get(0).getAsFloat()).isEqualTo(1.0f);
        } else {
            assertThat(body.get("top").getAsInt()).isEqualTo(2);
        }
        if (mode.equals("filtered")) {
            assertThat(body.get("filter").getAsString()).isEqualTo("private-filter eq 'secret'");
        }
        assertThat(iterator.next().getScore()).isEqualTo(1.0);
        assertThat(iterator.hasNext()).isFalse();
        assertThat(requests).hasValue(2);
        assertThat(retrievals()).hasSize(2).allSatisfy(span -> {
            assertThat(span.getStatus().getStatusCode()).isEqualTo(StatusCode.OK);
            assertThat(span.getAttributes().get(AttributeKey.longKey(SemanticConventions.DB_VECTOR_RESULTS_COUNT)))
                .isEqualTo(1L);
        });
    }

    @ParameterizedTest
    @CsvSource({"false,false", "false,true", "true,false", "true,true"})
    void deferredPagesKeepCallerIdentityAndRestoreConsumer(boolean populated, boolean threaded) throws Exception {
        Span parent = telemetry.getTracer("app").spanBuilder("caller").startSpan();
        SearchPagedIterable results;
        Map<String, String> caller;
        try (Scope ignored = parent.makeCurrent();
             var session = ContextAttributes.usingSession(populated ? "caller-session" : null);
             var user = ContextAttributes.usingUser(populated ? "caller-user" : null);
             var metadata = ContextAttributes.usingMetadata(populated ? Map.of("origin", "caller") : null);
             var tags = ContextAttributes.usingTags(populated ? List.of("caller-tag") : null)) {
            caller = ContextAttributes.getAttributesFromContext();
            results = search("text");
        } finally {
            parent.end();
        }
        Runnable consume = () -> {
            try (var session = ContextAttributes.usingSession("consumer-session");
                 var user = ContextAttributes.usingUser("consumer-user");
                 var metadata = ContextAttributes.usingMetadata(Map.of("origin", "consumer"));
                 var tags = ContextAttributes.usingTags(List.of("consumer-tag"))) {
                var before = ContextAttributes.getAttributesFromContext();
                var iterator = results.iterator();
                assertThat(iterator.next().getScore()).isEqualTo(2.0);
                assertThat(ContextAttributes.getAttributesFromContext()).isEqualTo(before);
                nextFailure = new IllegalStateException("synthetic failure");
                assertThatThrownBy(iterator::hasNext).isSameAs(nextFailure);
                assertThat(ContextAttributes.getAttributesFromContext()).isEqualTo(before);
                assertThat(Span.current().getSpanContext().isValid()).isFalse();
            } catch (Exception error) {
                throw new RuntimeException(error);
            }
        };
        if (threaded) CompletableFuture.runAsync(consume).join(); else consume.run();
        assertThat(retrievals()).hasSize(2).allSatisfy(span -> {
            assertThat(span.getParentSpanId()).isEqualTo(parent.getSpanContext().getSpanId());
            for (String key : List.of(SemanticConventions.SESSION_ID, SemanticConventions.GEN_AI_CONVERSATION_ID,
                    SemanticConventions.USER_ID, SemanticConventions.METADATA, SemanticConventions.TAG_TAGS)) {
                assertThat(span.getAttributes().get(AttributeKey.stringKey(key))).isEqualTo(caller.get(key));
            }
            assertThat(span.getAttributes().asMap().toString()).doesNotContain("consumer");
        });
    }

    @ParameterizedTest
    @CsvSource({
        "false,false,false", "false,false,true", "false,true,false", "false,true,true",
        "true,false,false", "true,false,true", "true,true,false", "true,true,true"
    })
    void deferredPagesKeepEntireCallerContext(boolean populated, boolean threaded, boolean fail) {
        ContextKey<String> tenantKey = ContextKey.named("test-tenant");
        ContextKey<String> consumerOnlyKey = ContextKey.named("test-consumer-only");
        Span caller = telemetry.getTracer("app").spanBuilder("caller").startSpan();
        Context callerContext = Context.root().with(caller);
        if (populated) {
            callerContext = Baggage.builder().put("tenant", "caller-tenant").build()
                .storeInContext(callerContext.with(tenantKey, "caller-value"));
        }
        SearchPagedIterable results;
        try (Scope ignored = callerContext.makeCurrent()) {
            results = search("text");
        } finally {
            caller.end();
        }
        assertThat(requests).hasValue(0);

        Runnable consume = () -> {
            Context before = Context.current();
            Span consumer = telemetry.getTracer("app").spanBuilder("consumer").startSpan();
            Context consumerContext = Baggage.builder().put("tenant", "consumer-tenant")
                .put("consumer-only", "private-consumer-baggage").build()
                .storeInContext(Context.root().with(consumer).with(tenantKey, "consumer-value")
                    .with(consumerOnlyKey, "private-consumer-value"));
            try (Scope ignored = consumerContext.makeCurrent()) {
                var iterator = results.iterator();
                assertThat(iterator.next().getScore()).isEqualTo(2.0);
                assertThat(Context.current()).isSameAs(consumerContext);
                if (fail) {
                    nextFailure = new IllegalStateException("synthetic failure");
                    assertThatThrownBy(iterator::hasNext).isSameAs(nextFailure);
                } else {
                    assertThat(iterator.next().getScore()).isEqualTo(1.0);
                    assertThat(iterator.hasNext()).isFalse();
                }
                assertThat(Context.current()).isSameAs(consumerContext);
            } finally {
                consumer.end();
            }
            assertThat(Context.current()).isSameAs(before);
        };
        if (threaded) CompletableFuture.runAsync(consume).join(); else consume.run();

        assertThat(requests).hasValue(2);
        List<SpanData> pages = retrievals();
        assertThat(pages).hasSize(2).allSatisfy(span -> {
            assertThat(span.getParentSpanId()).isEqualTo(caller.getSpanContext().getSpanId());
            assertThat(span.getTraceId()).isEqualTo(caller.getSpanContext().getTraceId());
        });
        assertThat(pages.get(1).getStatus().getStatusCode()).isEqualTo(fail ? StatusCode.ERROR : StatusCode.OK);
        Baggage expectedBaggage = Baggage.fromContext(callerContext);
        assertThat(requestContexts).hasSize(2);
        for (int i = 0; i < requestContexts.size(); i++) {
            Context observed = requestContexts.get(i);
            assertThat(Baggage.fromContext(observed).asMap()).isEqualTo(expectedBaggage.asMap());
            assertThat(observed.get(tenantKey)).isEqualTo(populated ? "caller-value" : null);
            assertThat(observed.get(consumerOnlyKey)).isNull();
            assertThat(Span.fromContext(observed).getSpanContext().getSpanId()).isEqualTo(pages.get(i).getSpanId());
        }
    }

    @Test
    void repeatedIterationPreservesSdk12ReplayBehavior() {
        SearchPagedIterable results = search("text");
        assertThat(results.stream().count()).isEqualTo(2);
        assertThat(results.stream().count()).isEqualTo(2);
        // SDK 12 intentionally re-fetches every page on repeated iteration.
        assertThat(requests).hasValue(4);
        assertThat(retrievals()).hasSize(4);
    }

    @Test
    void metadataPagesAndContinuationTokensRemainAvailable() {
        SearchPagedIterable results = search("text");
        SearchPagedResponse first = results.iterableByPage(1).iterator().next();
        assertThat(first.getStatusCode()).isEqualTo(200);
        assertThat(first.getCount()).isEqualTo(2L);
        assertThat(first.getCoverage()).isEqualTo(100.0);
        assertThat(requests).hasValue(1);
        assertThat(first.getContinuationToken()).isNotNull();
        SearchPagedResponse next = results.iterableByPage(first.getContinuationToken(), 1).iterator().next();
        assertThat(next.getValue()).hasSize(1);
        assertThat(next.getValue().get(0).getScore()).isEqualTo(1.0);
        assertThat(requests).hasValue(2);
        assertThat(retrievals()).hasSize(2);
    }

    @Test
    void abandonedIterationHasNoUnfinishedSpanAndPreservesParent() {
        Span parent = telemetry.getTracer("app").spanBuilder("parent").startSpan();
        SearchPagedIterable results;
        try (Scope ignored = parent.makeCurrent()) {
            results = search("filtered");
        }
        parent.end();
        assertThat(results.stream().limit(1).count()).isEqualTo(1);
        assertThat(requests).hasValue(1);
        SpanData retrieval = retrievals().get(0);
        assertThat(retrieval.getParentSpanId()).isEqualTo(parent.getSpanContext().getSpanId());
        SpanData transport = exporter.getFinishedSpanItems().stream()
            .filter(span -> span.getName().equals("transport")).findFirst().orElseThrow();
        assertThat(transport.getParentSpanId()).isEqualTo(retrieval.getSpanId());
        assertThat(retrieval.getAttributes().asMap().toString()).doesNotContain("private-filter", "secret", "input.value");
        assertThat(Span.current().getSpanContext().isValid()).isFalse();
    }

    @Test
    void concurrentIterationsEachRetrieveUnderCallerParent() {
        Span parent = telemetry.getTracer("app").spanBuilder("parent").startSpan();
        SearchPagedIterable results;
        try (Scope ignored = parent.makeCurrent()) {
            results = search("text");
        } finally {
            parent.end();
        }
        var first = CompletableFuture.supplyAsync(() -> results.iterator().next().getScore());
        var second = CompletableFuture.supplyAsync(() -> results.iterator().next().getScore());
        assertThat(first.join()).isEqualTo(2.0);
        assertThat(second.join()).isEqualTo(2.0);
        assertThat(requests).hasValue(2);
        assertThat(retrievals()).hasSize(2).allSatisfy(span ->
            assertThat(span.getParentSpanId()).isEqualTo(parent.getSpanContext().getSpanId()));
    }

    @Test
    void emptyPageIsTracedWithoutInventingATopScore() {
        firstPage = "{\"value\":[]}";
        assertThat(search("text").stream().count()).isZero();
        assertThat(requests).hasValue(1);
        assertThat(retrievals()).singleElement().satisfies(span -> {
            assertThat(span.getAttributes().get(AttributeKey.longKey(SemanticConventions.DB_VECTOR_RESULTS_COUNT))).isZero();
            assertThat(span.getAttributes().get(AttributeKey.doubleKey("azure_search.top_score"))).isNull();
        });
    }

    @Test
    void laterPageErrorIsTracedWhenConsumed() {
        Iterator<SearchResult> iterator = search("text").iterator();
        iterator.next();
        nextFailure = new IllegalStateException("private-service-error");
        assertThatThrownBy(iterator::hasNext).isSameAs(nextFailure);
        assertThat(retrievals()).hasSize(2);
        SpanData failed = retrievals().get(1);
        assertThat(failed.getStatus().getStatusCode()).isEqualTo(StatusCode.ERROR);
        assertThat(failed.getStatus().getDescription()).isEmpty();
        assertThat(failed.getAttributes().asMap().toString()).doesNotContain("private-service-error");
        assertThat(failed.getEvents()).singleElement().satisfies(event ->
            assertThat(event.getAttributes().asMap().toString()).doesNotContain("private-service-error"));
        assertThat(Span.current().getSpanContext().isValid()).isFalse();
    }

    @ParameterizedTest
    @ValueSource(booleans = {false, true})
    void exportsRetrievalAndPrivateErrorsOverOtlp(boolean fail) throws Exception {
        LinkedBlockingQueue<byte[]> payloads = new LinkedBlockingQueue<>();
        LinkedBlockingQueue<String> contentTypes = new LinkedBlockingQueue<>();
        HttpServer collector = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        collector.createContext("/v1/traces", exchange -> {
            payloads.add(exchange.getRequestBody().readAllBytes());
            contentTypes.add(exchange.getRequestHeaders().getFirst("Content-Type"));
            exchange.getResponseHeaders().set("Content-Type", "application/x-protobuf");
            exchange.sendResponseHeaders(200, -1);
            exchange.close();
        });
        collector.start();
        try (SdkTracerProvider wireProvider = SdkTracerProvider.builder()
            .addSpanProcessor(SimpleSpanProcessor.create(OtlpHttpSpanExporter.builder()
                .setEndpoint("http://127.0.0.1:" + collector.getAddress().getPort() + "/v1/traces")
                .build())).build()) {
            FITracer wireTracer = new FITracer(wireProvider.get("wire-test"), tracer.getConfig());
            TracedSearchClient traced = new TracedSearchClient(client, "index", wireTracer);
            Span parent = telemetry.getTracer("app").spanBuilder("parent").startSpan();
            SearchPagedIterable results;
            try (Scope ignored = parent.makeCurrent()) {
                results = traced.searchWithVectorAndFilter("private-query", List.of(1.0f), "embedding", 2,
                    "private-filter eq 'secret'");
            } finally {
                parent.end();
            }
            if (fail) {
                nextFailure = new IllegalStateException("private-service-error");
                assertThatThrownBy(() -> results.iterator().hasNext()).isSameAs(nextFailure);
            } else {
                assertThat(results.iterator().next().getScore()).isEqualTo(2.0);
            }
            wireProvider.forceFlush().join(5, TimeUnit.SECONDS);
            byte[] payload = payloads.poll(5, TimeUnit.SECONDS);
            assertThat(payload).isNotNull();
            assertThat(contentTypes.poll()).isEqualTo("application/x-protobuf");
            var request = ExportTraceServiceRequest.parseFrom(payload);
            var spans = request.getResourceSpansList().stream()
                .flatMap(resource -> resource.getScopeSpansList().stream())
                .flatMap(scope -> scope.getSpansList().stream()).toList();
            assertThat(spans).hasSize(1);
            var span = spans.get(0);
            assertThat(span.getName()).isEqualTo("Azure Search Filtered Vector Query");
            assertThat(HexFormat.of().formatHex(span.getParentSpanId().toByteArray()))
                .isEqualTo(parent.getSpanContext().getSpanId());
            assertThat(span.getEndTimeUnixNano()).isGreaterThanOrEqualTo(span.getStartTimeUnixNano());
            assertThat(span.getStatus().getCodeValue()).isEqualTo(fail ? 2 : 1);
            assertThat(span.getAttributesList()).anySatisfy(attribute -> {
                assertThat(attribute.getKey()).isEqualTo("fi.span.kind");
                assertThat(attribute.getValue().getStringValue()).isEqualTo("RETRIEVER");
            }).anySatisfy(attribute -> {
                assertThat(attribute.getKey()).isEqualTo("azure_search.result_scope");
                assertThat(attribute.getValue().getStringValue()).isEqualTo("page");
            });
            if (!fail) {
                assertThat(span.getAttributesList()).anySatisfy(attribute -> {
                    assertThat(attribute.getKey()).isEqualTo(SemanticConventions.DB_VECTOR_RESULTS_COUNT);
                    assertThat(attribute.getValue().getIntValue()).isEqualTo(1);
                });
            }
            assertThat(span.toString()).doesNotContain("private-query", "private-filter", "private-service-error", "secret");
            assertThat(payloads).isEmpty();
        } finally {
            collector.stop(0);
        }
    }

    @ParameterizedTest
    @CsvSource({
        "vector, supplied-query, false, false", "vector, supplied-query, false, true",
        "vector, supplied-query, true, false", "vector, supplied-query, true, true",
        "vector, , false, false", "vector, , false, true",
        "vector, , true, false", "vector, , true, true",
        "filtered, supplied-query, false, false", "filtered, supplied-query, false, true",
        "filtered, supplied-query, true, false", "filtered, supplied-query, true, true",
        "filtered, , false, false", "filtered, , false, true",
        "filtered, , true, false", "filtered, , true, true"
    })
    void vectorPageExportsRespectQueryInputCapture(String mode, String searchText,
                                                   boolean hideInputs, boolean failLaterPage) throws Exception {
        LinkedBlockingQueue<byte[]> payloads = new LinkedBlockingQueue<>();
        HttpServer collector = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        collector.createContext("/v1/traces", exchange -> {
            payloads.add(exchange.getRequestBody().readAllBytes());
            exchange.getResponseHeaders().set("Content-Type", "application/x-protobuf");
            exchange.sendResponseHeaders(200, -1);
            exchange.close();
        });
        collector.start();
        try (SdkTracerProvider wireProvider = SdkTracerProvider.builder()
            .addSpanProcessor(SimpleSpanProcessor.create(OtlpHttpSpanExporter.builder()
                .setEndpoint("http://127.0.0.1:" + collector.getAddress().getPort() + "/v1/traces")
                .build())).build()) {
            FITracer wireTracer = new FITracer(wireProvider.get("vector-input-test"),
                TraceConfig.builder().hideInputs(hideInputs).hideOutputs(true).hideErrorDetails(true).build());
            TracedSearchClient traced = new TracedSearchClient(client, "index", wireTracer);
            firstPage = FIRST_PAGE.replace("\"query\"", searchText == null ? "null" : "\"" + searchText + "\"");
            SearchPagedIterable results = mode.equals("filtered")
                ? traced.searchWithVectorAndFilter(searchText, List.of(1.0f), "embedding", 2,
                    "private-filter eq 'secret'")
                : traced.searchWithVector(searchText, List.of(1.0f), "embedding", 2);
            assertThat(requests).hasValue(0);
            assertThat(payloads).isEmpty();

            Iterator<SearchResult> iterator = results.iterator();
            assertThat(iterator.next().getScore()).isEqualTo(2.0);
            // Capture settings must not change the query submitted to the real Azure SDK.
            var body = com.google.gson.JsonParser.parseString(requestBodies.get(0)).getAsJsonObject();
            String submittedText = body.has("search") && !body.get("search").isJsonNull()
                ? body.get("search").getAsString() : null;
            assertThat(submittedText).isEqualTo(searchText);
            if (failLaterPage) {
                nextFailure = new IllegalStateException("private-service-error");
                assertThatThrownBy(iterator::hasNext).isSameAs(nextFailure);
            } else {
                assertThat(iterator.next().getScore()).isEqualTo(1.0);
                assertThat(iterator.hasNext()).isFalse();
            }
            assertThat(requests).hasValue(2);
            assertThat(Span.current().getSpanContext().isValid()).isFalse();
            wireProvider.forceFlush().join(5, TimeUnit.SECONDS);

            // OTLP requests may complete out of order. Check both pages, not arrival order.
            List<ExportTraceServiceRequest> exports = new java.util.ArrayList<>();
            for (int page = 0; page < 2; page++) {
                byte[] payload = payloads.poll(5, TimeUnit.SECONDS);
                assertThat(payload).isNotNull();
                exports.add(ExportTraceServiceRequest.parseFrom(payload));
            }
            var spans = exports.stream().flatMap(request -> request.getResourceSpansList().stream())
                .flatMap(resource -> resource.getScopeSpansList().stream())
                .flatMap(scope -> scope.getSpansList().stream()).toList();
            assertThat(spans).hasSize(2).extracting(span -> span.getStatus().getCodeValue())
                .containsExactlyInAnyOrder(1, failLaterPage ? 2 : 1);
            // Both the initial page and a successful or failed continuation carry the original input.
            for (var span : spans) {
                assertThat(span.getName()).isEqualTo(mode.equals("filtered")
                    ? "Azure Search Filtered Vector Query" : "Azure Search Vector Query");
                String capturedText = span.getAttributesList().stream()
                    .filter(attribute -> attribute.getKey().equals(SemanticConventions.INPUT_VALUE))
                    .map(attribute -> attribute.getValue().getStringValue()).findFirst().orElse(null);
                assertThat(capturedText).as("vector page input.value").isEqualTo(hideInputs ? null : searchText);
                assertThat(span.toString()).doesNotContain("private-service-error");
                if (hideInputs) {
                    assertThat(span.toString()).doesNotContain("supplied-query", "private-filter", "secret");
                }
            }
            assertThat(payloads).isEmpty();
        } finally {
            collector.stop(0);
        }
    }

    private List<SpanData> retrievals() {
        return exporter.getFinishedSpanItems().stream()
            .filter(span -> span.getName().startsWith("Azure Search")).toList();
    }

    private static final class JsonResponse extends HttpResponse {
        private final String body;

        private JsonResponse(HttpRequest request, String body) {
            super(request);
            this.body = body;
        }

        @Override public int getStatusCode() { return 200; }
        @Override public String getHeaderValue(String name) { return getHeaders().getValue(name); }
        @Override public HttpHeaders getHeaders() {
            return new HttpHeaders().set("Content-Type", "application/json");
        }
        @Override public Flux<ByteBuffer> getBody() {
            return Flux.just(ByteBuffer.wrap(body.getBytes(StandardCharsets.UTF_8)));
        }
        @Override public Mono<byte[]> getBodyAsByteArray() { return Mono.just(body.getBytes(StandardCharsets.UTF_8)); }
        @Override public Mono<String> getBodyAsString() { return Mono.just(body); }
        @Override public Mono<String> getBodyAsString(Charset charset) { return Mono.just(body); }
    }
}
