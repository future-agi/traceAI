package ai.traceai;

import com.google.gson.JsonParser;
import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.sdk.testing.junit5.OpenTelemetryExtension;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.RegisterExtension;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;

import java.util.Collections;
import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

class FITracerMessageBoundsTest {
    @RegisterExtension
    static final OpenTelemetryExtension otel = OpenTelemetryExtension.create();

    @ParameterizedTest
    @ValueSource(strings = {"a", "\"\\\n\t\u0001", "🚀"})
    void boundsSerializedMessagesWithoutChangingCallerData(String pattern) {
        String content = pattern.repeat(32000);
        var messages = List.of(FITracer.message("system", "unchanged"), FITracer.message("user", content));
        for (String key : capture(messages)) {
            assertThat(key.length()).isLessThanOrEqualTo(32000);
            var parsed = JsonParser.parseString(key).getAsJsonArray();
            assertThat(parsed).hasSize(2);
            assertThat(parsed.get(0).getAsJsonObject().get("content").getAsString()).isEqualTo("unchanged");
            String truncated = parsed.get(1).getAsJsonObject().get("content").getAsString();
            assertThat(truncated).endsWith("...[truncated]");
            String prefix = truncated.substring(0, truncated.length() - "...[truncated]".length());
            assertThat(content).startsWith(prefix);
            assertThat(Character.isHighSurrogate(prefix.charAt(prefix.length() - 1))).isFalse();
        }
        assertThat(messages.get(1).get("content")).isEqualTo(content);
    }

    @Test
    void boundsManyMessagesAndOversizedNonContentFields() {
        for (String json : capture(Collections.nCopies(4000, FITracer.message("user", "hello")))) {
            assertThat(json.length()).isLessThanOrEqualTo(32000);
            assertThat(JsonParser.parseString(json).getAsJsonArray().size()).isBetween(1, 3999);
        }
        for (String json : capture(List.of(FITracer.message("x".repeat(32000), "hello")))) {
            assertThat(json).isEqualTo("[]");
        }
        for (String json : capture(List.of(Map.of("role", "x".repeat(32000))))) {
            assertThat(json).isEqualTo("[]");
        }
    }

    @Test
    void preservesExactBoundaryAndShortMessages() {
        for (int length : List.of(0, 31970)) {
            String content = "a".repeat(length);
            for (String json : capture(List.of(FITracer.message("user", content)))) {
                assertThat(json).hasSize(length + 30);
                assertThat(JsonParser.parseString(json).getAsJsonArray().get(0).getAsJsonObject()
                    .get("content").getAsString()).isEqualTo(content);
            }
        }
    }

    @Test
    void plainValuesRespectTheSameLimitIncludingTheTruncationMarker() {
        var tracer = new FITracer(otel.getOpenTelemetry().getTracer("bounds"));
        var span = tracer.startSpan("value", FISpanKind.LLM);
        tracer.setOutputValue(span, "a".repeat(32001));
        span.end();
        assertThat(otel.getSpans().get(0).getAttributes().get(AttributeKey.stringKey("output.value")))
            .hasSize(32000).endsWith("...[truncated]");
    }

    private List<String> capture(List<Map<String, String>> messages) {
        var tracer = new FITracer(otel.getOpenTelemetry().getTracer("bounds"));
        var span = tracer.startSpan("messages", FISpanKind.LLM);
        tracer.setInputMessages(span, messages);
        tracer.setOutputMessages(span, messages);
        span.end();
        var attrs = otel.getSpans().get(otel.getSpans().size() - 1).getAttributes();
        return List.of(attrs.get(AttributeKey.stringKey("gen_ai.input.messages")),
            attrs.get(AttributeKey.stringKey("gen_ai.output.messages")));
    }
}
