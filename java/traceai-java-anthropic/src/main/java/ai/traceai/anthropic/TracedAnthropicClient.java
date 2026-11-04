package ai.traceai.anthropic;

import ai.traceai.*;
import com.anthropic.core.ObjectMappers;
import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.json.JsonMapper;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.context.Scope;

import java.lang.reflect.InvocationTargetException;
import java.util.ArrayList;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Optional;

/**
 * Legacy instrumentation wrapper for blocking Anthropic message creation.
 *
 * <p>For SDKs with native OpenTelemetry tracing, prefer the native client and a
 * standard OTLP exporter. Do not wrap a native-traced client: both instrumentations
 * emit a request span and token counts. If retaining this wrapper with a modern
 * SDK, construct that client with {@code openTelemetry(false)}. Older SDKs without
 * native tracing do not require that option.</p>
 *
 * <p>Usage:</p>
 * <pre>
 * AnthropicClient anthropic = AnthropicOkHttpClient.builder()
 *     .apiKey(System.getenv("ANTHROPIC_API_KEY"))
 *     .openTelemetry(false) // Omit on older SDKs without native tracing.
 *     .build();
 * TracedAnthropicClient traced = new TracedAnthropicClient(anthropic);
 *
 * Message response = traced.createMessage(
 *     MessageCreateParams.builder()
 *         .model("claude-opus-5-5")
 *         .addUserMessage("Hello!")
 *         .maxTokens(1024)
 *         .build()
 * );
 * </pre>
 *
 * <p>Note: This implementation uses reflection-based approach to support
 * different versions of the Anthropic Java SDK.</p>
 */
public class TracedAnthropicClient {

    private static final JsonMapper JSON_MAPPER = ObjectMappers.jsonMapper();
    private static final Map<String, String> REQUEST_BODY_FIELDS = Map.ofEntries(
        Map.entry("model", "model"), Map.entry("messages", "messages"),
        Map.entry("maxTokens", "max_tokens"), Map.entry("system", "system"),
        Map.entry("metadata", "metadata"), Map.entry("stopSequences", "stop_sequences"),
        Map.entry("temperature", "temperature"), Map.entry("topK", "top_k"),
        Map.entry("topP", "top_p"), Map.entry("tools", "tools"),
        Map.entry("toolChoice", "tool_choice"), Map.entry("thinking", "thinking"),
        Map.entry("outputConfig", "output_config")
    );

    private final Object client; // AnthropicClient
    private final FITracer tracer;

    /**
     * Creates a new traced Anthropic client with the given client and tracer.
     *
     * @param client the Anthropic client to wrap
     * @param tracer the FITracer for instrumentation
     */
    public TracedAnthropicClient(Object client, FITracer tracer) {
        this.client = client;
        this.tracer = tracer;
    }

    /**
     * Creates a new traced Anthropic client using the global TraceAI tracer.
     *
     * @param client the Anthropic client to wrap
     */
    public TracedAnthropicClient(Object client) {
        this(client, TraceAI.getTracer());
    }

    /**
     * Creates a message with tracing.
     * Uses reflection to call the underlying client's createMessage method.
     *
     * @param params the message creation parameters
     * @param <T> the response type
     * @return the message response
     */
    @SuppressWarnings("unchecked")
    public <T> T createMessage(Object params) {
        Span span = tracer.startSpan("Anthropic Message", FISpanKind.LLM);

        try (Scope scope = span.makeCurrent()) {
            // Set system attributes
            span.setAttribute(SemanticConventions.LLM_SYSTEM, "anthropic");
            span.setAttribute(SemanticConventions.LLM_PROVIDER, "anthropic");

            // Extract model from params using reflection
            String model = extractField(params, "model");
            if (model != null) {
                span.setAttribute(SemanticConventions.LLM_MODEL_NAME, model);
                span.setAttribute(SemanticConventions.LLM_REQUEST_MODEL, model);
            }

            // Extract max_tokens
            Object maxTokens = extractFieldObject(params, "maxTokens");
            if (maxTokens != null) {
                span.setAttribute(SemanticConventions.LLM_REQUEST_MAX_TOKENS, ((Number) maxTokens).longValue());
            }

            // Extract temperature
            Object temperature = extractFieldObject(params, "temperature");
            if (temperature instanceof Number) {
                span.setAttribute(SemanticConventions.LLM_REQUEST_TEMPERATURE, ((Number) temperature).doubleValue());
            }

            // Capture input messages
            List<Map<String, String>> inputMsgs = new ArrayList<>();

            // System prompt goes first
            String systemPrompt = extractText(extractFieldObject(params, "system"));
            if (systemPrompt != null) {
                inputMsgs.add(FITracer.message("system", systemPrompt));
            }

            List<?> messages = extractFieldList(params, "messages");
            if (messages != null) {
                for (Object msg : messages) {
                    String role = extractField(msg, "role");
                    String content = extractText(extractFieldObject(msg, "content"));
                    inputMsgs.add(FITracer.message(role != null ? role : "user", content));
                }
            }

            tracer.setInputMessages(span, inputMsgs);

            // Allowlist body fields, never the params envelope or its transport
            // headers/query parameters. SDK serializers preserve structured blocks.
            if (!tracer.getConfig().isHideInputs()) {
                Map<String, Object> body = new LinkedHashMap<>();
                REQUEST_BODY_FIELDS.forEach((accessor, key) -> {
                    Object value = extractFieldObject(params, accessor);
                    if (value != null) body.put(key, value);
                });
                tracer.setRawInput(span, serializeContent(body));
            }

            // Execute request using reflection
            Object messagesApi = invokeMethod(client, "messages");
            T result = (T) invokeMethod(messagesApi, "create", params);

            // StructuredMessage holds parsed POJOs, not wire text/JSON. Observe its
            // raw Message when available, but return the original typed result.
            Object rawMessage = extractFieldObject(result, "rawMessage");
            Object response = rawMessage != null ? rawMessage : result;

            // Extract response model
            String responseModel = extractField(response, "model");
            if (responseModel != null) {
                span.setAttribute(SemanticConventions.LLM_RESPONSE_MODEL, responseModel);
            }

            // Extract response ID
            String responseId = extractField(response, "id");
            if (responseId != null) {
                span.setAttribute(SemanticConventions.LLM_RESPONSE_ID, responseId);
            }

            // Unwrap SDK content unions; never export their debug representation.
            String output = extractText(extractFieldObject(response, "content"));
            if (output != null) {
                tracer.setOutputValue(span, output);
                tracer.setOutputMessages(span, Collections.singletonList(FITracer.message("assistant", output)));
            }

            // Extract stop reason
            String stopReason = extractField(response, "stopReason");
            if (stopReason != null) {
                span.setAttribute(SemanticConventions.LLM_RESPONSE_FINISH_REASON, stopReason);
            }

            // Extract usage
            Object usage = extractFieldObject(response, "usage");
            if (usage != null) {
                Object inputTokens = extractFieldObject(usage, "inputTokens");
                Object outputTokens = extractFieldObject(usage, "outputTokens");
                Number cacheRead = extractNumber(usage, "cacheReadInputTokens");
                Number cacheWrite = extractNumber(usage, "cacheCreationInputTokens");
                if (cacheRead != null) {
                    span.setAttribute(SemanticConventions.GEN_AI_USAGE_INPUT_TOKENS_CACHE_READ, cacheRead.longValue());
                }
                if (cacheWrite != null) {
                    span.setAttribute(SemanticConventions.GEN_AI_USAGE_INPUT_TOKENS_CACHE_WRITE, cacheWrite.longValue());
                }
                if (inputTokens instanceof Number && outputTokens instanceof Number) {
                    // Anthropic's inputTokens excludes both cache reads and creation.
                    long input = ((Number) inputTokens).longValue()
                        + (cacheRead == null ? 0 : cacheRead.longValue())
                        + (cacheWrite == null ? 0 : cacheWrite.longValue());
                    long completion = ((Number) outputTokens).longValue();
                    tracer.setTokenCounts(span, input, completion, input + completion);
                }
            }

            // Capture raw output
            if (!tracer.getConfig().isHideOutputs()) {
                tracer.setRawOutput(span, serializeContent(response));
            }

            span.setStatus(StatusCode.OK);
            return result;

        } catch (Exception e) {
            Throwable failure = e;
            while (failure instanceof InvocationTargetException && failure.getCause() != null) {
                failure = failure.getCause();
            }
            tracer.setError(span, failure);
            if (failure instanceof RuntimeException) {
                throw (RuntimeException) failure;
            }
            if (failure instanceof Error) {
                throw (Error) failure;
            }
            throw new RuntimeException("Failed to create message", failure);
        } finally {
            span.end();
        }
    }

    /**
     * Gets the underlying Anthropic client.
     *
     * @return the wrapped Anthropic client
     */
    public Object unwrap() {
        return client;
    }

    private String serializeContent(Object value) {
        try {
            return JSON_MAPPER.writeValueAsString(value);
        } catch (JsonProcessingException | IllegalArgumentException e) {
            // Unsupported content must not fail a provider call or fall back to
            // SDK debug strings (which can contain implementation/transport state).
            return null;
        }
    }

    // Reflection utility methods

    private String extractField(Object obj, String fieldName) {
        Object value = extractFieldObject(obj, fieldName);
        return value != null ? value.toString() : null;
    }

    private Object extractFieldObject(Object obj, String fieldName) {
        if (obj == null) return null;
        try {
            // Try getter method first
            String getterName = "get" + Character.toUpperCase(fieldName.charAt(0)) + fieldName.substring(1);
            try {
                return unwrapOptional(obj.getClass().getMethod(getterName).invoke(obj));
            } catch (NoSuchMethodException e) {
                // Try direct method
                try {
                    return unwrapOptional(obj.getClass().getMethod(fieldName).invoke(obj));
                } catch (NoSuchMethodException e2) {
                    // Try field access
                    var field = obj.getClass().getDeclaredField(fieldName);
                    field.setAccessible(true);
                    return unwrapOptional(field.get(obj));
                }
            }
        } catch (Exception e) {
            return null;
        }
    }

    private List<?> extractFieldList(Object obj, String fieldName) {
        Object value = extractFieldObject(obj, fieldName);
        if (value instanceof List) {
            return (List<?>) value;
        }
        return null;
    }

    private Object unwrapOptional(Object value) {
        return value instanceof Optional<?> ? ((Optional<?>) value).orElse(null) : value;
    }

    private Number extractNumber(Object obj, String fieldName) {
        Object value = extractFieldObject(obj, fieldName);
        return value instanceof Number ? (Number) value : null;
    }

    private String extractText(Object content) {
        if (content == null) return null;
        if (content instanceof String) return (String) content;
        if (content instanceof List<?>) {
            StringBuilder text = new StringBuilder();
            for (Object block : (List<?>) content) {
                String blockText = extractText(block);
                if (blockText != null) text.append(blockText);
            }
            return text.toString();
        }

        // Message/system content is a string-or-blocks union. ContentBlock.text()
        // returns Optional<TextBlock>, whose text() in turn returns the string.
        // The list accessor was renamed between the SDK floor and newer releases.
        for (String field : new String[] {"string", "text", "blockParams", "contentBlockParams", "textBlockParams"}) {
            Object value = extractFieldObject(content, field);
            if (value != null) return extractText(value);
        }
        // Non-text blocks remain in raw input/output, not in the text observation.
        return null;
    }

    private Object invokeMethod(Object obj, String methodName, Object... args) throws Exception {
        // Find a matching method
        for (var method : obj.getClass().getMethods()) {
            if (method.getName().equals(methodName) && method.getParameterCount() == args.length) {
                try {
                    return method.invoke(obj, args);
                } catch (IllegalArgumentException e) {
                    // Try next matching method
                }
            }
        }

        throw new NoSuchMethodException(methodName);
    }
}
