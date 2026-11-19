package ai.traceai.auto;

import io.opentelemetry.sdk.testing.exporter.InMemorySpanExporter;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.data.SpanData;
import io.opentelemetry.sdk.trace.export.SimpleSpanProcessor;
import java.util.*;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;
import org.junit.jupiter.api.Test;
import static org.junit.jupiter.api.Assertions.*;

/**
 * An application span's raw request and response (raw.input, raw.output and the Java aliases fi.raw_input,
 * fi.raw_output, and keys below them) are not exported while any hide switch is on, including the agent-only
 * text, image and invocation-parameter switches, the same rule the core SDK applies to its four hide flags.
 */
class RawValuesAgentSwitchesTest {
    static final List<String> RAW = List.of(
        "raw.input", "fi.raw_input", "raw.output", "fi.raw_output", "raw.input.body", "raw.output.body");

    /** Exports one application span carrying every raw key with its own sentinel; returns the keys whose value was exported. */
    private static List<String> exported(Map<String, Object> setting) {
        var memory = InMemorySpanExporter.create();
        try (var provider = SdkTracerProvider.builder().addSpanProcessor(SimpleSpanProcessor.create(memory)).build()) {
            var span = provider.get("application-wrapper").spanBuilder("call").startSpan();
            for (int i = 0; i < RAW.size(); i++) span.setAttribute(RAW.get(i), "SENTINEL-RAW-" + i + "-END");
            span.setAttribute("llm.model_name", "visible-model");
            span.end();
            SpanData data = memory.getFinishedSpanItems().get(0);
            String json = WireCore.dump(new OtlpJsonExporter(AutoConfig.resolve(setting, Map.of())).serialize(data));
            assertTrue(json.contains("visible-model"), "non-content attributes stay visible");
            List<String> found = new ArrayList<>();
            for (int i = 0; i < RAW.size(); i++) if (json.contains("SENTINEL-RAW-" + i + "-END")) found.add(RAW.get(i));
            return found;
        }
    }

    @Test void rawValuesAreExportedWithoutAHideSwitch() {
        assertEquals(RAW, exported(Map.of()));
    }

    @ParameterizedTest
    @ValueSource(strings = {"hide_input_text", "hide_output_text", "hide_input_images", "hide_llm_invocation_parameters",
        "hide_inputs", "hide_outputs", "hide_input_messages", "hide_output_messages"})
    void rawValuesAreNotExportedUnderAnyHideSwitch(String flag) {
        assertEquals(List.of(), exported(Map.of(flag, true)), flag + " exported these raw keys");
    }
}
