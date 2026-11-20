package ai.traceai.auto;

import io.opentelemetry.sdk.testing.exporter.InMemorySpanExporter;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.data.SpanData;
import io.opentelemetry.sdk.trace.export.SimpleSpanProcessor;
import java.util.*;
import org.junit.jupiter.api.Test;
import static org.junit.jupiter.api.Assertions.*;

/**
 * Every content key the Python SDK's TraceConfig masks (the tables in ai.traceai.ContentKeys), written by an
 * application span and exported through the agent's own masker (TraceAIAuto.hidden) and OTLP serializer, under
 * the matching FI_HIDE_* setting. Each key carries its own sentinel value; a sentinel in the OTLP JSON means the value was exported.
 */
class ContentKeyHideFlagsTest {
    static final List<String> INPUT = List.of(
        "input.value", "raw.input", "fi.raw_input",
        "llm.prompt_template.variables", "gen_ai.prompt.template.variables",
        "gen_ai.system_instructions", "gen_ai.request.parameters", "gen_ai.request.stop_sequences",
        "gen_ai.agent.description", "gen_ai.image.prompt", "gen_ai.image.negative_prompt",
        "gen_ai.input.images", "gen_ai.prompts", "gen_ai.tool.definitions", "gen_ai.tool.description",
        "gen_ai.tool.call.arguments", "gen_ai.tool.parameters", "tool.parameters",
        "tool_call.function.arguments", "message.function_call_arguments_json",
        "gen_ai.computer_use.text", "gen_ai.computer_use.element_selector", "tool.json_schema",
        "gen_ai.retrieval.query", "gen_ai.reranker.query", "gen_ai.reranker.input_documents",
        "reranker.query", "reranker.input_documents");
    static final List<String> OUTPUT = List.of(
        "output.value", "raw.output", "fi.raw_output",
        "gen_ai.tool.call.result", "tool.result", "gen_ai.image.revised_prompt", "gen_ai.image.output_urls",
        "gen_ai.guardrail.modified_output", "gen_ai.evaluation.explanation", "gen_ai.computer_use.result",
        "retrieval.documents", "gen_ai.retrieval.documents", "gen_ai.reranker.output_documents",
        "reranker.output_documents");
    static final List<String> EITHER = List.of(
        "gen_ai.tool.call",
        "gen_ai.voice.transcript", "gen_ai.voice.recording.url", "gen_ai.voice.recording.stereo_url",
        "gen_ai.voice.recording.customer_url", "gen_ai.voice.recording.assistant_url",
        "gen_ai.audio.url", "gen_ai.audio.transcript", "gen_ai.computer_use.screenshot",
        "gen_ai.computer_use.current_url");
    /** Python private_input: hide_inputs or hide_input_messages. */
    static final List<String> INPUT_VALUES = List.of(
        "input.value", "raw.input", "fi.raw_input", "llm.prompt_template.variables", "gen_ai.prompt.template.variables",
        "embedding.embeddings.0.embedding.text", "embedding.input_text", "embedding.input_text.1",
        "gen_ai.input.messages.0.message.content");
    /** Python private_output: hide_outputs or hide_output_messages. */
    static final List<String> OUTPUT_VALUES = List.of(
        "output.value", "raw.output", "fi.raw_output", "gen_ai.output.messages.0.message.content");
    static final List<String> NESTED_TOOL_ARGUMENTS = List.of(
        "message.tool_calls.0.tool_call.function.arguments", "message.tool_calls.0.message.function_call_arguments_json");

    /** Exports one span carrying every key with its own sentinel, under the setting; returns the keys whose value was exported. */
    private static List<String> exportedUnmasked(Map<String, Object> setting, List<String>... groups) throws Exception {
        List<String> keys = new ArrayList<>();
        for (List<String> group : groups) for (String key : group) if (!keys.contains(key)) keys.add(key);
        var memory = InMemorySpanExporter.create();
        try (var provider = SdkTracerProvider.builder().addSpanProcessor(SimpleSpanProcessor.create(memory)).build()) {
            var span = provider.get("application-wrapper").spanBuilder("call").startSpan();
            for (int i = 0; i < keys.size(); i++) span.setAttribute(keys.get(i), "SENTINEL-CONTENT-" + i + "-END");
            span.end();
            SpanData data = memory.getFinishedSpanItems().get(0);
            String json = WireCore.dump(new OtlpJsonExporter(AutoConfig.resolve(setting, Map.of())).serialize(data));
            List<String> unmasked = new ArrayList<>();
            for (int i = 0; i < keys.size(); i++) if (json.contains("SENTINEL-CONTENT-" + i + "-END")) unmasked.add(keys.get(i));
            return unmasked;
        }
    }

    @SafeVarargs
    private static void assertMasked(String flag, List<String>... groups) throws Exception {
        assertEquals(List.of(), exportedUnmasked(Map.of(flag, true), groups), flag + " exported these content keys unmasked");
    }

    @Test void controlSpanExportsEveryKeyWithoutAHideFlag() throws Exception {
        List<String> all = new ArrayList<>(INPUT); all.addAll(OUTPUT); all.addAll(EITHER);
        all.remove("gen_ai.request.parameters"); // withheld by default (FI_HIDE_LLM_INVOCATION_PARAMETERS is separate)
        assertEquals(new HashSet<>(all), new HashSet<>(exportedUnmasked(Map.of(), all)), "sentinels must be visible when nothing is hidden");
    }
    @Test void hideInputsMasksEveryInputAndEitherDirectionKey() throws Exception { assertMasked("hide_inputs", INPUT, EITHER, INPUT_VALUES); }
    @Test void hideOutputsMasksEveryOutputAndEitherDirectionKey() throws Exception { assertMasked("hide_outputs", OUTPUT, EITHER, OUTPUT_VALUES); }
    @Test void hideInputMessagesMasksTheInputValueKeysEmbeddingTextAndMessages() throws Exception { assertMasked("hide_input_messages", INPUT_VALUES); }
    @Test void hideOutputMessagesMasksTheOutputValueKeysAndMessages() throws Exception { assertMasked("hide_output_messages", OUTPUT_VALUES); }
    @Test void nestedToolCallArgumentsAreMaskedUnderEveryContentFlag() throws Exception {
        for (String flag : List.of("hide_inputs", "hide_outputs", "hide_input_messages", "hide_output_messages")) assertMasked(flag, NESTED_TOOL_ARGUMENTS);
    }
}
