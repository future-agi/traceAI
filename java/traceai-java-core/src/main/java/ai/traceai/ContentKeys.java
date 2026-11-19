package ai.traceai;

import java.util.List;

/**
 * The span attribute keys that carry prompt, response or tool content, and so are masked by the
 * hide settings. The reference is the Python SDK's {@code TraceConfig}
 * ({@code python/fi_instrumentation/instrumentation/config.py}); this class lists the same keys,
 * plus the legacy and alias names the Java SDK writes itself, so a key masked in one SDK is masked
 * in the other. Model names, ids, counts and token usage are deliberately not here.
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
 * Not carried over: Python's separate text, image, embedding-vector and invocation-parameter hide
 * flags, which the Java configuration does not have. Python also redacts input.value, raw.input and
 * the template variables under its input-message flag; Java keeps the message flags to the message
 * keys, as {@link FITracer#setInputMessages} always has.
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

    static Action actionFor(String key, TraceConfig config) {
        boolean inputs = config.isHideInputs();
        boolean outputs = config.isHideOutputs();
        if (inputs && key.equals(INPUT_MIME_TYPE)) return Action.OMIT;
        if (outputs && key.equals(OUTPUT_MIME_TYPE)) return Action.OMIT;
        if ((inputs || outputs) && (key.equals(AGGREGATE_TOOL_CALL) || under(key, EITHER))) return Action.REDACT;
        if (inputs && under(key, INPUT)) return Action.REDACT;
        if (outputs && under(key, OUTPUT)) return Action.REDACT;
        if ((inputs || config.isHideInputMessages()) && key.contains(INPUT_MESSAGES)) return Action.OMIT;
        if ((outputs || config.isHideOutputMessages()) && key.contains(OUTPUT_MESSAGES)) return Action.OMIT;
        return Action.KEEP;
    }
}
