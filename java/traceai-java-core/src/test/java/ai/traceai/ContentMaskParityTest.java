package ai.traceai;

import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.sdk.testing.junit5.OpenTelemetryExtension;
import io.opentelemetry.sdk.trace.data.SpanData;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.RegisterExtension;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.MethodSource;

import java.util.List;
import java.util.stream.Stream;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * Every content key the Python SDK's TraceConfig masks (python/fi_instrumentation/instrumentation/
 * config.py: _INPUT_CONTENT_PREFIXES, _OUTPUT_CONTENT_PREFIXES, _MIXED_CONTENT_PREFIXES and the
 * input/output value keys), plus the aliases the Java SDK writes, set through the SDK's own tracer.
 */
class ContentMaskParityTest {
    @RegisterExtension
    static final OpenTelemetryExtension otel = OpenTelemetryExtension.create();

    static final String SECRET = "private-content-DO-NOT-EXPORT";
    static final String REDACTED = "__REDACTED__";

    static final List<String> INPUT_KEYS = List.of(
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
    static final List<String> OUTPUT_KEYS = List.of(
        "output.value", "raw.output", "fi.raw_output",
        "gen_ai.tool.call.result", "tool.result", "gen_ai.image.revised_prompt", "gen_ai.image.output_urls",
        "gen_ai.guardrail.modified_output", "gen_ai.evaluation.explanation", "gen_ai.computer_use.result",
        "retrieval.documents", "gen_ai.retrieval.documents", "gen_ai.reranker.output_documents",
        "reranker.output_documents");
    static final List<String> EITHER_KEYS = List.of(
        "gen_ai.tool.call",
        "gen_ai.voice.transcript", "gen_ai.voice.recording.url", "gen_ai.voice.recording.stereo_url",
        "gen_ai.voice.recording.customer_url", "gen_ai.voice.recording.assistant_url",
        "gen_ai.audio.url", "gen_ai.audio.transcript", "gen_ai.computer_use.screenshot",
        "gen_ai.computer_use.current_url");
    /** Identifiers, names and counts: never masked, whatever is hidden. */
    static final List<String> CONTROL_KEYS = List.of(
        "gen_ai.request.model", "gen_ai.tool.name", "gen_ai.tool.call.id", "gen_ai.agent.name",
        "gen_ai.prompt.template.name", "gen_ai.usage.input_tokens", "tool.name", "tool_call.id",
        "gen_ai.retrieval.top_k", "gen_ai.response.id");

    static Stream<String> inputKeys() { return INPUT_KEYS.stream(); }
    static Stream<String> outputKeys() { return OUTPUT_KEYS.stream(); }
    static Stream<String> eitherKeys() { return EITHER_KEYS.stream(); }

    private static SpanData export(TraceConfig config, String key) {
        otel.clearSpans();
        FITracer tracer = new FITracer(otel.getOpenTelemetry().getTracer("parity"), config);
        Span span = tracer.startSpan("parity", FISpanKind.LLM);
        tracer.setAttribute(span, key, SECRET);
        span.end();
        return otel.getSpans().get(0);
    }

    private static String stored(SpanData data, String key) {
        return data.getAttributes().get(AttributeKey.stringKey(key));
    }

    private static TraceConfig hiding(boolean inputs, boolean outputs) {
        return TraceConfig.builder().hideInputs(inputs).hideOutputs(outputs).build();
    }

    @ParameterizedTest(name = "hideInputs masks {0}")
    @MethodSource("inputKeys")
    void hideInputsMasksEveryInputContentKey(String key) {
        assertThat(stored(export(hiding(true, false), key), key)).isEqualTo(REDACTED);
        assertThat(stored(export(hiding(false, true), key), key)).isEqualTo(SECRET);
        assertThat(stored(export(hiding(false, false), key), key)).isEqualTo(SECRET);
    }

    @ParameterizedTest(name = "hideOutputs masks {0}")
    @MethodSource("outputKeys")
    void hideOutputsMasksEveryOutputContentKey(String key) {
        assertThat(stored(export(hiding(false, true), key), key)).isEqualTo(REDACTED);
        assertThat(stored(export(hiding(true, false), key), key)).isEqualTo(SECRET);
        assertThat(stored(export(hiding(false, false), key), key)).isEqualTo(SECRET);
    }

    @ParameterizedTest(name = "either flag masks {0}")
    @MethodSource("eitherKeys")
    void eitherFlagMasksTheKeysThatCanHoldBothDirections(String key) {
        assertThat(stored(export(hiding(true, false), key), key)).isEqualTo(REDACTED);
        assertThat(stored(export(hiding(false, true), key), key)).isEqualTo(REDACTED);
        assertThat(stored(export(hiding(false, false), key), key)).isEqualTo(SECRET);
    }

    @ParameterizedTest(name = "descendants of {0} are masked too")
    @MethodSource("inputKeys")
    void descendantsOfAContentKeyAreMasked(String key) {
        String child = key + ".0.value";
        assertThat(stored(export(hiding(true, false), child), child)).isEqualTo(REDACTED);
    }

    @Test
    void controlKeysAreNeverMasked() {
        TraceConfig everything = TraceConfig.builder().hideInputs(true).hideOutputs(true)
            .hideInputMessages(true).hideOutputMessages(true).build();
        for (String key : CONTROL_KEYS) {
            assertThat(stored(export(everything, key), key)).as(key).isEqualTo(SECRET);
        }
    }

    @Test
    void similarlyNamedKeysAreNotMasked() {
        for (String key : List.of("gen_ai.tool.call.id", "gen_ai.tool.calls", "input.valued", "tool.parameters_count",
                                  "gen_ai.prompt.template.name", "gen_ai.request.model")) {
            assertThat(stored(export(hiding(true, true), key), key)).as(key).isEqualTo(SECRET);
        }
    }

    @Test
    void mimeTypesAreDroppedWithTheirContent() {
        assertThat(stored(export(hiding(true, false), "input.mime_type"), "input.mime_type")).isNull();
        assertThat(stored(export(hiding(false, true), "output.mime_type"), "output.mime_type")).isNull();
        assertThat(stored(export(hiding(true, false), "output.mime_type"), "output.mime_type")).isEqualTo(SECRET);
    }

    @Test
    void messageKeysAreDroppedByTheirFlags() {
        String in = "gen_ai.input.messages.0.message.content";
        String out = "gen_ai.output.messages.0.message.content";
        assertThat(stored(export(TraceConfig.builder().hideInputMessages(true).build(), in), in)).isNull();
        assertThat(stored(export(TraceConfig.builder().hideInputMessages(true).build(), out), out)).isEqualTo(SECRET);
        assertThat(stored(export(TraceConfig.builder().hideOutputMessages(true).build(), out), out)).isNull();
        assertThat(stored(export(TraceConfig.builder().hideOutputMessages(true).build(), in), in)).isEqualTo(SECRET);
        assertThat(stored(export(hiding(true, false), in), in)).isNull();
        assertThat(stored(export(hiding(false, true), out), out)).isNull();
    }

    @Test
    void stringArraysAreMaskedWithTheSinglePlaceholder() {
        otel.clearSpans();
        FITracer tracer = new FITracer(otel.getOpenTelemetry().getTracer("parity"), hiding(true, false));
        Span span = tracer.startSpan("parity", FISpanKind.LLM);
        tracer.setAttribute(span, "gen_ai.request.stop_sequences", List.of("END", SECRET));
        tracer.setAttribute(span, "gen_ai.response.finish_reasons", List.of("stop"));
        span.end();
        SpanData data = otel.getSpans().get(0);
        assertThat(stored(data, "gen_ai.request.stop_sequences")).isEqualTo(REDACTED);
        assertThat(data.getAttributes().get(AttributeKey.stringArrayKey("gen_ai.response.finish_reasons")))
            .containsExactly("stop");
    }

    /** Python: private_input = hide_inputs or hide_input_messages (or hide_input_text, absent in Java). */
    static final List<String> INPUT_VALUE_KEYS = List.of(
        "input.value", "raw.input", "fi.raw_input",
        "llm.prompt_template.variables", "gen_ai.prompt.template.variables");
    static final List<String> OUTPUT_VALUE_KEYS = List.of("output.value", "raw.output", "fi.raw_output");

    @ParameterizedTest(name = "hideInputMessages masks {0}")
    @MethodSource("inputValueKeys")
    void hideInputMessagesMasksTheInputValueKeys(String key) {
        TraceConfig messages = TraceConfig.builder().hideInputMessages(true).build();
        assertThat(stored(export(messages, key), key)).isEqualTo(REDACTED);
        assertThat(stored(export(messages, key + ".0.value"), key + ".0.value")).isEqualTo(REDACTED);
        assertThat(stored(export(TraceConfig.builder().hideOutputMessages(true).build(), key), key)).isEqualTo(SECRET);
    }

    @ParameterizedTest(name = "hideOutputMessages masks {0}")
    @MethodSource("outputValueKeys")
    void hideOutputMessagesMasksTheOutputValueKeys(String key) {
        TraceConfig messages = TraceConfig.builder().hideOutputMessages(true).build();
        assertThat(stored(export(messages, key), key)).isEqualTo(REDACTED);
        assertThat(stored(export(TraceConfig.builder().hideInputMessages(true).build(), key), key)).isEqualTo(SECRET);
    }

    static Stream<String> inputValueKeys() { return INPUT_VALUE_KEYS.stream(); }
    static Stream<String> outputValueKeys() { return OUTPUT_VALUE_KEYS.stream(); }

    @Test
    void theTracerSettersHonourTheMessageFlags() {
        otel.clearSpans();
        FITracer tracer = new FITracer(otel.getOpenTelemetry().getTracer("parity"),
            TraceConfig.builder().hideInputMessages(true).hideOutputMessages(true).build());
        Span span = tracer.startSpan("parity", FISpanKind.LLM);
        tracer.setInputValue(span, SECRET);
        tracer.setOutputValue(span, SECRET);
        tracer.setRawInput(span, SECRET);
        tracer.setRawOutput(span, SECRET);
        span.end();
        SpanData data = otel.getSpans().get(0);
        for (String key : List.of("input.value", "output.value", "raw.input", "raw.output")) {
            assertThat(data.getAttributes().get(AttributeKey.stringKey(key))).as(key)
                .satisfiesAnyOf(v -> assertThat(v).isNull(), v -> assertThat(v).isEqualTo(REDACTED));
        }
    }

    @Test
    void embeddingTextIsInputContentUnderHideInputsAndHideInputMessages() {
        for (String key : List.of("embedding.embeddings.0.embedding.text", "embedding.embeddings.12.embedding.text",
                                  "embedding.input_text", "embedding.input_text.0", "embedding.input_text.3")) {
            assertThat(stored(export(hiding(true, false), key), key)).as(key).isEqualTo(REDACTED);
            assertThat(stored(export(TraceConfig.builder().hideInputMessages(true).build(), key), key))
                .as(key).isEqualTo(REDACTED);
            assertThat(stored(export(hiding(false, true), key), key)).as(key).isEqualTo(SECRET);
            assertThat(stored(export(hiding(false, false), key), key)).as(key).isEqualTo(SECRET);
        }
        for (String key : List.of("embedding.embeddings.0.embedding.vector", "embedding.model_name",
                                  "embedding.vector_count", "embedding.input_texts_count")) {
            assertThat(stored(export(hiding(true, true), key), key)).as(key).isEqualTo(SECRET);
        }
    }

    @Test
    void toolCallArgumentsNestedInAMessageAreMasked() {
        // The shape LangChain4j writes: message.tool_calls.N.tool_call.function.arguments, with no
        // gen_ai.*.messages prefix, so an anchored prefix match misses it.
        for (String key : List.of("message.tool_calls.0.tool_call.function.arguments",
                                  "message.tool_calls.2.tool_call.function.arguments",
                                  "llm.output_messages.0.message.tool_calls.1.tool_call.function.arguments",
                                  "message.tool_calls.0.message.function_call_arguments_json")) {
            assertThat(stored(export(hiding(true, false), key), key)).as(key).isEqualTo(REDACTED);
            assertThat(stored(export(hiding(false, true), key), key)).as(key).isEqualTo(REDACTED);
            assertThat(stored(export(hiding(false, false), key), key)).as(key).isEqualTo(SECRET);
        }
        for (String key : List.of("message.tool_calls.0.tool_call.function.name", "message.tool_calls.0.tool_call.id",
                                  "message.tool_calls.0.tool_call.function.arguments_count")) {
            assertThat(stored(export(hiding(true, true), key), key)).as(key).isEqualTo(SECRET);
        }
    }
}
