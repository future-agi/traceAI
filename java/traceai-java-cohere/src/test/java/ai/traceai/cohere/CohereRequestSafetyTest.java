package ai.traceai.cohere;

import ai.traceai.FITracer;
import ai.traceai.SemanticConventions;
import ai.traceai.TraceConfig;
import com.cohere.api.Cohere;
import com.cohere.api.core.ObjectMappers;
import com.cohere.api.requests.ChatRequest;
import com.cohere.api.requests.ChatStreamRequest;
import com.cohere.api.resources.v2.requests.V2EmbedRequest;
import com.sun.net.httpserver.HttpServer;
import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.sdk.testing.junit5.OpenTelemetryExtension;
import io.opentelemetry.sdk.trace.data.SpanData;
import org.junit.jupiter.api.extension.RegisterExtension;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.Arguments;
import org.junit.jupiter.params.provider.CsvSource;
import org.junit.jupiter.params.provider.MethodSource;
import org.junit.jupiter.params.provider.ValueSource;

import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.util.concurrent.atomic.AtomicReference;
import java.util.stream.Stream;

import static org.assertj.core.api.Assertions.*;

/** Real SDK requests over loopback, with synthetic credentials and no provider access. */
class CohereRequestSafetyTest {
    @RegisterExtension
    static final OpenTelemetryExtension otel = OpenTelemetryExtension.create();

    private static final String CONNECTOR_TOKEN = "synthetic-connector-bearer-token";
    private static final String SECOND_TOKEN = "synthetic-second-connector-token";
    private static final String CONNECTOR_REQUEST = """
        {"model":"test-model","message":"visible-input","connectors":[
          {"id":"internal-docs","user_access_token":"%s"},
          {"id":"other-docs","user_access_token":"%s"},
          {"id":"public-docs"}]}
        """.formatted(CONNECTOR_TOKEN, SECOND_TOKEN);
    private static final String V1_RESPONSE = "{\"text\":\"visible-output\",\"generation_id\":\"response-id\",\"finish_reason\":\"COMPLETE\"}";

    @ParameterizedTest
    @CsvSource({
        "true,200,0", "true,401,0", "false,200,0", "false,401,0",
        "true,200,1", "true,401,1", "false,200,1", "false,401,1",
        "true,200,2", "true,401,2", "false,200,2", "false,401,2"
    })
    void connectorCredentialsAreTransportOnly(boolean streaming, int status, int privacyFlag) throws Exception {
        String body = status == 401 ? "{\"message\":\"synthetic-provider-failure\"}"
            : streaming ? streamResponse(V1_RESPONSE) : V1_RESPONSE;
        try (var server = new ProviderServer(status, body)) {
            var config = TraceConfig.builder().hideInputs(privacyFlag == 1)
                .hideInputMessages(privacyFlag == 2).build();
            var traced = server.traced(config);
            Class<?> requestType = streaming ? ChatStreamRequest.class : ChatRequest.class;
            Object request = ObjectMappers.JSON_MAPPER.readValue(CONNECTOR_REQUEST, requestType);
            org.assertj.core.api.ThrowableAssert.ThrowingCallable call = () -> {
                if (streaming) {
                    try (var stream = traced.chatStream((ChatStreamRequest) request)) {
                        stream.forEach(event -> {});
                    }
                } else {
                    assertThat(traced.chat((ChatRequest) request).getText()).isEqualTo("visible-output");
                }
            };
            if (status == 401) {
                assertThatThrownBy(call).isInstanceOf(RuntimeException.class);
            } else {
                assertThatCode(call).doesNotThrowAnyException();
            }

            // Sanitization must not mutate either the SDK request or the bytes on the wire.
            assertCredentials(ObjectMappers.JSON_MAPPER.valueToTree(request).toString());
            assertCredentials(server.request.get());
            SpanData span = onlySpan();
            assertThat(span.getStatus().getStatusCode()).isEqualTo(status == 401 ? StatusCode.ERROR : StatusCode.OK);
            assertThat(span.getAttributes().toString()).doesNotContain(CONNECTOR_TOKEN, SECOND_TOKEN);
            assertThat(span.getEvents().toString()).doesNotContain(CONNECTOR_TOKEN, SECOND_TOKEN);
            assertThat(span.getStatus().getDescription()).doesNotContain(CONNECTOR_TOKEN, SECOND_TOKEN);
            if (privacyFlag == 0) {
                var raw = ObjectMappers.JSON_MAPPER.readTree(span.getAttributes().get(AttributeKey.stringKey(SemanticConventions.RAW_INPUT)));
                assertThat(raw.path("message").asText()).isEqualTo("visible-input");
                assertThat(raw.path("connectors")).hasSize(3);
                assertThat(raw.path("connectors").get(0).path("id").asText()).isEqualTo("internal-docs");
                for (var connector : raw.path("connectors")) {
                    assertThat(connector.has("user_access_token")).isFalse();
                }
            }
        }
    }

    @ParameterizedTest
    @MethodSource("embeddingUsage")
    void embeddingUsageUsesBilledInputOnlyAsFallback(String meta, Long input, Long billed, Long output, Long total) throws Exception {
        String response = "{\"id\":\"embed-id\",\"embeddings\":{\"float\":[[0.1,0.2]]},\"meta\":" + meta + "}";
        try (var server = new ProviderServer(200, response)) {
            var request = ObjectMappers.JSON_MAPPER.readValue("""
                {"model":"embed-model","input_type":"search_document","texts":["visible-input"]}
                """, V2EmbedRequest.class);
            var result = server.traced(TraceConfig.builder().build()).v2().embed(request);
            assertThat(result.getEmbeddings().getFloat().orElseThrow()).hasSize(1);
            SpanData span = onlySpan();
            assertThat(span.getAttributes().get(AttributeKey.longKey("cohere.billed_units.input_tokens"))).isEqualTo(billed);
            assertThat(span.getAttributes().get(AttributeKey.longKey(SemanticConventions.LLM_TOKEN_COUNT_PROMPT))).isEqualTo(input);
            assertThat(span.getAttributes().get(AttributeKey.longKey(SemanticConventions.LLM_TOKEN_COUNT_COMPLETION))).isEqualTo(output);
            assertThat(span.getAttributes().get(AttributeKey.longKey(SemanticConventions.LLM_TOKEN_COUNT_TOTAL))).isEqualTo(total);
        }
    }

    static Stream<Arguments> embeddingUsage() {
        return Stream.of(
            Arguments.of("{\"billed_units\":{\"input_tokens\":4}}", 4L, 4L, null, null),
            Arguments.of("{\"tokens\":{},\"billed_units\":{\"input_tokens\":4}}", 4L, 4L, null, null),
            Arguments.of("{\"tokens\":{\"input_tokens\":9},\"billed_units\":{\"input_tokens\":4}}", 9L, 4L, null, null),
            Arguments.of("{\"tokens\":{\"input_tokens\":0},\"billed_units\":{\"input_tokens\":4}}", 0L, 4L, null, null),
            Arguments.of("{\"tokens\":{\"input_tokens\":9,\"output_tokens\":2},\"billed_units\":{\"input_tokens\":4}}", 9L, 4L, 2L, 11L),
            Arguments.of("{\"tokens\":{\"output_tokens\":2},\"billed_units\":{\"input_tokens\":4}}", 4L, 4L, 2L, null),
            Arguments.of("{\"tokens\":{\"input_tokens\":9}}", 9L, null, null, null),
            Arguments.of("{\"billed_units\":{\"input_tokens\":0}}", 0L, 0L, null, null),
            Arguments.of("{}", null, null, null, null)
        );
    }

    @ParameterizedTest
    @ValueSource(booleans = {false, true})
    void chatNeverSubstitutesBilledUnitsForActualTokenUsage(boolean hasTokens) throws Exception {
        String meta = "{\"billed_units\":{\"input_tokens\":4}"
            + (hasTokens ? ",\"tokens\":{\"input_tokens\":9,\"output_tokens\":2}" : "") + "}";
        String response = "{\"text\":\"visible-output\",\"meta\":" + meta + "}";
        try (var server = new ProviderServer(200, streamResponse(response))) {
            try (var stream = server.traced(TraceConfig.builder().build()).chatStream(
                    ChatStreamRequest.builder().message("visible-input").build())) {
                stream.forEach(event -> {});
            }
            SpanData span = onlySpan();
            assertThat(span.getAttributes().get(AttributeKey.longKey("cohere.billed_units.input_tokens"))).isEqualTo(4L);
            assertThat(span.getAttributes().get(AttributeKey.longKey(SemanticConventions.LLM_TOKEN_COUNT_PROMPT))).isEqualTo(hasTokens ? 9L : null);
            assertThat(span.getAttributes().get(AttributeKey.longKey(SemanticConventions.LLM_TOKEN_COUNT_COMPLETION))).isEqualTo(hasTokens ? 2L : null);
            assertThat(span.getAttributes().get(AttributeKey.longKey(SemanticConventions.LLM_TOKEN_COUNT_TOTAL))).isEqualTo(hasTokens ? 11L : null);
        }
    }

    private static String streamResponse(String response) {
        return "{\"event_type\":\"stream-end\",\"finish_reason\":\"COMPLETE\",\"response\":" + response + "}\n";
    }

    private static void assertCredentials(String json) throws Exception {
        var connectors = ObjectMappers.JSON_MAPPER.readTree(json).path("connectors");
        assertThat(connectors.get(0).path("user_access_token").asText()).isEqualTo(CONNECTOR_TOKEN);
        assertThat(connectors.get(1).path("user_access_token").asText()).isEqualTo(SECOND_TOKEN);
        assertThat(connectors.get(2).has("user_access_token")).isFalse();
    }

    private static SpanData onlySpan() {
        assertThat(otel.getSpans()).hasSize(1);
        return otel.getSpans().get(0);
    }

    private static final class ProviderServer implements AutoCloseable {
        private final HttpServer server;
        private final AtomicReference<String> request = new AtomicReference<>();

        ProviderServer(int status, String body) throws Exception {
            server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
            server.createContext("/", exchange -> {
                try {
                    request.set(new String(exchange.getRequestBody().readAllBytes(), StandardCharsets.UTF_8));
                    byte[] bytes = body.getBytes(StandardCharsets.UTF_8);
                    exchange.getResponseHeaders().set("Content-Type", "application/json");
                    exchange.sendResponseHeaders(status, bytes.length);
                    exchange.getResponseBody().write(bytes);
                } finally {
                    exchange.close();
                }
            });
            server.start();
        }

        TracedCohereClient traced(TraceConfig config) {
            var client = Cohere.builder().token("synthetic-provider-key").maxRetries(0)
                .url("http://127.0.0.1:" + server.getAddress().getPort()).build();
            return new TracedCohereClient(client, new FITracer(otel.getOpenTelemetry().getTracer("cohere-safety-test"), config));
        }

        @Override
        public void close() {
            server.stop(0);
        }
    }
}
