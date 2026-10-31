package ai.traceai.googlegenai;

import ai.traceai.FITracer;
import ai.traceai.SemanticConventions;
import ai.traceai.TraceConfig;
import com.google.genai.Client;
import com.google.genai.ResponseStream;
import com.google.genai.types.Content;
import com.google.genai.types.EmbedContentConfig;
import com.google.genai.types.GenerateContentConfig;
import com.google.genai.types.GenerateContentResponse;
import com.google.genai.types.HttpOptions;
import com.google.genai.types.Part;
import com.google.gson.JsonObject;
import com.google.gson.JsonParser;
import com.sun.net.httpserver.HttpServer;
import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.context.Scope;
import io.opentelemetry.exporter.otlp.http.trace.OtlpHttpSpanExporter;
import io.opentelemetry.proto.collector.trace.v1.ExportTraceServiceRequest;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.data.SpanData;
import io.opentelemetry.sdk.trace.export.SimpleSpanProcessor;
import io.opentelemetry.sdk.testing.exporter.InMemorySpanExporter;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Iterator;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.Executors;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicReference;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.Mockito.*;

/** Real SDK requests to loopback only. No vendor keys or TraceAI global initialization. */
class TracedGenerativeModelTest {
    private static final String RESPONSE = "{\"candidates\":[{\"content\":{\"role\":\"model\",\"parts\":[{\"text\":\"answer\"}]},"
        + "\"finishReason\":\"STOP\"}],\"usageMetadata\":{\"promptTokenCount\":3,"
        + "\"candidatesTokenCount\":2,\"thoughtsTokenCount\":5,\"totalTokenCount\":10}}";
    private static final String STREAM = "data: {\"candidates\":[{\"content\":{\"role\":\"model\",\"parts\":[{\"text\":\"first \"}]}}]}\n\n"
        + "data: " + RESPONSE + "\n\n";
    private HttpServer server;
    private ExecutorService executor;
    private SdkTracerProvider provider;
    private InMemorySpanExporter exporter;
    private TracedGenerativeModel model;
    private Client client;
    private FITracer tracer;
    private String body = RESPONSE;
    private int status = 200;
    private final AtomicReference<JsonObject> request = new AtomicReference<>();
    private final CountDownLatch received = new CountDownLatch(1);
    private final CountDownLatch release = new CountDownLatch(1);
    private boolean block;

    @BeforeEach
    void setUp() throws Exception {
        server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        executor = Executors.newCachedThreadPool();
        server.setExecutor(executor);
        server.createContext("/", exchange -> {
            request.set(JsonParser.parseString(new String(exchange.getRequestBody().readAllBytes(), StandardCharsets.UTF_8)).getAsJsonObject());
            received.countDown();
            if (block) {
                try {
                    release.await(10, TimeUnit.SECONDS);
                } catch (InterruptedException e) {
                    Thread.currentThread().interrupt();
                }
            }
            byte[] bytes = body.getBytes(StandardCharsets.UTF_8);
            exchange.getResponseHeaders().set("Content-Type", body.startsWith("data:") ? "text/event-stream" : "application/json");
            exchange.sendResponseHeaders(status, bytes.length);
            try (var output = exchange.getResponseBody()) {
                output.write(bytes);
            }
            exchange.close();
        });
        server.start();
        exporter = InMemorySpanExporter.create();
        provider = SdkTracerProvider.builder().addSpanProcessor(SimpleSpanProcessor.create(exporter)).build();
        client = Client.builder().apiKey("synthetic-key").vertexAI(false)
            .httpOptions(HttpOptions.builder().baseUrl(endpoint()).build()).build();
        configure(TraceConfig.builder().build());
    }

    private String endpoint() {
        return "http://127.0.0.1:" + server.getAddress().getPort();
    }

    private void configure(TraceConfig config) {
        tracer = new FITracer(provider.get("test"), config);
        model = new TracedGenerativeModel(client, tracer, "gemini-test");
    }

    @AfterEach
    void tearDown() {
        release.countDown();
        server.stop(0);
        executor.shutdownNow();
        provider.close();
    }

    private SpanData span() {
        assertThat(exporter.getFinishedSpanItems()).hasSize(1);
        return exporter.getFinishedSpanItems().get(0);
    }

    private long number(SpanData span, String name) {
        return span.getAttributes().get(AttributeKey.longKey(name));
    }

    @Test
    void includesThoughtTokensInGeneration() {
        assertThat(model.generateContent("question").text()).isEqualTo("answer");
        assertThat(number(span(), SemanticConventions.LLM_TOKEN_COUNT_COMPLETION)).isEqualTo(7);
        assertThat(number(span(), SemanticConventions.LLM_TOKEN_COUNT_TOTAL)).isEqualTo(10);
    }

    @Test
    void includesThoughtTokensInChat() {
        model.startChat().sendMessage("question");
        assertThat(number(span(), SemanticConventions.LLM_TOKEN_COUNT_COMPLETION)).isEqualTo(7);
    }

    @Test
    void exposesConfigurationWithoutUnwrapping() throws Exception {
        assertThat(TracedGenerativeModel.class.getMethod("generateContent", String.class, GenerateContentConfig.class)).isNotNull();
    }

    @Test
    void exposesStreamingWithoutUnwrapping() throws Exception {
        assertThat(TracedGenerativeModel.class.getMethod("generateContentStream", String.class, GenerateContentConfig.class)).isNotNull();
    }

    private GenerateContentConfig config() {
        return GenerateContentConfig.fromJson("{\"systemInstruction\":{\"parts\":[{\"text\":\"system-secret\"}]},"
            + "\"temperature\":0.2,\"responseMimeType\":\"application/json\","
            + "\"responseSchema\":{\"type\":\"OBJECT\",\"properties\":{\"answer\":{\"type\":\"STRING\"}}},"
            + "\"tools\":[{\"functionDeclarations\":[{\"name\":\"lookup\",\"description\":\"Look up a fact\"}]}]}");
    }

    @Test
    void forwardsConfigurationAndStructuredInputsUnchanged() {
        var contents = List.of(Content.builder().role("user").parts(List.of(Part.fromText("question"))).build());
        model.generateContent(contents, config());
        assertConfiguration();
        assertThat(span().getAttributes().get(AttributeKey.stringKey(SemanticConventions.LLM_INPUT_MESSAGES)))
            .contains("system-secret", "question");
    }

    private void assertConfiguration() {
        JsonObject json = request.get();
        assertThat(json.getAsJsonObject("generationConfig").get("responseMimeType").getAsString()).isEqualTo("application/json");
        assertThat(json.getAsJsonObject("generationConfig").getAsJsonObject("responseSchema").has("properties")).isTrue();
        assertThat(json.getAsJsonObject("systemInstruction").toString()).contains("system-secret");
        assertThat(json.getAsJsonArray("tools").toString()).contains("lookup");
    }

    @Test
    void supportsToolOnlyResponses() {
        body = "{\"candidates\":[{\"content\":{\"role\":\"model\",\"parts\":[{\"functionCall\":{\"name\":\"lookup\",\"args\":{\"id\":7}}}]}}]}";
        assertThat(model.generateContent("question").candidates()).isPresent();
        assertThat(span().getAttributes().get(AttributeKey.stringKey(SemanticConventions.RAW_OUTPUT))).contains("lookup");
    }

    @Test
    void fallsBackToReconciledTotalWhenMissing() {
        body = RESPONSE.replace(",\"totalTokenCount\":10", "");
        model.generateContent("question");
        assertThat(number(span(), SemanticConventions.LLM_TOKEN_COUNT_TOTAL)).isEqualTo(10);
    }

    @Test
    void streamsUntilExhaustionWithoutEagerConsumptionOrDuplicateSpans() {
        body = STREAM;
        try (var stream = model.generateContentStream("question", config())) {
            assertThat(exporter.getFinishedSpanItems()).isEmpty();
            List<String> values = new ArrayList<>();
            stream.forEach(chunk -> values.add(chunk.text()));
            assertThat(values).containsExactly("first ", "answer");
            assertThat(span().getAttributes().get(AttributeKey.stringKey(SemanticConventions.OUTPUT_VALUE))).isEqualTo("first answer");
            assertThat(number(span(), SemanticConventions.LLM_TOKEN_COUNT_COMPLETION)).isEqualTo(7);
        }
        assertConfiguration();
        assertThat(exporter.getFinishedSpanItems()).hasSize(1);
    }

    @Test
    void marksEarlyCloseAndNeverLeavesCurrentSpanSet() {
        body = STREAM;
        var parent = provider.get("test").spanBuilder("parent").startSpan();
        try (Scope ignored = parent.makeCurrent()) {
            try (var stream = model.generateContentStream("question", null)) {
                assertThat(Span.current()).isSameAs(parent);
                stream.iterator().next();
                assertThat(Span.current()).isSameAs(parent);
            }
            assertThat(span().getParentSpanId()).isEqualTo(parent.getSpanContext().getSpanId());
            assertThat(span().getAttributes().get(AttributeKey.booleanKey("google.genai.cancelled"))).isTrue();
            assertThat(span().getStatus().getStatusCode()).isEqualTo(StatusCode.UNSET);
        } finally {
            parent.end();
        }
    }

    @Test
    void marksMalformedStreamAsError() {
        body = "data: not-json\n\n";
        assertThatThrownBy(() -> {
            try (var stream = model.generateContentStream("question", null)) {
                stream.forEach(value -> { });
            }
        }).isInstanceOf(RuntimeException.class);
        assertThat(span().getStatus().getStatusCode()).isEqualTo(StatusCode.ERROR);
    }

    @Test
    void asyncGenerationKeepsParentAndConfig() throws Exception {
        var parent = provider.get("test").spanBuilder("parent").startSpan();
        CompletableFuture<GenerateContentResponse> response;
        try (Scope ignored = parent.makeCurrent()) {
            response = model.generateContentAsync("question", config());
            assertThat(Span.current()).isSameAs(parent);
        }
        assertThat(response.get(10, TimeUnit.SECONDS).text()).isEqualTo("answer");
        assertThat(span().getParentSpanId()).isEqualTo(parent.getSpanContext().getSpanId());
        assertThat(number(span(), SemanticConventions.LLM_TOKEN_COUNT_COMPLETION)).isEqualTo(7);
        assertConfiguration();
        parent.end();
    }

    @Test
    void asyncStreamingEndsOnlyAfterConsumption() throws Exception {
        body = STREAM;
        try (var stream = model.generateContentStreamAsync("question", config()).get(10, TimeUnit.SECONDS)) {
            assertThat(exporter.getFinishedSpanItems()).isEmpty();
            stream.forEach(value -> { });
        }
        assertThat(span().getStatus().getStatusCode()).isEqualTo(StatusCode.OK);
        assertConfiguration();
    }

    @Test
    void asyncCancellationFinishesOnce() throws Exception {
        block = true;
        var response = model.generateContentAsync("question", null);
        assertThat(received.await(5, TimeUnit.SECONDS)).isTrue();
        assertThat(response.cancel(true)).isTrue();
        assertThat(span().getAttributes().get(AttributeKey.booleanKey("google.genai.cancelled"))).isTrue();
        release.countDown();
    }

    @Test
    void asyncStreamCancellationFinishesOnce() throws Exception {
        block = true;
        body = STREAM;
        var response = model.generateContentStreamAsync("question", null);
        assertThat(received.await(5, TimeUnit.SECONDS)).isTrue();
        assertThat(response.cancel(true)).isTrue();
        assertThat(span().getAttributes().get(AttributeKey.booleanKey("google.genai.cancelled"))).isTrue();
        release.countDown();
    }

    @Test
    void supportsSyncAndAsyncEmbeddingsWithoutExportingVectors() throws Exception {
        body = "{\"embeddings\":[{\"values\":[0.123,0.456,0.789]}]}";
        var config = EmbedContentConfig.builder().outputDimensionality(3).build();
        assertThat(model.embedContent("secret input", config).embeddings()).isPresent();
        assertThat(number(span(), SemanticConventions.EMBEDDING_DIMENSIONS)).isEqualTo(3);
        assertThat(span().getAttributes().get(AttributeKey.stringKey(SemanticConventions.FI_SPAN_KIND))).isEqualTo("EMBEDDING");
        assertThat(span().getAttributes().toString()).doesNotContain("0.123", "0.456");
        exporter.reset();
        assertThat(model.embedContentAsync(List.of("secret input"), config).get(10, TimeUnit.SECONDS).embeddings()).isPresent();
        assertThat(number(span(), SemanticConventions.EMBEDDING_VECTOR_COUNT)).isEqualTo(1);
    }

    @Test
    void chatConfigurationAndStreamingRetainSdkHistory() {
        var chat = model.startChat(config());
        chat.sendMessage("first question");
        assertConfiguration();
        exporter.reset();
        body = STREAM;
        try (var stream = chat.sendMessageStream("second question", null)) {
            stream.forEach(value -> { });
        }
        assertThat(request.get().getAsJsonArray("contents").toString()).contains("first question", "answer", "second question");
        assertThat(number(span(), SemanticConventions.LLM_TOKEN_COUNT_COMPLETION)).isEqualTo(7);
    }

    @Test
    void preservesErrorTypeAndHidesDetails() {
        status = 400;
        body = "{\"error\":{\"code\":400,\"message\":\"sensitive-error\",\"status\":\"INVALID_ARGUMENT\"}}";
        configure(TraceConfig.builder().hideErrorDetails(true).build());
        assertThatThrownBy(() -> model.generateContent("question"))
            .isInstanceOf(com.google.genai.errors.ApiException.class);
        assertThat(span().getStatus().getStatusCode()).isEqualTo(StatusCode.ERROR);
        assertThat(span().getAttributes().toString()).doesNotContain("sensitive-error");
        assertThat(span().getEvents().toString()).doesNotContain("sensitive-error");
    }

    @Test
    void asyncErrorsPreserveCauseAndEndSpan() {
        status = 400;
        body = "{\"error\":{\"code\":400,\"message\":\"sensitive-error\",\"status\":\"INVALID_ARGUMENT\"}}";
        assertThatThrownBy(() -> model.generateContentAsync("question", null).get(10, TimeUnit.SECONDS))
            .hasCauseInstanceOf(com.google.genai.errors.ApiException.class);
        assertThat(span().getStatus().getStatusCode()).isEqualTo(StatusCode.ERROR);
    }

    @Test
    void hiddenStreamingContentNeverReachesAttributes() {
        configure(TraceConfig.builder().hideInputs(true).hideOutputs(true).build());
        body = STREAM;
        try (var stream = model.generateContentStream("private-question", config())) {
            stream.forEach(value -> { });
        }
        assertThat(span().getAttributes().toString()).doesNotContain("private-question", "system-secret", "answer", "first ");
        assertThat(number(span(), SemanticConventions.LLM_TOKEN_COUNT_COMPLETION)).isEqualTo(7);
    }

    @Test
    void doesNotExportRequestHeaders() {
        var config = GenerateContentConfig.builder().httpOptions(HttpOptions.builder()
            .headers(Map.of("X-Private", "private-header-secret")).build()).build();
        model.generateContent("question", config);
        assertThat(span().getAttributes().toString()).doesNotContain("private-header-secret", "synthetic-key");
    }

    @Test
    void excludesTransportHeadersFromRawResponse() {
        var response = GenerateContentResponse.fromJson(RESPONSE.substring(0, RESPONSE.length() - 1)
            + ",\"sdkHttpResponse\":{\"headers\":{\"X-Private\":\"response-header-secret\"}}}");
        var operation = new GenAiOperation(tracer, "test", "gemini-test", ai.traceai.FISpanKind.LLM, "question", null);
        operation.response(response, false);
        operation.finish(null, false, false);
        assertThat(span().getAttributes().get(AttributeKey.stringKey(SemanticConventions.RAW_OUTPUT)))
            .contains("answer").doesNotContain("response-header-secret", "sdkHttpResponse");
    }

    @Test
    void unwrappedClientRemainsOriginalAndExplicitlyUninstrumented() {
        assertThat(model.unwrap()).isSameAs(client);
        client.models.generateContent("gemini-test", "question", null);
        assertThat(exporter.getFinishedSpanItems()).isEmpty();
    }

    @Test
    void exportsOtlpUsageAndPrivacyToLoopbackCollector() throws Exception {
        var wire = new AtomicReference<byte[]>();
        var exported = new CountDownLatch(1);
        server.createContext("/v1/traces", exchange -> {
            wire.set(exchange.getRequestBody().readAllBytes());
            exchange.getResponseHeaders().set("Content-Type", "application/x-protobuf");
            exchange.sendResponseHeaders(200, -1);
            exchange.close();
            exported.countDown();
        });
        try (var otlp = SdkTracerProvider.builder().addSpanProcessor(SimpleSpanProcessor.create(
            OtlpHttpSpanExporter.builder().setEndpoint(endpoint() + "/v1/traces").build())).build()) {
            var hidden = new TracedGenerativeModel(client, new FITracer(otlp.get("wire"), TraceConfig.builder()
                .hideInputs(true).hideOutputs(true).hideErrorDetails(true).build()), "gemini-test");
            body = STREAM;
            try (var stream = hidden.generateContentStream("wire-private-input", config())) {
                stream.forEach(value -> { });
            }
            otlp.forceFlush().join(5, TimeUnit.SECONDS);
            assertThat(exported.await(5, TimeUnit.SECONDS)).isTrue();
            var request = ExportTraceServiceRequest.parseFrom(wire.get());
            var spans = request.getResourceSpans(0).getScopeSpans(0).getSpansList();
            assertThat(spans).hasSize(1);
            var attrs = spans.get(0).getAttributesList();
            assertThat(attrs).anySatisfy(attr -> {
                assertThat(attr.getKey()).isEqualTo(SemanticConventions.LLM_TOKEN_COUNT_COMPLETION);
                assertThat(attr.getValue().getIntValue()).isEqualTo(7);
            });
            assertThat(request.toString()).doesNotContain("wire-private-input", "system-secret", "answer", "synthetic-key");
        }
    }

    @Test
    void neverAddsCumulativeStreamingUsageSnapshots() {
        body = "data: " + RESPONSE + "\n\ndata: " + RESPONSE + "\n\n";
        try (var stream = model.generateContentStream("question", null)) {
            stream.forEach(value -> { });
        }
        assertThat(number(span(), SemanticConventions.LLM_TOKEN_COUNT_COMPLETION)).isEqualTo(7);
        assertThat(number(span(), SemanticConventions.LLM_TOKEN_COUNT_TOTAL)).isEqualTo(10);
    }

    @Test
    void boundsStreamCaptureWhileReturningEveryChunk() {
        body = "data: " + RESPONSE.replace("answer", "x".repeat(20000)) + "\n\n";
        body = body.repeat(10);
        int returnedCharacters = 0;
        try (var stream = model.generateContentStream("question", null)) {
            for (var chunk : stream) returnedCharacters += chunk.text().length();
        }
        assertThat(returnedCharacters).isEqualTo(200000);
        assertThat(span().getAttributes().get(AttributeKey.booleanKey("google.genai.stream.capture_truncated"))).isTrue();
        assertThat(span().getAttributes().get(AttributeKey.stringKey(SemanticConventions.OUTPUT_VALUE)).length()).isLessThanOrEqualTo(32768);
    }

    @Test
    void countTokensKeepsExistingEntryPoint() {
        body = "{\"totalTokens\":12}";
        assertThat(model.countTokens("question").totalTokens()).contains(12);
        assertThat(number(span(), SemanticConventions.LLM_TOKEN_COUNT_TOTAL)).isEqualTo(12);
    }

    @Test
    void emptyResponseDoesNotInventTokenCounts() {
        body = "{}";
        assertThat(model.generateContent("question")).isNotNull();
        assertThat(span().getAttributes().get(AttributeKey.longKey(SemanticConventions.LLM_TOKEN_COUNT_TOTAL))).isNull();
    }

    @Test
    @SuppressWarnings("unchecked")
    void cancelledAsyncStreamClosesLateResource() {
        ResponseStream<GenerateContentResponse> source = mock(ResponseStream.class,
            withSettings().mockMaker(org.mockito.MockMakers.SUBCLASS));
        when(source.iterator()).thenReturn(List.<GenerateContentResponse>of().iterator());
        var pending = new CompletableFuture<ResponseStream<GenerateContentResponse>>();
        var operation = new GenAiOperation(tracer, "test", "gemini-test", ai.traceai.FISpanKind.LLM, "question", null);
        var result = TracedGenerativeModel.asyncStream(operation, () -> pending);
        assertThat(result.cancel(true)).isTrue();
        assertThat(pending.isCancelled()).isFalse();
        pending.complete(source);
        verify(source, times(1)).close();
        assertThat(span().getAttributes().get(AttributeKey.booleanKey("google.genai.cancelled"))).isTrue();
    }

    @Test
    @SuppressWarnings("unchecked")
    void closesResourceWhenIteratorCreationFails() {
        ResponseStream<GenerateContentResponse> source = mock(ResponseStream.class,
            withSettings().mockMaker(org.mockito.MockMakers.SUBCLASS));
        RuntimeException failure = new IllegalStateException("iterator failed");
        when(source.iterator()).thenThrow(failure);
        var operation = new GenAiOperation(tracer, "test", "gemini-test", ai.traceai.FISpanKind.LLM, "question", null);
        var future = TracedGenerativeModel.asyncStream(operation, () -> CompletableFuture.completedFuture(source));
        assertThatThrownBy(future::join).hasCause(failure);
        verify(source, times(1)).close();
        assertThat(span().getStatus().getStatusCode()).isEqualTo(StatusCode.ERROR);
    }

    @Test
    @SuppressWarnings("unchecked")
    void streamClosesDelegateOnceOnReadFailure() {
        ResponseStream<GenerateContentResponse> source = mock(ResponseStream.class,
            withSettings().mockMaker(org.mockito.MockMakers.SUBCLASS));
        Iterator<GenerateContentResponse> iterator = mock(Iterator.class,
            withSettings().mockMaker(org.mockito.MockMakers.SUBCLASS));
        when(source.iterator()).thenReturn(iterator);
        RuntimeException failure = new IllegalStateException("read failed");
        when(iterator.hasNext()).thenThrow(failure);
        var operation = new GenAiOperation(tracer, "test", "gemini-test", ai.traceai.FISpanKind.LLM, "question", null);
        var stream = new TracedResponseStream(source, operation);
        assertThatThrownBy(() -> stream.iterator().hasNext()).isSameAs(failure);
        stream.close();
        verify(source, times(1)).close();
        assertThat(span().getStatus().getStatusCode()).isEqualTo(StatusCode.ERROR);
    }
}
