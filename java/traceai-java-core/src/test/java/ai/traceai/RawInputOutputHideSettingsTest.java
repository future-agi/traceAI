package ai.traceai;

import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.sdk.testing.junit5.OpenTelemetryExtension;
import io.opentelemetry.sdk.trace.data.SpanData;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.RegisterExtension;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;

import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

/** The raw request and response objects are not recorded while any hide setting is on. */
class RawInputOutputHideSettingsTest {
    @RegisterExtension
    static final OpenTelemetryExtension otel = OpenTelemetryExtension.create();

    static final String IN = "SENTINEL-RAW-IN";
    static final String OUT = "SENTINEL-RAW-OUT";
    static final List<String> RAW_KEYS = List.of("raw.input", "fi.raw_input", "raw.output", "fi.raw_output");

    private static TraceConfig config(String flag) {
        TraceConfig.Builder b = TraceConfig.builder().hideInputs(false).hideOutputs(false)
            .hideInputMessages(false).hideOutputMessages(false);
        switch (flag) {
            case "hideInputs": b.hideInputs(true); break;
            case "hideOutputs": b.hideOutputs(true); break;
            case "hideInputMessages": b.hideInputMessages(true); break;
            case "hideOutputMessages": b.hideOutputMessages(true); break;
            default: break;
        }
        return b.build();
    }

    private SpanData run(String flag) {
        otel.clearSpans();
        FITracer tracer = new FITracer(otel.getOpenTelemetry().getTracer("raw-test"), config(flag));
        Span span = tracer.startSpan("raw", FISpanKind.LLM);
        tracer.setRawInput(span, Map.of("echo", IN));
        tracer.setRawOutput(span, Map.of("echo", OUT));
        for (String key : RAW_KEYS) tracer.setAttribute(span, key, key.contains("input") ? IN : OUT);
        span.end();
        return otel.getSpans().get(0);
    }

    private static String dump(SpanData data) {
        StringBuilder sb = new StringBuilder();
        data.getAttributes().forEach((k, v) -> sb.append(k.getKey()).append('=').append(v).append('\n'));
        return sb.toString();
    }

    @ParameterizedTest
    @ValueSource(strings = {"hideInputs", "hideOutputs", "hideInputMessages", "hideOutputMessages"})
    void rawInputAndOutputAreNotRecordedUnderAnyHideSetting(String flag) {
        String dumped = dump(run(flag));
        assertThat(dumped).as(flag).doesNotContain(IN).doesNotContain(OUT);
    }

    @ParameterizedTest
    @ValueSource(strings = {"hideInputs", "hideOutputs", "hideInputMessages", "hideOutputMessages"})
    void rawKeysWrittenDirectlyAreMaskedUnderAnyHideSetting(String flag) {
        TraceConfig config = config(flag);
        for (String key : RAW_KEYS) {
            assertThat(config.maskAttribute(key, "value")).as("%s under %s", key, flag)
                .satisfiesAnyOf(v -> assertThat(v).isNull(), v -> assertThat(v).isEqualTo(ContentKeys.REDACTED));
        }
    }

    @Test
    void withNoHideSettingRawInputAndOutputAreRecorded() {
        SpanData data = run("none");
        assertThat(data.getAttributes().get(AttributeKey.stringKey(SemanticConventions.RAW_INPUT))).contains(IN);
        assertThat(data.getAttributes().get(AttributeKey.stringKey(SemanticConventions.RAW_OUTPUT))).contains(OUT);
    }
}
