package ai.traceai.anthropic;

import ai.traceai.FITracer;
import ai.traceai.SemanticConventions;
import com.anthropic.client.okhttp.AnthropicOkHttpClient;
import com.anthropic.models.Message;
import com.anthropic.models.MessageCreateParams;
import com.sun.net.httpserver.HttpServer;
import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.sdk.testing.junit5.OpenTelemetryExtension;
import org.junit.jupiter.api.extension.RegisterExtension;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.NullSource;
import org.junit.jupiter.params.provider.ValueSource;

import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.util.concurrent.atomic.AtomicInteger;

import static org.assertj.core.api.Assertions.assertThat;

/** Real pinned SDK transport, with no Anthropic credentials or outbound requests. */
class LegacySdkHttpTest {
    @RegisterExtension
    static final OpenTelemetryExtension otel = OpenTelemetryExtension.create();

    @ParameterizedTest
    @NullSource
    @ValueSource(doubles = {0.0, 0.7})
    void optionalTemperatureReachesTheRealSdkAndExportsOnce(Double temperature) throws Exception {
        var calls = new AtomicInteger();
        var server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        server.createContext("/v1/messages", exchange -> {
            exchange.getRequestBody().readAllBytes();
            calls.incrementAndGet();
            byte[] response = """
                {"id":"msg_synthetic","type":"message","role":"assistant",
                 "model":"test-model","content":[{"type":"text","text":"Paris"}],
                 "stop_reason":"end_turn","stop_sequence":null,
                 "usage":{"input_tokens":11,"output_tokens":7}}
                """.getBytes(StandardCharsets.UTF_8);
            exchange.getResponseHeaders().set("Content-Type", "application/json");
            exchange.sendResponseHeaders(200, response.length);
            exchange.getResponseBody().write(response);
            exchange.close();
        });
        server.start();
        var client = AnthropicOkHttpClient.builder().apiKey("synthetic-key")
            .baseUrl("http://127.0.0.1:" + server.getAddress().getPort()).maxRetries(0).build();
        try {
            var params = MessageCreateParams.builder().model("test-model").maxTokens(64).addUserMessage("Capital of France?");
            if (temperature != null) params.temperature(temperature);
            var traced = new TracedAnthropicClient(client, new FITracer(otel.getOpenTelemetry().getTracer("legacy")));
            Message result = traced.createMessage(params.build());
            assertThat(result.id()).isEqualTo("msg_synthetic");
            assertThat(traced.unwrap()).isSameAs(client);
            assertThat(calls.get()).isEqualTo(1);
            assertThat(otel.getSpans()).hasSize(1);
            var attributes = otel.getSpans().get(0).getAttributes();
            assertThat(attributes.get(AttributeKey.doubleKey(SemanticConventions.LLM_REQUEST_TEMPERATURE)))
                .isEqualTo(temperature);
            assertThat(attributes.get(AttributeKey.longKey(SemanticConventions.LLM_TOKEN_COUNT_PROMPT))).isEqualTo(11L);
            assertThat(attributes.get(AttributeKey.longKey(SemanticConventions.LLM_TOKEN_COUNT_COMPLETION))).isEqualTo(7L);
        } finally {
            client.close();
            server.stop(0);
        }
    }
}
