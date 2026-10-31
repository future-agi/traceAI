package ai.traceai.azure.search;

import ai.traceai.FITracer;
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
import com.azure.search.documents.util.SearchPagedIterable;
import com.azure.search.documents.util.SearchPagedResponse;
import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.api.trace.StatusCode;
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

    private final AtomicInteger requests = new AtomicInteger();
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
            int attempt = requests.incrementAndGet();
            if (nextFailure != null) {
                return Mono.error(nextFailure);
            }
            // A transport child must run under the retrieval span, not the later consumer's context.
            Span transport = telemetry.getTracer("transport").spanBuilder("transport").startSpan();
            transport.end();
            return Mono.just(new JsonResponse(request, attempt == 1 ? firstPage : LAST_PAGE));
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
        assertThat(iterator.next().getScore()).isEqualTo(1.0);
        assertThat(iterator.hasNext()).isFalse();
        assertThat(requests).hasValue(2);
        assertThat(retrievals()).hasSize(2).allSatisfy(span -> {
            assertThat(span.getStatus().getStatusCode()).isEqualTo(StatusCode.OK);
            assertThat(span.getAttributes().get(AttributeKey.longKey(SemanticConventions.DB_VECTOR_RESULTS_COUNT)))
                .isEqualTo(1L);
        });
    }

    @Test
    void repeatedIterationKeepsTheSdkFirstPageCache() {
        SearchPagedIterable results = search("text");
        assertThat(results.stream().count()).isEqualTo(2);
        assertThat(results.stream().count()).isEqualTo(2);
        // The SDK caches the first page, but intentionally re-fetches continuation pages.
        assertThat(requests).hasValue(3);
        assertThat(retrievals()).hasSize(3);
    }

    @Test
    void metadataPagesAndContinuationTokensRemainAvailable() {
        SearchPagedIterable results = search("text");
        assertThat(results.getTotalCount()).isEqualTo(2L);
        assertThat(results.getCoverage()).isEqualTo(100.0);
        assertThat(requests).hasValue(1);
        SearchPagedResponse first = results.iterableByPage(1).iterator().next();
        assertThat(first.getStatusCode()).isEqualTo(200);
        assertThat(first.getContinuationToken()).isNotEmpty();
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
    void concurrentFirstPageReadsShareOneRetrievalSpan() {
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
        assertThat(requests).hasValue(1);
        assertThat(retrievals()).singleElement().satisfies(span ->
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
