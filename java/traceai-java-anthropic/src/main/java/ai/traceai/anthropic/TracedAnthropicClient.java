package ai.traceai.anthropic;

import ai.traceai.*;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.context.Scope;

import java.util.List;
import java.util.stream.Stream;

/**
 * Instrumentation wrapper for Anthropic Java client.
 * Wraps the Anthropic client to provide automatic tracing of all API calls.
 *
 * <p>Usage:</p>
 * <pre>
 * AnthropicClient anthropic = Anthropic.client();
 * TracedAnthropicClient traced = new TracedAnthropicClient(anthropic);
 *
 * Message response = traced.createMessage(
 *     MessageCreateParams.builder()
 *         .model("claude-3-5-sonnet-20241022")
 *         .addMessage(MessageParam.user("Hello!"))
 *         .maxTokens(1024)
 *         .build()
 * );
 * </pre>
 *
 * <p>Note: This implementation uses reflection-based approach to support
 * different versions of the Anthropic Java SDK.</p>
 */
public class TracedAnthropicClient {

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
            if (temperature != null) {
                span.setAttribute(SemanticConventions.LLM_REQUEST_TEMPERATURE, ((Number) temperature).doubleValue());
            }

            // Capture input messages
            List<?> messages = extractFieldList(params, "messages");
            if (messages != null) {
                for (int i = 0; i < messages.size(); i++) {
                    Object msg = messages.get(i);
                    String role = extractField(msg, "role");
                    String content = extractContent(msg);
                    tracer.setInputMessage(span, i, role != null ? role : "user", content);
                }
            }

            // Extract system prompt
            String systemPrompt = extractField(params, "system");
            if (systemPrompt != null) {
                tracer.setInputMessage(span, -1, "system", systemPrompt);
            }

            // Capture raw input
            tracer.setRawInput(span, params);

            // Execute request using reflection
            Object messagesApi = invokeMethod(client, "messages");
            T result = (T) invokeMethod(messagesApi, "create", params);

            // Extract response model
            String responseModel = extractField(result, "model");
            if (responseModel != null) {
                span.setAttribute(SemanticConventions.LLM_RESPONSE_MODEL, responseModel);
            }

            // Extract response ID
            String responseId = extractField(result, "id");
            if (responseId != null) {
                span.setAttribute(SemanticConventions.LLM_RESPONSE_ID, responseId);
            }

            // Extract output content
            List<?> contentBlocks = extractFieldList(result, "content");
            if (contentBlocks != null && !contentBlocks.isEmpty()) {
                StringBuilder outputBuilder = new StringBuilder();
                for (int i = 0; i < contentBlocks.size(); i++) {
                    Object block = contentBlocks.get(i);
                    String text = extractField(block, "text");
                    if (text != null) {
                        outputBuilder.append(text);
                    }
                }
                String output = outputBuilder.toString();
                tracer.setOutputValue(span, output);
                tracer.setOutputMessage(span, 0, "assistant", output);
            }

            // Extract stop reason
            String stopReason = extractField(result, "stopReason");
            if (stopReason != null) {
                span.setAttribute(SemanticConventions.LLM_RESPONSE_FINISH_REASON, stopReason);
            }

            // Extract usage
            Object usage = extractFieldObject(result, "usage");
            if (usage != null) {
                Object inputTokens = extractFieldObject(usage, "inputTokens");
                Object outputTokens = extractFieldObject(usage, "outputTokens");
                if (inputTokens != null && outputTokens != null) {
                    int input = ((Number) inputTokens).intValue();
                    int output = ((Number) outputTokens).intValue();
                    tracer.setTokenCounts(span, input, output, input + output);
                }
            }

            // Capture raw output
            tracer.setRawOutput(span, result);

            span.setStatus(StatusCode.OK);
            return result;

        } catch (Exception e) {
            tracer.setError(span, e);
            throw new RuntimeException("Failed to create message", e);
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
                return obj.getClass().getMethod(getterName).invoke(obj);
            } catch (NoSuchMethodException e) {
                // Try direct method
                try {
                    return obj.getClass().getMethod(fieldName).invoke(obj);
                } catch (NoSuchMethodException e2) {
                    // Try field access
                    var field = obj.getClass().getDeclaredField(fieldName);
                    field.setAccessible(true);
                    return field.get(obj);
                }
            }
        } catch (Exception e) {
            return null;
        }
    }

    @SuppressWarnings("unchecked")
    private List<?> extractFieldList(Object obj, String fieldName) {
        Object value = extractFieldObject(obj, fieldName);
        if (value instanceof List) {
            return (List<?>) value;
        }
        return null;
    }

    private String extractContent(Object message) {
        // Anthropic messages can have complex content structures
        Object content = extractFieldObject(message, "content");
        if (content == null) return null;

        if (content instanceof String) {
            return (String) content;
        }

        if (content instanceof List) {
            StringBuilder sb = new StringBuilder();
            for (Object block : (List<?>) content) {
                String text = extractField(block, "text");
                if (text != null) {
                    sb.append(text);
                }
            }
            return sb.toString();
        }

        return content.toString();
    }

    private Object invokeMethod(Object obj, String methodName, Object... args) throws Exception {
        Class<?>[] argTypes = new Class<?>[args.length];
        for (int i = 0; i < args.length; i++) {
            argTypes[i] = args[i].getClass();
        }

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
