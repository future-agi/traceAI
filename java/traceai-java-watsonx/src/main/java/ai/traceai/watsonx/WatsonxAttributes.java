package ai.traceai.watsonx;

import ai.traceai.FITracer;
import ai.traceai.SemanticConventions;
import com.ibm.watsonx.ai.chat.ChatRequest;
import com.ibm.watsonx.ai.chat.ChatResponse;
import com.ibm.watsonx.ai.embedding.EmbeddingResponse;
import com.ibm.watsonx.ai.textgeneration.TextGenerationResponse;
import io.opentelemetry.api.trace.Span;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;

/** Metadata only. IBM changed public bean getters to record-style accessors between releases. */
final class WatsonxAttributes {
    private final FITracer tracer;

    WatsonxAttributes(FITracer tracer) { this.tracer = tracer; }

    void request(Span span, Object request, Object explicitParameters) {
        span.setAttribute(SemanticConventions.LLM_SYSTEM, "watsonx");
        span.setAttribute(SemanticConventions.LLM_PROVIDER, "ibm");
        Object parameters = explicitParameters != null ? explicitParameters : field(request, "parameters");
        text(span, SemanticConventions.LLM_REQUEST_MODEL, field(parameters, "modelId"));
        text(span, SemanticConventions.LLM_MODEL_NAME, field(parameters, "modelId"));
        number(span, SemanticConventions.LLM_REQUEST_MAX_TOKENS,
            first(field(parameters, "maxNewTokens"), first(field(parameters, "maxCompletionTokens"), field(parameters, "maxTokens"))));
        decimal(span, SemanticConventions.LLM_REQUEST_TEMPERATURE, field(parameters, "temperature"));
        decimal(span, SemanticConventions.LLM_REQUEST_TOP_P, field(parameters, "topP"));
        if (request instanceof ChatRequest) {
            if (!tracer.getConfig().isHideInputMessages()) {
                List<Map<String, String>> messages = new ArrayList<>();
                String lastUserContent = null;
                for (Object message : list(field(request, "messages"))) {
                    String role = string(field(message, "role"));
                    String messageContent = content(field(message, "content"));
                    messages.add(FITracer.message(role, messageContent));
                    if ("user".equalsIgnoreCase(role)) lastUserContent = messageContent;
                }
                tracer.setInputMessages(span, messages);
                tracer.setInputValue(span, lastUserContent);
                tracer.setRawInput(span, request);
            }
        } else {
            Object input = request instanceof List<?> ? request : field(request, "input");
            tracer.setInputValue(span, input instanceof List<?> ? tracer.toJson(input) : string(input));
            if (input instanceof String value) tracer.setInputMessages(span, List.of(FITracer.message("user", value)));
            tracer.setRawInput(span, request);
        }
    }

    void textResponse(Span span, TextGenerationResponse response) {
        if (response == null) return;
        model(span, response.modelId());
        if (response.results() != null && !response.results().isEmpty()) {
            var result = response.results().get(0);
            tracer.setOutputValue(span, result.generatedText());
            tracer.setOutputMessages(span, List.of(FITracer.message("assistant", result.generatedText())));
            text(span, SemanticConventions.LLM_RESPONSE_FINISH_REASON, result.stopReason());
            tokens(span, result.inputTokenCount(), result.generatedTokenCount());
        }
        // Generation responses may include input_tokens with the original prompt text.
        if (!tracer.getConfig().isHideInputs()) tracer.setRawOutput(span, response);
    }

    void chatResponse(Span span, ChatResponse response) {
        if (response == null) return;
        model(span, string(field(response, "modelId")));
        text(span, SemanticConventions.LLM_RESPONSE_ID, field(response, "id"));
        List<?> choices = list(field(response, "choices"));
        if (!choices.isEmpty()) {
            Object choice = choices.get(0);
            if (!tracer.getConfig().isHideOutputMessages()) {
                String content = content(field(field(choice, "message"), "content"));
                tracer.setOutputValue(span, content);
                tracer.setOutputMessages(span, List.of(FITracer.message("assistant", content)));
            }
            text(span, SemanticConventions.LLM_RESPONSE_FINISH_REASON, field(choice, "finishReason"));
        }
        Object usage = field(response, "usage");
        tokens(span, field(usage, "promptTokens"), field(usage, "completionTokens"));
        if (!tracer.getConfig().isHideOutputMessages()) tracer.setRawOutput(span, response);
    }

    void embeddingResponse(Span span, EmbeddingResponse response) {
        if (response == null) return;
        text(span, SemanticConventions.EMBEDDING_MODEL_NAME, response.modelId());
        number(span, SemanticConventions.LLM_TOKEN_COUNT_PROMPT, response.inputTokenCount());
        if (response.results() != null) {
            span.setAttribute(SemanticConventions.EMBEDDING_VECTOR_COUNT, (long) response.results().size());
            if (!response.results().isEmpty() && response.results().get(0).embedding() != null) {
                span.setAttribute(SemanticConventions.EMBEDDING_DIMENSIONS, (long) response.results().get(0).embedding().size());
            }
        }
        // IBM embedding responses can echo input text. Do not bypass hideInputs via raw.output.
        if (!tracer.getConfig().isHideInputs()) tracer.setRawOutput(span, response);
    }

    private void model(Span span, String model) {
        // LLM_MODEL_NAME aliases the request-model key; never overwrite it with a response model.
        text(span, SemanticConventions.LLM_RESPONSE_MODEL, model);
    }

    private void tokens(Span span, Object prompt, Object completion) {
        number(span, SemanticConventions.LLM_TOKEN_COUNT_PROMPT, prompt);
        number(span, SemanticConventions.LLM_TOKEN_COUNT_COMPLETION, completion);
        if (prompt instanceof Number p && completion instanceof Number c) {
            span.setAttribute(SemanticConventions.LLM_TOKEN_COUNT_TOTAL, p.longValue() + c.longValue());
        }
    }

    private static void text(Span span, String key, Object value) {
        if (value != null) span.setAttribute(key, value.toString());
    }

    private static void number(Span span, String key, Object value) {
        if (value instanceof Number number) span.setAttribute(key, number.longValue());
    }

    private static void decimal(Span span, String key, Object value) {
        if (value instanceof Number number) span.setAttribute(key, number.doubleValue());
    }

    private static Object first(Object first, Object second) { return first != null ? first : second; }
    private static String string(Object value) { return value == null ? null : value.toString(); }
    private static List<?> list(Object value) { return value instanceof List<?> list ? list : List.of(); }

    private static String content(Object value) {
        if (value instanceof String text) return text;
        if (value instanceof List<?> parts) {
            StringBuilder text = new StringBuilder();
            for (Object part : parts) {
                Object fragment = field(part, "text");
                if (fragment != null) text.append(fragment);
            }
            return text.toString();
        }
        return null;
    }

    // Only public accessors with exact zero-argument signatures. No private-field access.
    static Object field(Object value, String name) {
        if (value == null) return null;
        for (String accessor : new String[]{name, "get" + Character.toUpperCase(name.charAt(0)) + name.substring(1)}) {
            try {
                return value.getClass().getMethod(accessor).invoke(value);
            } catch (NoSuchMethodException absent) {
                // An optional getter may be absent or have its record-style spelling.
            } catch (ReflectiveOperationException failure) {
                // Never log a request or an accessor exception's potentially sensitive message.
                org.slf4j.LoggerFactory.getLogger(WatsonxAttributes.class).debug(
                    "Cannot read Watsonx metadata accessor {} ({})", accessor, failure.getClass().getName());
                return null;
            }
        }
        return null;
    }
}
