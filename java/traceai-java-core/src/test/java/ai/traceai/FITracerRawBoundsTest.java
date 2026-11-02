package ai.traceai;

import com.google.gson.JsonParser;
import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.api.common.Attributes;
import io.opentelemetry.sdk.testing.junit5.OpenTelemetryExtension;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.RegisterExtension;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;

import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

class FITracerRawBoundsTest {
    @RegisterExtension
    static final OpenTelemetryExtension otel = OpenTelemetryExtension.create();

    @ParameterizedTest
    @ValueSource(ints = {31999, 32000, 32001, 40000})
    void boundsPreSerializedJsonWithoutCuttingIt(int length) {
        String json = "\"" + "a".repeat(length - 2) + "\"";
        assertRawBounds(capture(json, TraceConfig.builder().build()), json);
    }

    @ParameterizedTest
    @ValueSource(strings = {"a", "\"\\\n\t", "🚀"})
    void boundsSerializedObjectsIncludingEscaping(String pattern) {
        var tracer = new FITracer(otel.getOpenTelemetry().getTracer("raw-bounds"));
        var value = Map.of("text", pattern.repeat(20000));
        assertRawBounds(capture(value, TraceConfig.builder().build()), tracer.toJson(value));
        assertThat(value.get("text")).isEqualTo(pattern.repeat(20000));
    }

    @Test
    void hiddenOrNullRawPayloadsDoNotEmitTruncationMetadata() {
        for (Object value : new Object[] {null, Map.of("text", "a".repeat(40000))}) {
            var attrs = capture(value, TraceConfig.builder().hideInputs(true).hideOutputs(true).build());
            assertThat(attrs.get(AttributeKey.stringKey("raw.input"))).isNull();
            assertThat(attrs.get(AttributeKey.stringKey("raw.output"))).isNull();
            assertThat(attrs.get(AttributeKey.booleanKey("fi.raw_input_truncated"))).isNull();
            assertThat(attrs.get(AttributeKey.booleanKey("fi.raw_output_truncated"))).isNull();
        }
        var attrs = capture(null, TraceConfig.builder().build());
        assertThat(attrs.get(AttributeKey.stringKey("raw.input"))).isNull();
        assertThat(attrs.get(AttributeKey.stringKey("raw.output"))).isNull();
        assertThat(attrs.get(AttributeKey.booleanKey("fi.raw_input_truncated"))).isNull();
        assertThat(attrs.get(AttributeKey.booleanKey("fi.raw_output_truncated"))).isNull();
    }

    private void assertRawBounds(Attributes attrs, String json) {
        for (String direction : new String[] {"input", "output"}) {
            String actual = attrs.get(AttributeKey.stringKey("raw." + direction));
            Boolean truncated = attrs.get(AttributeKey.booleanKey("fi.raw_" + direction + "_truncated"));
            if (json.length() > 32000) {
                assertThat(actual).as("oversized raw JSON must be omitted, not sliced").isNull();
                assertThat(truncated).isTrue();
            } else {
                assertThat(actual).isEqualTo(json);
                assertThat(JsonParser.parseString(actual)).isEqualTo(JsonParser.parseString(json));
                assertThat(truncated).isNull();
            }
        }
    }

    private Attributes capture(Object value, TraceConfig config) {
        var tracer = new FITracer(otel.getOpenTelemetry().getTracer("raw-bounds"), config);
        var span = tracer.startSpan("raw", FISpanKind.LLM);
        tracer.setRawInput(span, value);
        tracer.setRawOutput(span, value);
        span.end();
        return otel.getSpans().get(otel.getSpans().size() - 1).getAttributes();
    }
}
