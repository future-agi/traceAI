package ai.traceai.ollama;

import ai.traceai.FITracer;
import ai.traceai.SemanticConventions;
import ai.traceai.TraceConfig;
import com.google.gson.JsonObject;
import com.google.gson.JsonParser;
import com.sun.net.httpserver.HttpServer;
import io.github.ollama4j.OllamaAPI;
import io.github.ollama4j.exceptions.OllamaBaseException;
import io.github.ollama4j.models.chat.OllamaChatMessage;
import io.github.ollama4j.models.chat.OllamaChatMessageRole;
import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.sdk.testing.junit5.OpenTelemetryExtension;
import io.opentelemetry.sdk.trace.data.SpanData;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.RegisterExtension;

import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.util.List;
import java.util.concurrent.CopyOnWriteArrayList;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/** Real Ollama4j 1.0.79 transport against a synthetic loopback server; no models or credentials. */
class TracedOllamaAPITest {
    @RegisterExtension
    static final OpenTelemetryExtension telemetry = OpenTelemetryExtension.create();

    private HttpServer server;
    private OllamaAPI api;
    private TracedOllamaAPI traced;
    private final List<String> paths = new CopyOnWriteArrayList<>();
    private final List<JsonObject> requests = new CopyOnWriteArrayList<>();
    private volatile int status = 200;

    @BeforeEach
    void setUp() throws Exception {
        server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        server.createContext("/", exchange -> {
            String path = exchange.getRequestURI().getPath();
            paths.add(path);
            String body = new String(exchange.getRequestBody().readAllBytes(), StandardCharsets.UTF_8);
            if (!body.isEmpty()) {
                requests.add(JsonParser.parseString(body).getAsJsonObject());
            }
            String response = status != 200 ? "{\"error\":\"synthetic failure\"}" : switch (path) {
                case "/api/generate" -> "{\"model\":\"test-model\",\"response\":\"Hello\",\"done\":true}";
                case "/api/chat" -> "{\"model\":\"test-model\",\"message\":{\"role\":\"assistant\",\"content\":\"Hello\"},\"done\":true}";
                case "/api/embeddings" -> "{\"embedding\":[0.1,0.2,0.3]}";
                case "/api/tags" -> "{\"models\":[{\"name\":\"test-model\",\"model\":\"test-model\"}]}";
                default -> throw new IllegalStateException("Unexpected path: " + path);
            };
            byte[] bytes = response.getBytes(StandardCharsets.UTF_8);
            exchange.getResponseHeaders().set("Content-Type", "application/json");
            exchange.sendResponseHeaders(status, bytes.length);
            try (var output = exchange.getResponseBody()) {
                output.write(bytes);
            }
        });
        server.start();
        api = new OllamaAPI("http://127.0.0.1:" + server.getAddress().getPort());
        api.setRequestTimeoutSeconds(5);
        traced = new TracedOllamaAPI(api, new FITracer(telemetry.getOpenTelemetry().getTracer("test"),
            TraceConfig.builder().hideInputs(false).hideOutputs(false)
                .hideInputMessages(false).hideOutputMessages(false).hideErrorDetails(false).build()));
    }

    @AfterEach
    void tearDown() {
        if (server != null) {
            server.stop(0);
        }
    }

    @Test
    void generateUsesEmptyOptionsAndPreservesResponse() throws Exception {
        var result = traced.generate("test-model", "Hello?");

        assertThat(result.getResponse()).isEqualTo("Hello");
        assertThat(result.getHttpStatusCode()).isEqualTo(200);
        assertThat(paths).containsExactly("/api/generate");
        JsonObject request = requests.get(0);
        assertThat(request.get("model").getAsString()).isEqualTo("test-model");
        assertThat(request.get("prompt").getAsString()).isEqualTo("Hello?");
        assertThat(request.getAsJsonObject("options").size()).isZero();
        assertThat(request.get("raw").getAsBoolean()).isFalse();
        assertThat(request.get("stream").getAsBoolean()).isFalse();
        SpanData span = onlySpan("Ollama Generate", StatusCode.OK);
        assertThat(attribute(span, SemanticConventions.FI_SPAN_KIND)).isEqualTo("LLM");
        assertThat(attribute(span, SemanticConventions.LLM_MODEL_NAME)).isEqualTo("test-model");
        assertThat(attribute(span, SemanticConventions.OUTPUT_VALUE)).isEqualTo("Hello");
    }

    @Test
    void chatPreservesMessagesAndResponse() throws Exception {
        var result = traced.chat("test-model", List.of(
            new OllamaChatMessage(OllamaChatMessageRole.USER, "Hello?")));

        assertThat(result.getResponse()).isEqualTo("Hello");
        assertThat(paths).containsExactly("/api/chat");
        JsonObject message = requests.get(0).getAsJsonArray("messages").get(0).getAsJsonObject();
        assertThat(message.get("role").getAsString()).isEqualTo("user");
        assertThat(message.get("content").getAsString()).isEqualTo("Hello?");
        SpanData span = onlySpan("Ollama Chat", StatusCode.OK);
        assertThat(attribute(span, SemanticConventions.LLM_INPUT_MESSAGES)).contains("Hello?");
        assertThat(attribute(span, SemanticConventions.OUTPUT_VALUE)).isEqualTo("Hello");
    }

    @Test
    void embedPreservesVectorAndRecordsDimensions() throws Exception {
        assertThat(traced.embed("test-model", "hello")).containsExactly(0.1, 0.2, 0.3);
        assertThat(paths).containsExactly("/api/embeddings");
        SpanData span = onlySpan("Ollama Embed", StatusCode.OK);
        assertThat(attribute(span, SemanticConventions.FI_SPAN_KIND)).isEqualTo("EMBEDDING");
        assertThat(span.getAttributes().get(AttributeKey.longKey(SemanticConventions.EMBEDDING_DIMENSIONS)))
            .isEqualTo(3L);
    }

    @Test
    void embedBatchUsesOneSpanForAllRequests() throws Exception {
        assertThat(traced.embedBatch("test-model", List.of("first", "second")))
            .containsExactly(List.of(0.1, 0.2, 0.3), List.of(0.1, 0.2, 0.3));
        assertThat(paths).containsExactly("/api/embeddings", "/api/embeddings");
        SpanData span = onlySpan("Ollama Embed Batch", StatusCode.OK);
        assertThat(span.getAttributes().get(AttributeKey.longKey(SemanticConventions.EMBEDDING_VECTOR_COUNT)))
            .isEqualTo(2L);
    }

    @Test
    void listModelsPreservesNames() throws Exception {
        assertThat(traced.listModels()).containsExactly("test-model");
        assertThat(paths).containsExactly("/api/tags");
        onlySpan("Ollama List Models", StatusCode.OK);
    }

    @Test
    void generatePreservesSdkExceptionAndEndsSpan() {
        status = 400;
        assertThatThrownBy(() -> traced.generate("test-model", "hello"))
            .isInstanceOf(OllamaBaseException.class).hasMessageContaining("synthetic failure");
        assertThat(paths).containsExactly("/api/generate");
        assertThat(onlySpan("Ollama Generate", StatusCode.ERROR).getEvents()).hasSize(1);
    }

    @Test
    void unwrapKeepsTheOriginalClient() {
        assertThat(traced.unwrap()).isSameAs(api);
        assertThat(telemetry.getSpans()).isEmpty();
    }

    private SpanData onlySpan(String name, StatusCode statusCode) {
        assertThat(telemetry.getSpans()).hasSize(1);
        SpanData span = telemetry.getSpans().get(0);
        assertThat(span.getName()).isEqualTo(name);
        assertThat(span.getStatus().getStatusCode()).isEqualTo(statusCode);
        return span;
    }

    private String attribute(SpanData span, String name) {
        return span.getAttributes().get(AttributeKey.stringKey(name));
    }
}
