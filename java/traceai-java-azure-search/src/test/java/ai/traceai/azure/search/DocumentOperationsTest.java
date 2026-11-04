package ai.traceai.azure.search;

import ai.traceai.FITracer;
import ai.traceai.SemanticConventions;
import ai.traceai.TraceConfig;
import com.azure.core.credential.AzureKeyCredential;
import com.azure.core.exception.HttpResponseException;
import com.azure.core.http.HttpHeaders;
import com.azure.core.http.HttpRequest;
import com.azure.core.http.HttpResponse;
import com.azure.core.http.policy.FixedDelayOptions;
import com.azure.core.http.policy.RetryOptions;
import com.azure.core.util.ClientOptions;
import com.azure.core.util.TracingOptions;
import com.azure.search.documents.SearchClient;
import com.azure.search.documents.SearchClientBuilder;
import com.azure.search.documents.models.IndexDocumentsResult;
import com.google.gson.JsonParser;
import com.sun.net.httpserver.HttpServer;
import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.exporter.otlp.http.trace.OtlpHttpSpanExporter;
import io.opentelemetry.proto.collector.trace.v1.ExportTraceServiceRequest;
import io.opentelemetry.sdk.testing.exporter.InMemorySpanExporter;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.export.SimpleSpanProcessor;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.CsvSource;
import org.junit.jupiter.params.provider.ValueSource;
import reactor.core.publisher.Flux;
import reactor.core.publisher.Mono;

import java.net.HttpURLConnection;
import java.net.InetSocketAddress;
import java.net.URI;
import java.nio.ByteBuffer;
import java.nio.charset.Charset;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.Iterator;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.LinkedBlockingQueue;
import java.util.concurrent.TimeUnit;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

class DocumentOperationsTest {
    private static final String KEY = "private-document-key-canary";
    private static final String ERROR = "private-error-canary";
    private static final String CREDENTIAL = "synthetic-not-a-real-credential";
    private static final List<Map<String, Object>> DOCUMENTS = List.of(
        Map.of("id", "one", "content", "first synthetic document"),
        Map.of("id", "two", "content", "second synthetic document"));

    private final List<String> bodies = new CopyOnWriteArrayList<>();
    private final List<String> paths = new CopyOnWriteArrayList<>();
    private final LinkedBlockingQueue<byte[]> exported = new LinkedBlockingQueue<>();
    private final InMemorySpanExporter exporter = InMemorySpanExporter.create();
    private HttpServer server;
    private SdkTracerProvider provider;
    private SearchClient client;
    private volatile boolean fail;

    @BeforeEach
    void start() throws Exception {
        server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        server.createContext("/v1/traces", exchange -> {
            exported.add(exchange.getRequestBody().readAllBytes());
            exchange.getResponseHeaders().set("Content-Type", "application/x-protobuf");
            exchange.sendResponseHeaders(200, -1);
            exchange.close();
        });
        server.createContext("/", exchange -> {
            bodies.add(new String(exchange.getRequestBody().readAllBytes(), StandardCharsets.UTF_8));
            paths.add(exchange.getRequestURI().getPath());
            String response = fail
                ? "{\"error\":{\"code\":\"BadRequest\",\"message\":\"" + ERROR + "\"}}"
                : exchange.getRequestMethod().equals("GET")
                    ? "{\"id\":\"" + KEY + "\"}"
                    : "{\"value\":[{\"key\":\"one\",\"status\":true,\"statusCode\":200},"
                        + "{\"key\":\"two\",\"status\":true,\"statusCode\":200}]}";
            byte[] bytes = response.getBytes(StandardCharsets.UTF_8);
            exchange.getResponseHeaders().set("Content-Type", "application/json");
            exchange.sendResponseHeaders(fail ? 400 : 200, bytes.length);
            exchange.getResponseBody().write(bytes);
            exchange.close();
        });
        server.start();
        provider = SdkTracerProvider.builder()
            .addSpanProcessor(SimpleSpanProcessor.create(exporter))
            .addSpanProcessor(SimpleSpanProcessor.create(OtlpHttpSpanExporter.builder()
                .setEndpoint(endpoint() + "/v1/traces").build()))
            .build();
        client = new SearchClientBuilder()
            .endpoint("https://offline.search.windows.net")
            .indexName("index")
            .credential(new AzureKeyCredential(CREDENTIAL))
            .retryOptions(new RetryOptions(new FixedDelayOptions(0, Duration.ofMillis(1))))
            // FI capture controls do not configure Azure's independent native instrumentation.
            .clientOptions(new ClientOptions().setTracingOptions(new TracingOptions().setEnabled(false)))
            .httpClient(this::sendToLoopback)
            .buildClient();
    }

    @AfterEach
    void close() {
        if (provider != null) provider.close();
        if (server != null) server.stop(0);
    }

    @ParameterizedTest
    @CsvSource({"false,false", "false,true", "true,false", "true,true"})
    void documentLookupRespectsInputCaptureOnSuccessAndFailure(boolean hideInputs, boolean failure) throws Exception {
        fail = failure;
        TracedSearchClient traced = traced(hideInputs);
        if (failure) {
            assertThatThrownBy(() -> traced.getDocument(KEY, Map.class))
                .isInstanceOf(RuntimeException.class).hasCauseInstanceOf(HttpResponseException.class);
        } else {
            assertThat(traced.getDocument(KEY, Map.class)).containsEntry("id", KEY);
        }
        // Hiding telemetry must not alter the document key sent to Azure.
        assertThat(paths).singleElement().asString().contains(KEY);
        assertThat(exporter.getFinishedSpanItems()).singleElement().satisfies(span -> {
            assertThat(span.getName()).isEqualTo("Azure Search Get Document");
            assertThat(span.getStatus().getStatusCode()).isEqualTo(failure ? StatusCode.ERROR : StatusCode.OK);
            assertThat(span.getAttributes().get(AttributeKey.stringKey("azure_search.document_key")))
                .isEqualTo(hideInputs ? null : KEY);
            assertThat(span.getAttributes().get(AttributeKey.booleanKey("azure_search.document_found")))
                .isEqualTo(failure ? null : true);
        });
        provider.forceFlush().join(5, TimeUnit.SECONDS);
        byte[] payload = exported.poll(5, TimeUnit.SECONDS);
        assertThat(payload).isNotNull();
        var spans = ExportTraceServiceRequest.parseFrom(payload).getResourceSpansList().stream()
            .flatMap(resource -> resource.getScopeSpansList().stream())
            .flatMap(scope -> scope.getSpansList().stream()).toList();
        assertThat(spans).singleElement().satisfies(span -> {
            assertThat(span.getName()).isEqualTo("Azure Search Get Document");
            assertThat(span.getStatus().getCodeValue()).isEqualTo(failure ? 2 : 1);
            assertThat(span.toString()).doesNotContain(ERROR, CREDENTIAL);
            if (hideInputs) {
                // Check attributes, events, and status as actually exported, not just a mocked setter.
                assertThat(span.toString()).doesNotContain(KEY, "azure_search.document_key");
            } else {
                assertThat(span.getAttributesList()).anySatisfy(attribute -> {
                    assertThat(attribute.getKey()).isEqualTo("azure_search.document_key");
                    assertThat(attribute.getValue().getStringValue()).isEqualTo(KEY);
                });
            }
        });
        assertThat(exported).isEmpty();
        assertThat(Span.current().getSpanContext().isValid()).isFalse();
    }

    @ParameterizedTest
    @CsvSource({
        "upload,single-pass", "merge,single-pass", "delete,single-pass",
        "upload,collection", "merge,collection", "delete,collection",
        "upload,repeatable", "merge,repeatable", "delete,repeatable",
        "upload,empty", "merge,empty", "delete,empty"
    })
    void documentWritesPreserveNativePayloadAndCount(String operation, String source) {
        // Control: the real unwrapped SDK must accept the same kind of iterable.
        write(client, operation, documents(source));
        assertThat(bodies).hasSize(1);
        var expected = JsonParser.parseString(bodies.get(0));
        int count = source.equals("empty") ? 0 : DOCUMENTS.size();
        assertThat(expected.getAsJsonObject().getAsJsonArray("value")).hasSize(count);
        bodies.clear();
        paths.clear();

        IndexDocumentsResult result = write(traced(true), operation, documents(source));
        assertThat(result.getResults()).hasSize(2);
        assertThat(bodies).hasSize(1);
        assertThat(JsonParser.parseString(bodies.get(0))).isEqualTo(expected);
        assertThat(exporter.getFinishedSpanItems()).singleElement().satisfies(span -> {
            assertThat(span.getStatus().getStatusCode()).isEqualTo(StatusCode.OK);
            assertThat(span.getAttributes().get(AttributeKey.longKey(SemanticConventions.DB_VECTOR_UPSERT_COUNT)))
                .isEqualTo((long) count);
        });
        assertThat(Span.current().getSpanContext().isValid()).isFalse();
    }

    @ParameterizedTest
    @ValueSource(strings = {"upload", "merge", "delete"})
    void failingIterableDoesNotSubmitAPartialBatchAndEndsSpan(String operation) {
        IllegalStateException failure = new IllegalStateException("synthetic iterable failure");
        Iterable<Map<String, Object>> documents = () -> new Iterator<>() {
            private int position;
            @Override public boolean hasNext() { return true; }
            @Override public Map<String, Object> next() {
                if (position++ == 0) return DOCUMENTS.get(0);
                throw failure;
            }
        };
        assertThatThrownBy(() -> write(traced(true), operation, documents)).hasCause(failure);
        assertThat(bodies).isEmpty();
        assertThat(exporter.getFinishedSpanItems()).singleElement().satisfies(span -> {
            assertThat(span.hasEnded()).isTrue();
            assertThat(span.getStatus().getStatusCode()).isEqualTo(StatusCode.ERROR);
            assertThat(span.toString()).doesNotContain(failure.getMessage());
        });
        assertThat(Span.current().getSpanContext().isValid()).isFalse();
    }

    private TracedSearchClient traced(boolean hideInputs) {
        return new TracedSearchClient(client, "index", new FITracer(provider.get("document-test"),
            TraceConfig.builder().hideInputs(hideInputs).hideOutputs(true).hideErrorDetails(true).build()));
    }

    private Iterable<Map<String, Object>> documents(String source) {
        return switch (source) {
            case "empty" -> List.of();
            case "collection" -> DOCUMENTS;
            case "repeatable" -> DOCUMENTS::iterator;
            default -> {
                Iterator<Map<String, Object>> iterator = DOCUMENTS.iterator();
                yield () -> iterator;
            }
        };
    }

    private IndexDocumentsResult write(SearchClient client, String operation, Iterable<Map<String, Object>> documents) {
        return switch (operation) {
            case "upload" -> client.uploadDocuments(documents);
            case "merge" -> client.mergeOrUploadDocuments(documents);
            default -> client.deleteDocuments(documents);
        };
    }

    private IndexDocumentsResult write(TracedSearchClient client, String operation, Iterable<Map<String, Object>> documents) {
        return switch (operation) {
            case "upload" -> client.uploadDocuments(documents);
            case "merge" -> client.mergeOrUploadDocuments(documents);
            default -> client.deleteDocuments(documents);
        };
    }

    private String endpoint() {
        return "http://127.0.0.1:" + server.getAddress().getPort();
    }

    private Mono<HttpResponse> sendToLoopback(HttpRequest request) {
        HttpURLConnection connection = null;
        try {
            // Preserve the SDK's actual serialization and response parsing; redirect only transport.
            connection = (HttpURLConnection) URI.create(endpoint() + request.getUrl().getFile()).toURL().openConnection();
            connection.setConnectTimeout(5000);
            connection.setReadTimeout(5000);
            connection.setRequestMethod(request.getHttpMethod().toString());
            connection.setRequestProperty("Content-Type", "application/json");
            if (request.getBodyAsBinaryData() != null) {
                connection.setDoOutput(true);
                try (var output = connection.getOutputStream()) {
                    output.write(request.getBodyAsBinaryData().toBytes());
                }
            }
            int status = connection.getResponseCode();
            try (var input = status >= 400 ? connection.getErrorStream() : connection.getInputStream()) {
                return Mono.just(new JsonResponse(request, status, input.readAllBytes()));
            }
        } catch (Exception error) {
            return Mono.error(error);
        } finally {
            if (connection != null) connection.disconnect();
        }
    }

    private static final class JsonResponse extends HttpResponse {
        private final int status;
        private final byte[] body;
        private JsonResponse(HttpRequest request, int status, byte[] body) {
            super(request);
            this.status = status;
            this.body = body;
        }
        @Override public int getStatusCode() { return status; }
        @Override public String getHeaderValue(String name) { return getHeaders().getValue(name); }
        @Override public HttpHeaders getHeaders() { return new HttpHeaders().set("Content-Type", "application/json"); }
        @Override public Flux<ByteBuffer> getBody() { return Flux.just(ByteBuffer.wrap(body)); }
        @Override public Mono<byte[]> getBodyAsByteArray() { return Mono.just(body); }
        @Override public Mono<String> getBodyAsString() { return getBodyAsString(StandardCharsets.UTF_8); }
        @Override public Mono<String> getBodyAsString(Charset charset) { return Mono.just(new String(body, charset)); }
    }
}
