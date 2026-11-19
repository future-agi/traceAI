package ai.traceai.auto;

import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.sdk.testing.exporter.InMemorySpanExporter;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.export.SimpleSpanProcessor;
import java.util.*;
import java.util.function.Consumer;
import org.junit.jupiter.api.Test;
import static org.junit.jupiter.api.Assertions.*;

/**
 * The hide settings apply to a content key whatever the type of its value: long, double and boolean values
 * written by an application span are masked like string values, and keys outside the content prefixes
 * (counts, flags) stay visible.
 */
class TypedContentValuesHideFlagsTest {
    private static final List<String> FLAGS = List.of("hide_inputs", "hide_outputs", "hide_input_messages", "hide_output_messages");

    /** Exports one span written by the given scope and returns the OTLP JSON. */
    private static String export(Map<String, Object> setting, String scope, Consumer<Span> writer) throws Exception {
        var memory = InMemorySpanExporter.create();
        try (var provider = SdkTracerProvider.builder().addSpanProcessor(SimpleSpanProcessor.create(memory)).build()) {
            Span span = provider.get(scope).spanBuilder("call").startSpan();
            writer.accept(span);
            span.end();
            return WireCore.dump(new OtlpJsonExporter(AutoConfig.resolve(setting, Map.of())).serialize(memory.getFinishedSpanItems().get(0)));
        }
    }
    private static String exportApp(Map<String, Object> setting, Consumer<Span> writer) throws Exception { return export(setting, "application-wrapper", writer); }

    private static void typedContent(Span span) {
        span.setAttribute("tool.result", 4242001L);
        span.setAttribute("llm.prompt_template.variables.age", 4243001L);
        span.setAttribute("tool.parameters.count", 4244001L);
        span.setAttribute("raw.input", 4245001L);
        span.setAttribute("raw.output", 4246001L);
        span.setAttribute("retrieval.documents.0.document.score", 0.4247001);
    }
    private static final String BOOLEAN_KEY = "gen_ai.computer_use.result";

    @Test void controlSpanExportsTypedValuesWithoutAHideFlag() throws Exception {
        String json = exportApp(Map.of(), TypedContentValuesHideFlagsTest::typedContent);
        for (String sentinel : List.of("4242001", "4243001", "4244001", "4245001", "4246001", "0.4247001")) assertTrue(json.contains(sentinel), sentinel);
        assertTrue(exportApp(Map.of(), s -> s.setAttribute(BOOLEAN_KEY, true)).contains("\"boolValue\":true"));
    }
    @Test void hideInputsMasksTypedInputValues() throws Exception {
        String json = exportApp(Map.of("hide_inputs", true), TypedContentValuesHideFlagsTest::typedContent);
        for (String sentinel : List.of("4243001", "4244001", "4245001")) assertFalse(json.contains(sentinel), sentinel);
    }
    @Test void hideInputMessagesMasksTypedInputValues() throws Exception {
        String json = exportApp(Map.of("hide_input_messages", true), TypedContentValuesHideFlagsTest::typedContent);
        for (String sentinel : List.of("4243001", "4245001")) assertFalse(json.contains(sentinel), sentinel);
    }
    @Test void hideOutputsMasksTypedOutputValues() throws Exception {
        String json = exportApp(Map.of("hide_outputs", true), TypedContentValuesHideFlagsTest::typedContent);
        for (String sentinel : List.of("4242001", "4246001", "0.4247001")) assertFalse(json.contains(sentinel), sentinel);
        assertFalse(exportApp(Map.of("hide_outputs", true), s -> s.setAttribute(BOOLEAN_KEY, true)).contains("\"boolValue\":true"));
    }
    @Test void hideOutputMessagesMasksTypedOutputValues() throws Exception {
        String json = exportApp(Map.of("hide_output_messages", true), TypedContentValuesHideFlagsTest::typedContent);
        assertFalse(json.contains("4246001"));
    }
    @Test void countKeysOutsideTheContentPrefixesStayVisibleUnderEveryFlag() throws Exception {
        for (String flag : FLAGS) {
            String json = exportApp(Map.of(flag, true), s -> { s.setAttribute("llm.token_count.prompt", 4248001L); s.setAttribute("llm.token_count.total", 4249001L); });
            assertTrue(json.contains("4248001") && json.contains("4249001"), flag);
        }
    }
    @Test void transcriptOfAnApplicationSpanFollowsTheEitherDirectionRule() throws Exception {
        for (String flag : List.of("hide_inputs", "hide_outputs")) {
            String json = exportApp(Map.of(flag, true), s -> { s.setAttribute("gen_ai.operation.name", "text_to_speech"); s.setAttribute("gen_ai.audio.transcript", "SENTINEL-TRANSCRIPT"); });
            assertFalse(json.contains("SENTINEL-TRANSCRIPT"), flag);
        }
    }
    @Test void transcriptOfTheAgentsOwnCallStaysDirectionAware() throws Exception {
        Consumer<Span> speech = s -> { s.setAttribute("gen_ai.operation.name", "text_to_speech"); s.setAttribute("gen_ai.audio.transcript", "SENTINEL-TRANSCRIPT"); };
        assertFalse(export(Map.of("hide_inputs", true), TraceAIAuto.SCOPE, speech).contains("SENTINEL-TRANSCRIPT"));
        assertTrue(export(Map.of("hide_outputs", true), TraceAIAuto.SCOPE, speech).contains("SENTINEL-TRANSCRIPT"));
    }

    private static void textKeys(Span span) {
        span.setAttribute("raw.input", "SENTINEL-RAW-IN");
        span.setAttribute("raw.output", "SENTINEL-RAW-OUT");
        span.setAttribute("llm.prompt_template.variables", "SENTINEL-TEMPLATE-VARS");
        span.setAttribute("llm.prompt_template.variables.age", "SENTINEL-TEMPLATE-VAR");
        span.setAttribute("embedding.embeddings.0.embedding.text", "SENTINEL-EMBED-TEXT");
    }
    @Test void inputTextSwitchMasksRawInputTemplateVariablesAndEmbeddingText() throws Exception {
        String json = exportApp(Map.of("hide_input_text", true), TypedContentValuesHideFlagsTest::textKeys);
        for (String sentinel : List.of("SENTINEL-RAW-IN", "SENTINEL-TEMPLATE-VARS", "SENTINEL-TEMPLATE-VAR", "SENTINEL-EMBED-TEXT")) assertFalse(json.contains(sentinel), sentinel);
        // The raw response is withheld too: raw values are not exported while any hide switch is on.
        assertFalse(json.contains("SENTINEL-RAW-OUT"));
    }
    @Test void outputTextSwitchMasksRawOutput() throws Exception {
        String json = exportApp(Map.of("hide_output_text", true), TypedContentValuesHideFlagsTest::textKeys);
        assertFalse(json.contains("SENTINEL-RAW-OUT"));
        // The raw request is withheld too: raw values are not exported while any hide switch is on.
        assertFalse(json.contains("SENTINEL-RAW-IN"));
    }
}
