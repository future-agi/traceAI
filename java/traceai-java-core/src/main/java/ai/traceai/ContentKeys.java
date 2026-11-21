package ai.traceai;

import java.util.List;

/**
 * The span attribute keys that carry prompt, response or tool content, and so are masked by the
 * hide settings. The reference is the Python SDK's {@code TraceConfig}
 * ({@code python/fi_instrumentation/instrumentation/config.py}); this class lists the same content keys,
 * plus the legacy and alias names the Java SDK writes itself, for the four hide flags and the error-detail
 * flag Java has. It is not full equivalence: see "Differences from Python" below. Model names, ids, counts
 * and token usage are deliberately not here; a whole-number document count on a reranker document key is
 * kept as well (see {@link #DOCUMENT_COUNT_KEYS}).
 *
 * <p>Each entry matches the key itself and everything below it ({@code key.*}).
 *
 * <pre>
 * Python list (input side)                        Java before   Java now
 * input.value                                     setter only   yes
 * raw.input                                       no            yes (also the Java alias fi.raw_input)
 * llm.prompt_template.variables                   no            yes
 * gen_ai.prompt.template.variables                no            yes
 * gen_ai.system_instructions                      no            yes
 * gen_ai.request.parameters                       no            yes
 * gen_ai.request.stop_sequences                   no            yes
 * gen_ai.agent.description                        no            yes
 * gen_ai.image.prompt / negative_prompt           no            yes
 * gen_ai.input.images                             no            yes
 * gen_ai.prompts                                  no            yes
 * gen_ai.tool.definitions / description           no            yes
 * gen_ai.tool.call.arguments                      no            yes
 * gen_ai.tool.parameters                          no            yes (also the Java alias tool.parameters)
 * tool_call.function.arguments                    no            yes
 * message.function_call_arguments_json            no            yes
 * gen_ai.computer_use.text / element_selector     no            yes
 * tool.json_schema                                no            yes
 * gen_ai.retrieval.query                          no            yes
 * gen_ai.reranker.query / input_documents         no            yes
 * reranker.query / input_documents                no            yes
 *
 * Python list (output side)
 * output.value                                    setter only   yes
 * raw.output                                      no            yes (also the Java alias fi.raw_output)
 * gen_ai.tool.call.result                         no            yes (also the Java alias tool.result)
 * gen_ai.image.revised_prompt / output_urls       no            yes
 * gen_ai.guardrail.modified_output                no            yes
 * gen_ai.evaluation.explanation                   no            yes
 * gen_ai.computer_use.result                      no            yes
 * retrieval.documents, gen_ai.retrieval.documents no            yes
 * gen_ai.reranker.output_documents                no            yes
 * reranker.output_documents                       no            yes
 *
 * Python list (either direction: hidden by hideInputs or hideOutputs)
 * gen_ai.tool.call (the aggregate, exact key)     no            yes
 * gen_ai.voice.transcript, gen_ai.voice.recording.{url,stereo_url,customer_url,assistant_url},
 * gen_ai.audio.url, gen_ai.audio.transcript,
 * gen_ai.computer_use.screenshot, gen_ai.computer_use.current_url
 *                                                 no            yes
 * </pre>
 *
 * The raw request and response keys (raw.input, raw.output and the Java aliases fi.raw_input, fi.raw_output) are
 * redacted when any of the four hide flags is set.
 *
 * Message flags (Python: private_input = hide_inputs or hide_input_messages; private_output likewise).
 * The value keys input.value, raw.input, fi.raw_input and both template-variable keys are redacted when
 * hideInputs or hideInputMessages is set; output.value, raw.output and fi.raw_output when hideOutputs
 * or hideOutputMessages is set. Every gen_ai.input.messages / gen_ai.output.messages key is dropped under
 * the matching flag. Embedding text (embedding.embeddings.N.embedding.text and the Java alias
 * embedding.input_text[.N]) is redacted under hideInputs or hideInputMessages.
 *
 * Differences from Python, stated so the table above is not read as exact parity:
 * <ul>
 * <li>Python's separate text, image, embedding-vector and invocation-parameter hide flags
 *     ({@code hide_input_text}, {@code hide_input_images}, ...) do not exist in the Java configuration.</li>
 * <li>Python anchors the prefix match at the start of the key. Java additionally redacts
 *     {@code tool_call.function.arguments} and {@code message.function_call_arguments_json} when they appear
 *     as a whole dotted segment deeper in a key (for example
 *     {@code message.tool_calls.0.tool_call.function.arguments}), under any of the four hide flags, because
 *     the Java instrumentations write tool-call arguments that way.</li>
 * <li>Java has no counterpart of Python's list-valued message flattening or ChatInput handling.</li>
 * <li>Error details: under hideErrorDetails the keys error.message, exception.message, exception.stacktrace and
 *     the {@code .error.message}, {@code .exception.message}, {@code .exception.stacktrace} and {@code .error_message}
 *     suffixes are dropped, matching the C# SDK's list; Python's own error-key helper may differ.</li>
 * </ul>
 */
public final class ContentKeys {
    private ContentKeys() { }

    /** Placeholder written in place of a masked value, the same string the Python and C# SDKs use. */
    public static final String REDACTED = "__REDACTED__";

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
        "gen_ai.voice.transcript", "gen_ai.voice.recording.url", "gen_ai.voice.recording.stereo_url",
        "gen_ai.voice.recording.customer_url", "gen_ai.voice.recording.assistant_url",
        "gen_ai.audio.url", "gen_ai.audio.transcript", "gen_ai.computer_use.screenshot",
        "gen_ai.computer_use.current_url");

    /**
     * The reranker document keys on which the Java instrumentations write a document count (Cohere rerank: how
     * many documents went in and came out). A whole-number value on exactly one of these keys is a count, not
     * content, and is kept under every hide setting. Text or any other value on them, and every key below them
     * ({@code gen_ai.reranker.input_documents.0.document.content}), is masked as listed above.
     */
    static final List<String> DOCUMENT_COUNT_KEYS = List.of(
        "gen_ai.reranker.input_documents", "gen_ai.reranker.output_documents",
        "reranker.input_documents", "reranker.output_documents");

    /** True when the value is a document count on one of {@link #DOCUMENT_COUNT_KEYS}. */
    static boolean isDocumentCount(String key, Object value) {
        return (value instanceof Long || value instanceof Integer || value instanceof Short || value instanceof Byte)
            && DOCUMENT_COUNT_KEYS.contains(key);
    }

    private static final String AGGREGATE_TOOL_CALL = "gen_ai.tool.call";
    private static final String INPUT_MIME_TYPE = "input.mime_type";
    private static final String OUTPUT_MIME_TYPE = "output.mime_type";
    private static final String INPUT_MESSAGES = "gen_ai.input.messages";
    private static final String OUTPUT_MESSAGES = "gen_ai.output.messages";

    private static boolean under(String key, List<String> prefixes) {
        for (String prefix : prefixes) {
            if (key.equals(prefix) || key.startsWith(prefix + ".")) return true;
        }
        return false;
    }

    /** What to do with an attribute: keep it, replace its value, or drop it. */
    enum Action { KEEP, REDACT, OMIT }

    /** The keys Python hides under hide_inputs or hide_input_messages (private_input). */
    private static final List<String> INPUT_VALUES = List.of(
        "input.value", "raw.input", "fi.raw_input",
        "llm.prompt_template.variables", "gen_ai.prompt.template.variables");
    /** The raw request and response objects: redacted under every hide setting, input and output alike. */
    private static final List<String> RAW_VALUES = List.of("raw.input", "fi.raw_input", "raw.output", "fi.raw_output");
    /** The keys Python hides under hide_outputs or hide_output_messages (private_output). */
    private static final List<String> OUTPUT_VALUES = List.of("output.value", "raw.output", "fi.raw_output");
    /** Embedding text: Python's embedding.embeddings.N.embedding.text, and the Java alias embedding.input_text[.N]. */
    private static final String EMBEDDINGS = "embedding.embeddings";
    private static final String EMBEDDING_TEXT = "embedding.text";
    private static final String EMBEDDING_INPUT_TEXT = "embedding.input_text";
    /**
     * Tool-call argument fields that SDKs nest below a message or tool-call index, for example
     * {@code message.tool_calls.0.tool_call.function.arguments}. Python anchors its prefix match at the
     * start of the key and so would miss these; Java matches the field wherever it appears as a whole
     * dotted segment, so {@code tool_call.function.arguments_count} is not a match.
     */
    private static final List<String> NESTED_TOOL_ARGUMENTS = List.of(
        "tool_call.function.arguments", "message.function_call_arguments_json");

    private static boolean hasSegment(String key, String field) {
        int at = key.indexOf(field);
        while (at >= 0) {
            boolean starts = at > 0 && key.charAt(at - 1) == '.'; // nested only; a leading field is in INPUT
            int end = at + field.length();
            boolean ends = end == key.length() || key.charAt(end) == '.';
            if (starts && ends) return true;
            at = key.indexOf(field, at + 1);
        }
        return false;
    }

    private static boolean isEmbeddingText(String key) {
        if (key.equals(EMBEDDING_INPUT_TEXT) || key.startsWith(EMBEDDING_INPUT_TEXT + ".")) return true;
        return key.startsWith(EMBEDDINGS + ".") && key.endsWith("." + EMBEDDING_TEXT);
    }

    private static boolean isErrorDetail(String key) {
        return key.equals("error.message") || key.equals("exception.message") || key.equals("exception.stacktrace")
            || key.endsWith(".error.message") || key.endsWith(".exception.message")
            || key.endsWith(".exception.stacktrace") || key.endsWith(".error_message");
    }

    static Action actionFor(String key, TraceConfig config) {
        if (config.isHideErrorDetails() && isErrorDetail(key)) return Action.OMIT;
        boolean inputs = config.isHideInputs();
        boolean outputs = config.isHideOutputs();
        boolean privateInput = inputs || config.isHideInputMessages();
        boolean privateOutput = outputs || config.isHideOutputMessages();
        if (inputs && key.equals(INPUT_MIME_TYPE)) return Action.OMIT;
        if (outputs && key.equals(OUTPUT_MIME_TYPE)) return Action.OMIT;
        if ((inputs || outputs || privateInput || privateOutput) && under(key, RAW_VALUES)) return Action.REDACT;
        if (privateInput && under(key, INPUT_VALUES)) return Action.REDACT;
        if (privateOutput && under(key, OUTPUT_VALUES)) return Action.REDACT;
        if ((inputs || outputs) && (key.equals(AGGREGATE_TOOL_CALL) || under(key, EITHER))) return Action.REDACT;
        if (inputs && under(key, INPUT)) return Action.REDACT;
        if (outputs && under(key, OUTPUT)) return Action.REDACT;
        if (privateInput && isEmbeddingText(key)) return Action.REDACT;
        if (privateInput && key.contains(INPUT_MESSAGES)) return Action.OMIT;
        if (privateOutput && key.contains(OUTPUT_MESSAGES)) return Action.OMIT;
        if ((privateInput || privateOutput) && nestedToolArguments(key)) return Action.REDACT;
        return Action.KEEP;
    }

    private static boolean nestedToolArguments(String key) {
        for (String field : NESTED_TOOL_ARGUMENTS) {
            if (hasSegment(key, field)) return true;
        }
        return false;
    }
}
