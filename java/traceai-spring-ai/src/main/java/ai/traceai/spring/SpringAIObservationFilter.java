package ai.traceai.spring;

import ai.traceai.SemanticConventions;
import io.micrometer.common.KeyValue;
import io.micrometer.observation.Observation;
import io.micrometer.observation.ObservationFilter;
import org.springframework.ai.chat.observation.ChatModelObservationContext;
import org.springframework.ai.embedding.observation.EmbeddingModelObservationContext;
import org.springframework.ai.tool.observation.ToolCallingObservationContext;

/**
 * Enriches Spring AI's native Micrometer observations for Future AGI. It never starts a
 * span, installs a tracing handler, or creates an OpenTelemetry provider/exporter.
 * Register this filter with the same ObservationRegistry used by Spring AI and the
 * application's Micrometer OpenTelemetry bridge. Do not also wrap those models in
 * {@link TracedChatModel} or {@link TracedEmbeddingModel}.
 *
 * <p>Content is off by default. The two opt-in flags control only content added by this
 * filter, not other handlers, native content logging, or exception messages. Content
 * attributes are bounded to 32,000 characters; vectors and binary media are never copied.</p>
 */
public final class SpringAIObservationFilter implements ObservationFilter {
    static final int MAX_CONTENT_LENGTH = 32_000;
    private final boolean captureInputs;
    private final boolean captureOutputs;

    /** Creates a metadata-only adapter. */
    public SpringAIObservationFilter() {
        this(false, false);
    }

    /**
     * Creates an adapter with explicit content opt-ins.
     *
     * @param captureInputs opt in to prompt, embedding text, and tool arguments
     * @param captureOutputs opt in to completions and tool results
     */
    public SpringAIObservationFilter(boolean captureInputs, boolean captureOutputs) {
        this.captureInputs = captureInputs;
        this.captureOutputs = captureOutputs;
    }

    @Override
    public Observation.Context map(Observation.Context context) {
        if (context instanceof ChatModelObservationContext chat) {
            tag(context, SemanticConventions.FI_SPAN_KIND, "LLM");
            tag(context, SemanticConventions.LLM_PROVIDER, chat.getOperationMetadata().provider());
            if (captureInputs) {
                StringBuilder text = new StringBuilder();
                for (var message : chat.getRequest().getInstructions()) {
                    append(text, message.getText());
                    if (text.length() >= MAX_CONTENT_LENGTH) break;
                }
                tag(context, SemanticConventions.INPUT_VALUE, text.toString());
            }
            if (captureOutputs && chat.getResponse() != null) {
                StringBuilder text = new StringBuilder();
                for (var generation : chat.getResponse().getResults()) {
                    append(text, generation.getOutput().getText());
                    if (text.length() >= MAX_CONTENT_LENGTH) break;
                }
                tag(context, SemanticConventions.OUTPUT_VALUE, text.toString());
            }
        } else if (context instanceof EmbeddingModelObservationContext embedding) {
            tag(context, SemanticConventions.FI_SPAN_KIND, "EMBEDDING");
            tag(context, SemanticConventions.LLM_PROVIDER, embedding.getOperationMetadata().provider());
            if (captureInputs) {
                StringBuilder text = new StringBuilder();
                for (String input : embedding.getRequest().getInstructions()) {
                    append(text, input);
                    if (text.length() >= MAX_CONTENT_LENGTH) break;
                }
                tag(context, SemanticConventions.INPUT_VALUE, text.toString());
            }
        } else if (context instanceof ToolCallingObservationContext tool) {
            tag(context, SemanticConventions.FI_SPAN_KIND, "TOOL");
            tag(context, SemanticConventions.GEN_AI_TOOL_NAME, tool.getToolDefinition().name());
            if (captureInputs) tag(context, SemanticConventions.INPUT_VALUE, tool.getToolCallArguments());
            if (captureOutputs) tag(context, SemanticConventions.OUTPUT_VALUE, tool.getToolCallResult());
        }
        return context;
    }

    private static void append(StringBuilder text, String value) {
        if (value == null || value.isEmpty() || text.length() >= MAX_CONTENT_LENGTH) return;
        if (!text.isEmpty()) text.append('\n');
        int remaining = MAX_CONTENT_LENGTH - text.length();
        text.append(value, 0, Math.min(remaining, value.length()));
    }

    private static void tag(Observation.Context context, String key, String value) {
        if (value != null) {
            context.addHighCardinalityKeyValue(KeyValue.of(key,
                value.length() > MAX_CONTENT_LENGTH ? value.substring(0, MAX_CONTENT_LENGTH) : value));
        }
    }
}
