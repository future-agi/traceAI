package ai.traceai.cohere;

import ai.traceai.FITracer;
import ai.traceai.SemanticConventions;
import com.cohere.api.core.ObjectMappers;
import com.cohere.api.resources.v2.types.V2ChatStreamResponse;
import com.cohere.api.types.ApiMeta;
import com.cohere.api.types.NonStreamedChatResponse;
import com.cohere.api.types.StreamedChatResponse;
import com.cohere.api.types.Usage;
import com.fasterxml.jackson.databind.JsonNode;
import io.opentelemetry.api.trace.Span;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;

/** Shared, privacy-gated attributes for the additional SDK surfaces. */
final class CohereAttributes {
    private final FITracer tracer;
    private final Span span;
    // Never accumulate unbounded output or retain content when capture is disabled.
    private final StringBuilder text = new StringBuilder();
    private boolean truncated;
    private boolean failed;
    private static final int MAX_TEXT = 32000;

    CohereAttributes(FITracer tracer, Span span) {
        this.tracer = tracer;
        this.span = span;
        span.setAttribute(SemanticConventions.LLM_SYSTEM, "cohere");
        span.setAttribute(SemanticConventions.LLM_PROVIDER, "cohere");
    }

    void model(String model) {
        span.setAttribute(SemanticConventions.LLM_MODEL_NAME, model);
        span.setAttribute(SemanticConventions.LLM_REQUEST_MODEL, model);
    }

    void chatInput(Object request) {
        if (tracer.getConfig().isHideInputs() || tracer.getConfig().isHideInputMessages()) return;
        JsonNode json = json(request);
        if (json == null) return;
        List<Map<String, String>> messages = new ArrayList<>();
        if (json.has("messages")) {
            for (JsonNode message : json.path("messages")) {
                messages.add(FITracer.message(message.path("role").asText(), content(message.path("content"))));
            }
        } else {
            if (json.hasNonNull("preamble")) messages.add(FITracer.message("system", json.get("preamble").asText()));
            for (JsonNode message : json.path("chat_history")) {
                String role = message.path("role").asText().toLowerCase(java.util.Locale.ROOT);
                messages.add(FITracer.message("chatbot".equals(role) ? "assistant" : role,
                    message.path("message").asText()));
            }
            messages.add(FITracer.message("user", json.path("message").asText()));
        }
        tracer.setInputMessages(span, messages);
        tracer.setInputValue(span, json.has("messages") ? json.get("messages").toString() : json.path("message").asText());
        tracer.setRawInput(span, json.toString());
    }

    void rawInput(Object request) {
        if (tracer.getConfig().isHideInputs()) return;
        JsonNode json = json(request);
        if (json != null) tracer.setRawInput(span, json.toString());
    }

    void rawOutput(Object response) {
        if (tracer.getConfig().isHideOutputs()) return;
        JsonNode json = json(response);
        if (json != null) tracer.setRawOutput(span, json.toString());
    }

    void chatOutput(Object response) {
        if (!captureOutput()) return;
        JsonNode json = json(response);
        if (json == null) return;
        String result = json.has("text") ? json.get("text").asText() : content(json.path("message").path("content"));
        tracer.setOutputValue(span, result);
        tracer.setOutputMessages(span, List.of(FITracer.message("assistant", result)));
        tracer.setRawOutput(span, json.toString());
    }

    void usage(Usage usage) {
        usage.getTokens().ifPresent(tokens -> {
            tokens.getInputTokens().ifPresent(value -> span.setAttribute(SemanticConventions.LLM_TOKEN_COUNT_PROMPT, value.longValue()));
            tokens.getOutputTokens().ifPresent(value -> span.setAttribute(SemanticConventions.LLM_TOKEN_COUNT_COMPLETION, value.longValue()));
            if (tokens.getInputTokens().isPresent() && tokens.getOutputTokens().isPresent()) {
                span.setAttribute(SemanticConventions.LLM_TOKEN_COUNT_TOTAL,
                    tokens.getInputTokens().get().longValue() + tokens.getOutputTokens().get().longValue());
            }
        });
    }

    void meta(ApiMeta meta) {
        meta.getTokens().ifPresent(tokens -> {
            tokens.getInputTokens().ifPresent(value -> span.setAttribute(SemanticConventions.LLM_TOKEN_COUNT_PROMPT, value.longValue()));
            tokens.getOutputTokens().ifPresent(value -> span.setAttribute(SemanticConventions.LLM_TOKEN_COUNT_COMPLETION, value.longValue()));
            if (tokens.getInputTokens().isPresent() && tokens.getOutputTokens().isPresent()) {
                span.setAttribute(SemanticConventions.LLM_TOKEN_COUNT_TOTAL,
                    tokens.getInputTokens().get().longValue() + tokens.getOutputTokens().get().longValue());
            }
        });
        meta.getBilledUnits().ifPresent(units -> {
            units.getInputTokens().ifPresent(value -> span.setAttribute("cohere.billed_units.input_tokens", value.longValue()));
            units.getSearchUnits().ifPresent(value -> span.setAttribute("cohere.rerank.search_units", value.longValue()));
        });
    }

    TracedCohereStream.EventState v1Event(StreamedChatResponse event) {
        event.getStreamStart().ifPresent(start -> span.setAttribute(SemanticConventions.LLM_RESPONSE_ID, start.getGenerationId()));
        event.getTextGeneration().ifPresent(delta -> append(delta.getText()));
        if (event.getStreamEnd().isEmpty()) return TracedCohereStream.EventState.CONTINUE;
        var end = event.getStreamEnd().get();
        NonStreamedChatResponse response = end.getResponse();
        response.getGenerationId().ifPresent(id -> span.setAttribute(SemanticConventions.LLM_RESPONSE_ID, id));
        response.getMeta().ifPresent(this::meta);
        if (captureOutput() && response.getText() != null) {
            text.setLength(0);
            truncated = false;
            append(response.getText());
        }
        return finishReason(end.getFinishReason().toString(), null);
    }

    TracedCohereStream.EventState v2Event(V2ChatStreamResponse event) {
        event.getMessageStart().flatMap(start -> start.getId())
            .ifPresent(id -> span.setAttribute(SemanticConventions.LLM_RESPONSE_ID, id));
        event.getContentDelta().flatMap(delta -> delta.getDelta()).flatMap(delta -> delta.getMessage())
            .flatMap(message -> message.getContent()).flatMap(content -> content.getText()).ifPresent(this::append);
        if (event.getMessageEnd().isEmpty()) return TracedCohereStream.EventState.CONTINUE;
        var end = event.getMessageEnd().get();
        end.getId().ifPresent(id -> span.setAttribute(SemanticConventions.LLM_RESPONSE_ID, id));
        end.getDelta().flatMap(delta -> delta.getUsage()).ifPresent(this::usage);
        String reason = end.getDelta().flatMap(delta -> delta.getFinishReason()).map(Object::toString).orElse("");
        String error = end.getDelta().flatMap(delta -> delta.getError()).orElse(null);
        return finishReason(reason, error);
    }

    boolean hasError() {
        return failed;
    }

    TracedCohereStream.EventState finishReason(String reason, String error) {
        if (!reason.isEmpty()) span.setAttribute(SemanticConventions.LLM_RESPONSE_FINISH_REASON, reason);
        if (error != null || reason.toLowerCase(java.util.Locale.ROOT).startsWith("error")) {
            failed = true;
            tracer.setError(span, new IllegalStateException(error == null ? "Cohere generation failed" : error));
            return TracedCohereStream.EventState.ERROR;
        }
        return TracedCohereStream.EventState.COMPLETE;
    }

    void streamOutput() {
        if (!captureOutput()) return;
        tracer.setOutputValue(span, text.toString());
        tracer.setOutputMessages(span, List.of(FITracer.message("assistant", text.toString())));
        if (truncated) span.setAttribute("cohere.stream.output_truncated", true);
    }

    private void append(String value) {
        if (!captureOutput()) return;
        int remaining = MAX_TEXT - text.length();
        text.append(value, 0, Math.min(remaining, value.length()));
        truncated |= value.length() > remaining;
    }

    private boolean captureOutput() {
        return !tracer.getConfig().isHideOutputs() && !tracer.getConfig().isHideOutputMessages();
    }

    private static String content(JsonNode content) {
        if (content.isTextual()) return content.asText();
        StringBuilder result = new StringBuilder();
        if (content.isArray()) for (JsonNode part : content) result.append(part.path("text").asText(""));
        return result.toString();
    }

    private static JsonNode json(Object value) {
        try {
            // Use the SDK's Jackson mapping, which understands its unions and Optional fields.
            return ObjectMappers.JSON_MAPPER.valueToTree(value);
        } catch (IllegalArgumentException error) {
            // Unsupported user-supplied extension data must not break the provider call.
            return null;
        }
    }
}
