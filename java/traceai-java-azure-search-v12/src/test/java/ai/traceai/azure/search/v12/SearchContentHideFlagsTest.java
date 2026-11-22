package ai.traceai.azure.search.v12;

import ai.traceai.FITracer;
import ai.traceai.TraceConfig;
import com.azure.core.credential.AzureKeyCredential;
import com.azure.core.http.HttpHeaders;
import com.azure.core.http.HttpRequest;
import com.azure.core.http.HttpResponse;
import com.azure.core.http.policy.FixedDelayOptions;
import com.azure.core.http.policy.RetryOptions;
import com.azure.search.documents.SearchClient;
import com.azure.search.documents.SearchClientBuilder;
import com.azure.search.documents.models.SearchPagedIterable;
import io.opentelemetry.sdk.testing.exporter.InMemorySpanExporter;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.data.SpanData;
import io.opentelemetry.sdk.trace.export.SimpleSpanProcessor;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.CsvSource;
import reactor.core.publisher.Flux;
import reactor.core.publisher.Mono;

import java.nio.ByteBuffer;
import java.nio.charset.Charset;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.ArrayList;
import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * The search query text is content (Python gen_ai.retrieval.query / input.value): it must not be exported under
 * hideInputs or hideInputMessages. Offline: the HTTP transport is a stub.
 */
class SearchContentHideFlagsTest {
    private static final String QUERY = "SENTINEL-SEARCH-QUERY-DO-NOT-EXPORT";
    private static final String PAGE = "{\"value\":[{\"@search.score\":2.0,\"id\":\"one\"}]}";

    private static List<SpanData> run(TraceConfig config, String mode) {
        InMemorySpanExporter exporter = InMemorySpanExporter.create();
        SdkTracerProvider provider = SdkTracerProvider.builder()
            .addSpanProcessor(SimpleSpanProcessor.create(exporter)).build();
        try {
            SearchClient client = new SearchClientBuilder()
                .endpoint("https://offline.search.windows.net")
                .indexName("index")
                .credential(new AzureKeyCredential("offline-placeholder"))
                .retryOptions(new RetryOptions(new FixedDelayOptions(0, Duration.ofMillis(1))))
                .httpClient(request -> Mono.just(new JsonResponse(request, PAGE)))
                .buildClient();
            TracedSearchClient traced = new TracedSearchClient(client, "index",
                new FITracer(provider.get("t"), config));
            SearchPagedIterable results = switch (mode) {
                case "vector" -> traced.searchWithVector(QUERY, List.of(1.0f), "embedding", 2);
                case "hybrid" -> traced.hybridSearch(QUERY, List.of(1.0f), "embedding", 2);
                default -> traced.search(QUERY, 2);
            };
            results.iterator().forEachRemaining(r -> { });
            return exporter.getFinishedSpanItems();
        } finally {
            provider.close();
        }
    }

    private static List<String> leakedKeys(List<SpanData> spans) {
        List<String> leaked = new ArrayList<>();
        for (SpanData span : spans) {
            span.getAttributes().forEach((k, v) -> {
                if (String.valueOf(v).contains(QUERY)) leaked.add(k.getKey());
            });
            span.getEvents().forEach(e -> e.getAttributes().forEach((k, v) -> {
                if (String.valueOf(v).contains(QUERY)) leaked.add("event:" + k.getKey());
            }));
        }
        return leaked;
    }

    @Test
    void controlExportsQueryWhenNothingIsHidden() {
        List<SpanData> spans = run(TraceConfig.builder().build(), "text");
        assertThat(spans).isNotEmpty();
        assertThat(leakedKeys(spans)).contains("input.value");
    }

    @ParameterizedTest(name = "{0} on {1} search")
    @CsvSource({"hideInputs,text", "hideInputs,vector", "hideInputs,hybrid",
        "hideInputMessages,text", "hideInputMessages,vector", "hideInputMessages,hybrid"})
    void hiddenQueryIsNotExported(String flag, String mode) {
        TraceConfig config = flag.equals("hideInputs")
            ? TraceConfig.builder().hideInputs(true).build()
            : TraceConfig.builder().hideInputMessages(true).build();
        List<SpanData> spans = run(config, mode);
        assertThat(spans).isNotEmpty();
        assertThat(leakedKeys(spans)).as("keys exported with the query text").isEmpty();
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
