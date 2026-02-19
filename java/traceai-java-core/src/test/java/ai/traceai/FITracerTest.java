package ai.traceai;

import io.opentelemetry.api.trace.Span;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.sdk.testing.junit5.OpenTelemetryExtension;
import io.opentelemetry.sdk.trace.data.SpanData;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.RegisterExtension;

import java.util.List;

import static org.assertj.core.api.Assertions.*;

class FITracerTest {

    @RegisterExtension
    static final OpenTelemetryExtension otelTesting = OpenTelemetryExtension.create();

    private FITracer tracer;

    @BeforeEach
    void setup() {
        tracer = new FITracer(otelTesting.getOpenTelemetry().getTracer("test"));
    }

    @Test
    void shouldCreateSpanWithCorrectKind() {
        Span span = tracer.startSpan("test-span", FISpanKind.LLM);
        span.end();

        List<SpanData> spans = otelTesting.getSpans();
        assertThat(spans).hasSize(1);

        SpanData spanData = spans.get(0);
        assertThat(spanData.getName()).isEqualTo("test-span");
        assertThat(spanData.getAttributes().get(
            io.opentelemetry.api.common.AttributeKey.stringKey(SemanticConventions.FI_SPAN_KIND)
        )).isEqualTo("LLM");
    }

    @Test
    void shouldSetInputValue() {
        Span span = tracer.startSpan("test", FISpanKind.LLM);
        tracer.setInputValue(span, "test input");
        span.end();

        List<SpanData> spans = otelTesting.getSpans();
        SpanData spanData = spans.get(0);

        assertThat(spanData.getAttributes().get(
            io.opentelemetry.api.common.AttributeKey.stringKey(SemanticConventions.INPUT_VALUE)
        )).isEqualTo("test input");
    }

    @Test
    void shouldHideInputValueWhenConfigured() {
        TraceConfig config = TraceConfig.builder().hideInputs(true).build();
        FITracer tracerWithHiddenInputs = new FITracer(
            otelTesting.getOpenTelemetry().getTracer("test"),
            config
        );

        Span span = tracerWithHiddenInputs.startSpan("test", FISpanKind.LLM);
        tracerWithHiddenInputs.setInputValue(span, "test input");
        span.end();

        List<SpanData> spans = otelTesting.getSpans();
        SpanData spanData = spans.get(0);

        assertThat(spanData.getAttributes().get(
            io.opentelemetry.api.common.AttributeKey.stringKey(SemanticConventions.INPUT_VALUE)
        )).isNull();
    }

    @Test
    void shouldSetOutputValue() {
        Span span = tracer.startSpan("test", FISpanKind.LLM);
        tracer.setOutputValue(span, "test output");
        span.end();

        List<SpanData> spans = otelTesting.getSpans();
        SpanData spanData = spans.get(0);

        assertThat(spanData.getAttributes().get(
            io.opentelemetry.api.common.AttributeKey.stringKey(SemanticConventions.OUTPUT_VALUE)
        )).isEqualTo("test output");
    }

    @Test
    void shouldSetTokenCounts() {
        Span span = tracer.startSpan("test", FISpanKind.LLM);
        tracer.setTokenCounts(span, 100, 50, 150);
        span.end();

        List<SpanData> spans = otelTesting.getSpans();
        SpanData spanData = spans.get(0);

        assertThat(spanData.getAttributes().get(
            io.opentelemetry.api.common.AttributeKey.longKey(SemanticConventions.LLM_TOKEN_COUNT_PROMPT)
        )).isEqualTo(100L);
        assertThat(spanData.getAttributes().get(
            io.opentelemetry.api.common.AttributeKey.longKey(SemanticConventions.LLM_TOKEN_COUNT_COMPLETION)
        )).isEqualTo(50L);
        assertThat(spanData.getAttributes().get(
            io.opentelemetry.api.common.AttributeKey.longKey(SemanticConventions.LLM_TOKEN_COUNT_TOTAL)
        )).isEqualTo(150L);
    }

    @Test
    void shouldSetInputMessage() {
        Span span = tracer.startSpan("test", FISpanKind.LLM);
        tracer.setInputMessage(span, 0, "user", "Hello, world!");
        span.end();

        List<SpanData> spans = otelTesting.getSpans();
        SpanData spanData = spans.get(0);

        assertThat(spanData.getAttributes().get(
            io.opentelemetry.api.common.AttributeKey.stringKey("llm.input_messages.0.message.role")
        )).isEqualTo("user");
        assertThat(spanData.getAttributes().get(
            io.opentelemetry.api.common.AttributeKey.stringKey("llm.input_messages.0.message.content")
        )).isEqualTo("Hello, world!");
    }

    @Test
    void shouldSetOutputMessage() {
        Span span = tracer.startSpan("test", FISpanKind.LLM);
        tracer.setOutputMessage(span, 0, "assistant", "Hi there!");
        span.end();

        List<SpanData> spans = otelTesting.getSpans();
        SpanData spanData = spans.get(0);

        assertThat(spanData.getAttributes().get(
            io.opentelemetry.api.common.AttributeKey.stringKey("llm.output_messages.0.message.role")
        )).isEqualTo("assistant");
        assertThat(spanData.getAttributes().get(
            io.opentelemetry.api.common.AttributeKey.stringKey("llm.output_messages.0.message.content")
        )).isEqualTo("Hi there!");
    }

    @Test
    void shouldRecordError() {
        Span span = tracer.startSpan("test", FISpanKind.LLM);
        RuntimeException error = new RuntimeException("Test error");
        tracer.setError(span, error);
        span.end();

        List<SpanData> spans = otelTesting.getSpans();
        SpanData spanData = spans.get(0);

        assertThat(spanData.getStatus().getStatusCode()).isEqualTo(StatusCode.ERROR);
        assertThat(spanData.getStatus().getDescription()).isEqualTo("Test error");
        assertThat(spanData.getEvents()).isNotEmpty();
    }

    @Test
    void shouldTraceSupplier() {
        String result = tracer.trace("test-operation", FISpanKind.LLM, () -> "result");

        assertThat(result).isEqualTo("result");

        List<SpanData> spans = otelTesting.getSpans();
        assertThat(spans).hasSize(1);
        assertThat(spans.get(0).getName()).isEqualTo("test-operation");
        assertThat(spans.get(0).getStatus().getStatusCode()).isEqualTo(StatusCode.OK);
    }

    @Test
    void shouldTraceSupplierWithError() {
        assertThatThrownBy(() ->
            tracer.trace("test-operation", FISpanKind.LLM, () -> {
                throw new RuntimeException("Test error");
            })
        ).isInstanceOf(RuntimeException.class).hasMessage("Test error");

        List<SpanData> spans = otelTesting.getSpans();
        assertThat(spans).hasSize(1);
        assertThat(spans.get(0).getStatus().getStatusCode()).isEqualTo(StatusCode.ERROR);
    }

    @Test
    void shouldTraceRunnable() {
        tracer.trace("test-operation", FISpanKind.TOOL, () -> {
            // do nothing
        });

        List<SpanData> spans = otelTesting.getSpans();
        assertThat(spans).hasSize(1);
        assertThat(spans.get(0).getName()).isEqualTo("test-operation");
        assertThat(spans.get(0).getStatus().getStatusCode()).isEqualTo(StatusCode.OK);
    }

    @Test
    void shouldSerializeObjectToJson() {
        String json = tracer.toJson(new TestObject("test", 42));
        assertThat(json).contains("\"name\":\"test\"");
        assertThat(json).contains("\"value\":42");
    }

    static class TestObject {
        String name;
        int value;

        TestObject(String name, int value) {
            this.name = name;
            this.value = value;
        }
    }
}
