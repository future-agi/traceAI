package ai.traceai;

import io.opentelemetry.api.trace.Span;
import io.opentelemetry.sdk.testing.junit5.OpenTelemetryExtension;
import io.opentelemetry.sdk.trace.data.EventData;
import io.opentelemetry.sdk.trace.data.SpanData;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.RegisterExtension;

import java.util.List;
import java.util.Map;
import java.util.function.Consumer;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * The FITracer public content paths (setInputValue, setOutputValue, setRawInput, setRawOutput,
 * setInputMessages, setOutputMessages) under each hide flag, checked against every exported attribute
 * and event of the span. Python hides input.value/raw.input under hide_inputs or hide_input_messages, and
 * output.value/raw.output under hide_outputs or hide_output_messages; message lists are dropped under the
 * matching flags.
 */
class FITracerContentFlagsTest {
    @RegisterExtension
    static final OpenTelemetryExtension otel = OpenTelemetryExtension.create();

    static final String IN = "SENTINEL-INPUT-7f3a9c";
    static final String OUT = "SENTINEL-OUTPUT-7f3a9c";

    private static String everything(SpanData data) {
        StringBuilder all = new StringBuilder(data.getAttributes().toString());
        for (EventData event : data.getEvents()) all.append(event.getName()).append(event.getAttributes());
        return all.toString();
    }

    private static SpanData run(TraceConfig config, Consumer<Span> body) {
        otel.clearSpans();
        FITracer tracer = new FITracer(otel.getOpenTelemetry().getTracer("flags"), config);
        Span span = tracer.startSpan("flags", FISpanKind.LLM);
        body.accept(span);
        span.end();
        return otel.getSpans().get(0);
    }

    private static SpanData runSetters(TraceConfig config) {
        FITracer tracer = new FITracer(otel.getOpenTelemetry().getTracer("flags"), config);
        return run(config, span -> {
            tracer.setInputValue(span, IN + "-value");
            tracer.setRawInput(span, Map.of("prompt", IN + "-raw"));
            tracer.setInputMessages(span, List.of(FITracer.message("user", IN + "-message")));
            tracer.setOutputValue(span, OUT + "-value");
            tracer.setRawOutput(span, Map.of("text", OUT + "-raw"));
            tracer.setOutputMessages(span, List.of(FITracer.message("assistant", OUT + "-message")));
        });
    }

    @Test
    void visibleByDefaultSoTheSentinelsAreReal() {
        String all = everything(runSetters(TraceConfig.builder().build()));
        assertThat(all).contains(IN + "-value", IN + "-raw", IN + "-message", OUT + "-value", OUT + "-raw", OUT + "-message");
    }

    @Test
    void hideInputsKeepsEveryInputSentinelOut() {
        String all = everything(runSetters(TraceConfig.builder().hideInputs(true).build()));
        assertThat(all).doesNotContain(IN).contains(OUT + "-value");
    }

    @Test
    void hideOutputsKeepsEveryOutputSentinelOut() {
        String all = everything(runSetters(TraceConfig.builder().hideOutputs(true).build()));
        assertThat(all).doesNotContain(OUT).contains(IN + "-value");
    }

    @Test
    void hideInputMessagesHidesInputValueRawInputAndMessages() {
        String all = everything(runSetters(TraceConfig.builder().hideInputMessages(true).build()));
        assertThat(all).doesNotContain(IN).contains(OUT + "-value");
    }

    @Test
    void hideOutputMessagesHidesOutputValueRawOutputAndMessages() {
        String all = everything(runSetters(TraceConfig.builder().hideOutputMessages(true).build()));
        assertThat(all).doesNotContain(OUT).contains(IN + "-value");
    }

    @Test
    void allFlagsLeaveNoContentAtAll() {
        TraceConfig everythingHidden = TraceConfig.builder().hideInputs(true).hideOutputs(true)
            .hideInputMessages(true).hideOutputMessages(true).build();
        assertThat(everything(runSetters(everythingHidden))).doesNotContain("SENTINEL");
    }

    @Test
    void setErrorHonoursHideErrorDetails() {
        TraceConfig config = TraceConfig.builder().hideErrorDetails(true).build();
        FITracer tracer = new FITracer(otel.getOpenTelemetry().getTracer("flags"), config);
        SpanData data = run(config, span -> tracer.setError(span, new IllegalStateException("SENTINEL-ERROR-7f3a9c")));
        assertThat(everything(data)).doesNotContain("SENTINEL-ERROR");
        assertThat(data.getStatus().getDescription()).doesNotContain("SENTINEL-ERROR");
    }

    /**
     * A span from startSpan is the plain OpenTelemetry span, so a content key set on it directly is not
     * masked by key (only the tracer setters mask). This documents the boundary: instrumentation must go
     * through FITracer. It is true at the pre-fix base and at HEAD.
     */
    @Test
    void directSpanSetAttributeBypassesTheTracerByDesign() {
        TraceConfig config = TraceConfig.builder().hideInputs(true).hideOutputs(true).build();
        SpanData data = run(config, span -> {
            span.setAttribute("input.value", IN + "-direct");
            span.setAttribute("output.value", OUT + "-direct");
        });
        assertThat(everything(data)).contains(IN + "-direct", OUT + "-direct");
    }
}
