package ai.traceai;

import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.sdk.testing.junit5.OpenTelemetryExtension;
import io.opentelemetry.sdk.trace.data.SpanData;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.RegisterExtension;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;

import java.util.List;
import java.util.Map;
import java.util.function.Supplier;

import static org.assertj.core.api.Assertions.*;

class FITracerPrivacyTest {
    @RegisterExtension
    static final OpenTelemetryExtension otel = OpenTelemetryExtension.create();

    @ParameterizedTest
    @ValueSource(ints = {0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15})
    void shouldApplyContentFlagsIndependently(int flags) {
        boolean inputs = (flags & 1) != 0;
        boolean outputs = (flags & 2) != 0;
        boolean inputMessages = (flags & 4) != 0;
        boolean outputMessages = (flags & 8) != 0;
        FITracer tracer = tracer(TraceConfig.builder()
            .hideInputs(inputs).hideOutputs(outputs)
            .hideInputMessages(inputMessages).hideOutputMessages(outputMessages).build());
        Span span = tracer.startSpan("privacy", FISpanKind.LLM);
        tracer.setInputValue(span, "private-input");
        tracer.setRawInput(span, Map.of("input", "private-input"));
        tracer.setInputMessages(span, List.of(FITracer.message("user", "private-input")));
        tracer.setOutputValue(span, "private-output");
        tracer.setRawOutput(span, Map.of("output", "private-output"));
        tracer.setOutputMessages(span, List.of(FITracer.message("assistant", "private-output")));
        tracer.setTokenCounts(span, 2, 3, 5);
        span.end();

        SpanData data = otel.getSpans().get(0);
        assertThat(value(data, SemanticConventions.INPUT_VALUE) == null).isEqualTo(inputs);
        assertThat(value(data, SemanticConventions.RAW_INPUT) == null).isEqualTo(inputs);
        assertThat(value(data, SemanticConventions.LLM_INPUT_MESSAGES) == null)
            .isEqualTo(inputs || inputMessages);
        assertThat(value(data, SemanticConventions.OUTPUT_VALUE) == null).isEqualTo(outputs);
        assertThat(value(data, SemanticConventions.RAW_OUTPUT) == null).isEqualTo(outputs);
        assertThat(value(data, SemanticConventions.LLM_OUTPUT_MESSAGES) == null)
            .isEqualTo(outputs || outputMessages);
        assertThat(data.getAttributes().get(AttributeKey.longKey(
            SemanticConventions.LLM_TOKEN_COUNT_TOTAL))).isEqualTo(5L);
    }

    @ParameterizedTest
    @ValueSource(booleans = {false, true})
    void shouldHideErrorDetailsOnlyWhenEnabled(boolean hide) {
        FITracer tracer = tracer(TraceConfig.builder().hideInputs(true).hideOutputs(true)
            .hideErrorDetails(hide).build());
        Span span = tracer.startSpan("privacy", FISpanKind.LLM);
        IllegalStateException failure = new IllegalStateException("private-error",
            new RuntimeException("private-cause"));
        tracer.setError(span, failure);
        span.end();

        SpanData data = otel.getSpans().get(0);
        assertThat(data.getStatus().getStatusCode()).isEqualTo(StatusCode.ERROR);
        assertThat(value(data, SemanticConventions.ERROR_TYPE))
            .isEqualTo(IllegalStateException.class.getName());
        assertThat(data.getEvents()).hasSize(1);
        assertThat(data.getEvents().get(0).getName()).isEqualTo("exception");
        assertThat(data.getEvents().get(0).getAttributes().get(AttributeKey.stringKey("exception.type")))
            .isEqualTo(IllegalStateException.class.getName());
        if (hide) {
            assertThat(data.getStatus().getDescription()).isEmpty();
            assertThat(value(data, SemanticConventions.ERROR_MESSAGE)).isNull();
            assertThat(data.getEvents().get(0).getAttributes().asMap())
                .containsOnlyKeys(AttributeKey.stringKey("exception.type"));
            assertThat(data.getAttributes().toString()).doesNotContain("private-error", "private-cause");
        } else {
            assertThat(data.getStatus().getDescription()).isEqualTo("private-error");
            assertThat(value(data, SemanticConventions.ERROR_MESSAGE)).isEqualTo("private-error");
            assertThat(data.getEvents().get(0).getAttributes().get(AttributeKey.stringKey("exception.message")))
                .isEqualTo("private-error");
            assertThat(data.getEvents().get(0).getAttributes().get(AttributeKey.stringKey("exception.stacktrace")))
                .contains("private-error", "private-cause");
        }
    }

    @ParameterizedTest
    @ValueSource(booleans = {false, true})
    void shouldPreserveThrownExceptionInBothTraceHelpers(boolean supplier) {
        FITracer tracer = tracer(TraceConfig.builder().hideErrorDetails(true).build());
        RuntimeException failure = new RuntimeException("private-error");
        assertThatThrownBy(() -> {
            if (supplier) {
                tracer.trace("supplier", FISpanKind.LLM, (Supplier<String>) () -> { throw failure; });
            } else {
                tracer.trace("runnable", FISpanKind.LLM, (Runnable) () -> { throw failure; });
            }
        }).isSameAs(failure);
        SpanData data = otel.getSpans().get(0);
        assertThat(data.getStatus().getDescription()).isEmpty();
        assertThat(data.getStatus().getStatusCode()).isEqualTo(StatusCode.ERROR);
        assertThat(data.getEvents().get(0).getAttributes().asMap())
            .containsOnlyKeys(AttributeKey.stringKey("exception.type"));
    }

    @Test
    void shouldReadEnvironmentForBothConfigurationEntryPoints() {
        boolean expected = Boolean.parseBoolean(System.getenv("FI_HIDE_ERROR_DETAILS"));
        assertThat(TraceConfig.builder().build().isHideErrorDetails()).isEqualTo(expected);
        assertThat(TraceConfig.fromEnvironment().isHideErrorDetails()).isEqualTo(expected);
        // Content flags do not implicitly enable error-detail hiding in Java.
        assertThat(TraceConfig.builder().hideInputs(true).hideOutputs(true).build().isHideErrorDetails())
            .isEqualTo(expected);
    }

    @ParameterizedTest
    @ValueSource(booleans = {false, true})
    void shouldPreferExplicitErrorDetailsFlagOverEnvironment(boolean hide) {
        assertThat(TraceConfig.builder().hideErrorDetails(hide).build().isHideErrorDetails())
            .isEqualTo(hide);
    }

    private FITracer tracer(TraceConfig config) {
        return new FITracer(otel.getOpenTelemetry().getTracer("privacy-test"), config);
    }

    private String value(SpanData data, String key) {
        return data.getAttributes().get(AttributeKey.stringKey(key));
    }
}
