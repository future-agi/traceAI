package ai.traceai.watsonx;

import ai.traceai.*;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.context.Scope;

import java.util.ArrayList;
import java.util.Collections;
import java.util.Iterator;
import java.util.List;
import java.util.Map;

/**
 * Instrumentation wrapper for IBM watsonx.ai Java client.
 * Wraps the WatsonxAI client to provide automatic tracing of all API calls.
 *
 * <p>Usage:</p>
 * <pre>
 * WatsonxAI watsonx = WatsonxAI.builder()
 *     .apiKey("api-key")
 *     .projectId("project-id")
 *     .build();
 * TracedWatsonxAI traced = new TracedWatsonxAI(watsonx);
 *
 * TextGenResponse response = traced.generateText(TextGenRequest.builder()
 *     .modelId("ibm/granite-13b-chat-v2")
 *     .input("Hello, how are you?")
 *     .build());
 * </pre>
 *
 * <p>Note: This implementation uses reflection-based approach to support
 * different versions of the IBM watsonx.ai Java SDK.</p>
 */
public class TracedWatsonxAI {

    private final Object client; // WatsonxAI client
    private final FITracer tracer;

    /**
     * Creates a new traced watsonx.ai client with the given client and tracer.
     *
     * @param client the WatsonxAI client to wrap
     * @param tracer the FITracer for instrumentation
     */
    public TracedWatsonxAI(Object client, FITracer tracer) {
        this.client = client;
        this.tracer = tracer;
    }

    /**
     * Creates a new traced watsonx.ai client using the global TraceAI tracer.
     *
     * @param client the WatsonxAI client to wrap
     */
    public TracedWatsonxAI(Object client) {
        this(client, TraceAI.getTracer());
    }

    /**
     * Generates text with tracing.
     * Uses reflection to call the underlying client's text generation method.
     *
     * @param request the text generation request (TextGenRequest)
     * @param <T> the response type
     * @return the text generation response
     */
    @SuppressWarnings("unchecked")
    public <T> T generateText(Object request) {
        Span span = tracer.startSpan("Watsonx Text Generation", FISpanKind.LLM);

        try (Scope scope = span.makeCurrent()) {
            // Set system attributes
            span.setAttribute(SemanticConventions.LLM_SYSTEM, "watsonx");
            span.setAttribute(SemanticConventions.LLM_PROVIDER, "ibm");

            // Extract model from request
            String modelId = extractField(request, "modelId");
            if (modelId != null) {
                span.setAttribute(SemanticConventions.LLM_MODEL_NAME, modelId);
                span.setAttribute(SemanticConventions.LLM_REQUEST_MODEL, modelId);
            }

            // Extract project_id and space_id
            String projectId = extractField(request, "projectId");
            if (projectId != null) {
                span.setAttribute("watsonx.project_id", projectId);
            }
            String spaceId = extractField(request, "spaceId");
            if (spaceId != null) {
                span.setAttribute("watsonx.space_id", spaceId);
            }

            // Extract input text
            String input = extractField(request, "input");
            if (input != null) {
                tracer.setInputValue(span, input);
                tracer.setInputMessages(span, Collections.singletonList(FITracer.message("user", input)));
            }

            // Extract parameters if available
            Object parameters = extractFieldObject(request, "parameters");
            if (parameters != null) {
                // Extract temperature
                Object temperature = extractFieldObject(parameters, "temperature");
                if (temperature != null) {
                    span.setAttribute(SemanticConventions.LLM_REQUEST_TEMPERATURE, ((Number) temperature).doubleValue());
                }

                // Extract max_new_tokens
                Object maxNewTokens = extractFieldObject(parameters, "maxNewTokens");
                if (maxNewTokens != null) {
                    span.setAttribute(SemanticConventions.LLM_REQUEST_MAX_TOKENS, ((Number) maxNewTokens).longValue());
                }

                // Extract top_p
                Object topP = extractFieldObject(parameters, "topP");
                if (topP != null) {
                    span.setAttribute(SemanticConventions.LLM_REQUEST_TOP_P, ((Number) topP).doubleValue());
                }

                // Extract stop sequences
                List<?> stopSequences = extractFieldList(parameters, "stopSequences");
                if (stopSequences != null && !stopSequences.isEmpty()) {
                    span.setAttribute(SemanticConventions.LLM_REQUEST_STOP_SEQUENCES, tracer.toJson(stopSequences));
                }
            }

            // Capture raw input
            tracer.setRawInput(span, request);

            // Execute request using reflection
            T result = (T) invokeMethod(client, "generateText", request);

            // Extract generated text from response
            String generatedText = extractField(result, "generatedText");
            if (generatedText == null) {
                // Try alternative field names
                generatedText = extractField(result, "generated_text");
            }
            if (generatedText == null) {
                // Try getting from results list
                List<?> results = extractFieldList(result, "results");
                if (results != null && !results.isEmpty()) {
                    Object firstResult = results.get(0);
                    generatedText = extractField(firstResult, "generatedText");
                    if (generatedText == null) {
                        generatedText = extractField(firstResult, "generated_text");
                    }

                    // Extract stop reason from first result
                    String stopReason = extractField(firstResult, "stopReason");
                    if (stopReason == null) {
                        stopReason = extractField(firstResult, "stop_reason");
                    }
                    if (stopReason != null) {
                        span.setAttribute(SemanticConventions.LLM_RESPONSE_FINISH_REASON, stopReason);
                        span.setAttribute("watsonx.stop_reason", stopReason);
                    }

                    // Extract token counts from first result
                    extractAndSetTokenCounts(span, firstResult);
                }
            }

            if (generatedText != null) {
                tracer.setOutputValue(span, generatedText);
                tracer.setOutputMessages(span, Collections.singletonList(FITracer.message("assistant", generatedText)));
            }

            // Extract stop reason from direct field if not found in results
            String stopReason = extractField(result, "stopReason");
            if (stopReason == null) {
                stopReason = extractField(result, "stop_reason");
            }
            if (stopReason != null) {
                span.setAttribute(SemanticConventions.LLM_RESPONSE_FINISH_REASON, stopReason);
                span.setAttribute("watsonx.stop_reason", stopReason);
            }

            // Try extracting token counts directly from response
            extractAndSetTokenCounts(span, result);

            // Extract model version if available
            String modelVersion = extractField(result, "modelVersion");
            if (modelVersion == null) {
                modelVersion = extractField(result, "model_version");
            }
            if (modelVersion != null) {
                span.setAttribute(SemanticConventions.LLM_RESPONSE_MODEL, modelId + ":" + modelVersion);
            }

            // Capture raw output
            tracer.setRawOutput(span, result);

            span.setStatus(StatusCode.OK);
            return result;

        } catch (Exception e) {
            tracer.setError(span, e);
            throw new RuntimeException("Watsonx text generation failed", e);
        } finally {
            span.end();
        }
    }

    /**
     * Generates text with streaming with tracing.
     * Uses reflection to call the underlying client's streaming text generation method.
     *
     * @param request the text generation request (TextGenRequest)
     * @param <T> the stream/iterator type
     * @return the streaming response iterator
     */
    @SuppressWarnings("unchecked")
    public <T> T generateTextStream(Object request) {
        Span span = tracer.startSpan("Watsonx Text Generation (Stream)", FISpanKind.LLM);

        try (Scope scope = span.makeCurrent()) {
            // Set system attributes
            span.setAttribute(SemanticConventions.LLM_SYSTEM, "watsonx");
            span.setAttribute(SemanticConventions.LLM_PROVIDER, "ibm");

            // Extract model from request
            String modelId = extractField(request, "modelId");
            if (modelId != null) {
                span.setAttribute(SemanticConventions.LLM_MODEL_NAME, modelId);
                span.setAttribute(SemanticConventions.LLM_REQUEST_MODEL, modelId);
            }

            // Extract project_id and space_id
            String projectId = extractField(request, "projectId");
            if (projectId != null) {
                span.setAttribute("watsonx.project_id", projectId);
            }
            String spaceId = extractField(request, "spaceId");
            if (spaceId != null) {
                span.setAttribute("watsonx.space_id", spaceId);
            }

            // Extract input text
            String input = extractField(request, "input");
            if (input != null) {
                tracer.setInputValue(span, input);
                tracer.setInputMessages(span, Collections.singletonList(FITracer.message("user", input)));
            }

            // Extract parameters if available
            Object parameters = extractFieldObject(request, "parameters");
            if (parameters != null) {
                Object temperature = extractFieldObject(parameters, "temperature");
                if (temperature != null) {
                    span.setAttribute(SemanticConventions.LLM_REQUEST_TEMPERATURE, ((Number) temperature).doubleValue());
                }

                Object maxNewTokens = extractFieldObject(parameters, "maxNewTokens");
                if (maxNewTokens != null) {
                    span.setAttribute(SemanticConventions.LLM_REQUEST_MAX_TOKENS, ((Number) maxNewTokens).longValue());
                }
            }

            // Capture raw input
            tracer.setRawInput(span, request);

            // Execute streaming request using reflection
            T streamResult = (T) invokeMethod(client, "generateTextStream", request);

            // For streaming, we wrap the iterator to capture the final output
            // Note: The actual streaming response handling depends on the SDK implementation
            // This creates a wrapper that accumulates the streamed content
            span.setStatus(StatusCode.OK);

            // Note: For proper streaming tracing, the span should be ended when the stream is consumed
            // This is a simplified implementation - a production version would wrap the iterator
            span.end();
            return streamResult;

        } catch (Exception e) {
            tracer.setError(span, e);
            span.end();
            throw new RuntimeException("Watsonx streaming text generation failed", e);
        }
    }

    /**
     * Sends a chat request with tracing.
     * Uses reflection to call the underlying client's chat method.
     *
     * @param request the chat request (TextChatRequest)
     * @param <T> the response type
     * @return the chat response
     */
    @SuppressWarnings("unchecked")
    public <T> T chat(Object request) {
        Span span = tracer.startSpan("Watsonx Chat", FISpanKind.LLM);

        try (Scope scope = span.makeCurrent()) {
            // Set system attributes
            span.setAttribute(SemanticConventions.LLM_SYSTEM, "watsonx");
            span.setAttribute(SemanticConventions.LLM_PROVIDER, "ibm");

            // Extract model from request
            String modelId = extractField(request, "modelId");
            if (modelId != null) {
                span.setAttribute(SemanticConventions.LLM_MODEL_NAME, modelId);
                span.setAttribute(SemanticConventions.LLM_REQUEST_MODEL, modelId);
            }

            // Extract project_id and space_id
            String projectId = extractField(request, "projectId");
            if (projectId != null) {
                span.setAttribute("watsonx.project_id", projectId);
            }
            String spaceId = extractField(request, "spaceId");
            if (spaceId != null) {
                span.setAttribute("watsonx.space_id", spaceId);
            }

            // Extract messages
            List<?> messages = extractFieldList(request, "messages");
            if (messages != null) {
                List<Map<String, String>> inputMessages = new ArrayList<>();
                for (Object msg : messages) {
                    String role = extractField(msg, "role");
                    String content = extractChatMessageContent(msg);
                    inputMessages.add(FITracer.message(role != null ? role : "user", content));
                }
                tracer.setInputMessages(span, inputMessages);

                // Set the last user message as the input value
                for (int i = messages.size() - 1; i >= 0; i--) {
                    Object msg = messages.get(i);
                    String role = extractField(msg, "role");
                    if ("user".equalsIgnoreCase(role)) {
                        String content = extractChatMessageContent(msg);
                        tracer.setInputValue(span, content);
                        break;
                    }
                }
            }

            // Extract parameters if available
            Object parameters = extractFieldObject(request, "parameters");
            if (parameters != null) {
                Object temperature = extractFieldObject(parameters, "temperature");
                if (temperature != null) {
                    span.setAttribute(SemanticConventions.LLM_REQUEST_TEMPERATURE, ((Number) temperature).doubleValue());
                }

                Object maxTokens = extractFieldObject(parameters, "maxTokens");
                if (maxTokens != null) {
                    span.setAttribute(SemanticConventions.LLM_REQUEST_MAX_TOKENS, ((Number) maxTokens).longValue());
                }

                Object topP = extractFieldObject(parameters, "topP");
                if (topP != null) {
                    span.setAttribute(SemanticConventions.LLM_REQUEST_TOP_P, ((Number) topP).doubleValue());
                }
            }

            // Capture raw input
            tracer.setRawInput(span, request);

            // Execute request using reflection
            T result = (T) invokeMethod(client, "chat", request);

            // Extract response content
            List<?> choices = extractFieldList(result, "choices");
            if (choices != null && !choices.isEmpty()) {
                Object firstChoice = choices.get(0);
                Object message = extractFieldObject(firstChoice, "message");
                if (message != null) {
                    String role = extractField(message, "role");
                    String content = extractChatMessageContent(message);
                    if (content != null) {
                        tracer.setOutputValue(span, content);
                        tracer.setOutputMessages(span, Collections.singletonList(FITracer.message(role != null ? role : "assistant", content)));
                    }
                }

                // Extract finish reason
                String finishReason = extractField(firstChoice, "finishReason");
                if (finishReason == null) {
                    finishReason = extractField(firstChoice, "finish_reason");
                }
                if (finishReason != null) {
                    span.setAttribute(SemanticConventions.LLM_RESPONSE_FINISH_REASON, finishReason);
                    span.setAttribute("watsonx.stop_reason", finishReason);
                }
            }

            // Extract usage/token counts
            Object usage = extractFieldObject(result, "usage");
            if (usage != null) {
                Object promptTokens = extractFieldObject(usage, "promptTokens");
                if (promptTokens == null) {
                    promptTokens = extractFieldObject(usage, "prompt_tokens");
                }
                Object completionTokens = extractFieldObject(usage, "completionTokens");
                if (completionTokens == null) {
                    completionTokens = extractFieldObject(usage, "completion_tokens");
                }

                if (promptTokens != null && completionTokens != null) {
                    int input = ((Number) promptTokens).intValue();
                    int output = ((Number) completionTokens).intValue();
                    tracer.setTokenCounts(span, input, output, input + output);
                }
            }

            // Extract response ID
            String responseId = extractField(result, "id");
            if (responseId != null) {
                span.setAttribute(SemanticConventions.LLM_RESPONSE_ID, responseId);
            }

            // Capture raw output
            tracer.setRawOutput(span, result);

            span.setStatus(StatusCode.OK);
            return result;

        } catch (Exception e) {
            tracer.setError(span, e);
            throw new RuntimeException("Watsonx chat failed", e);
        } finally {
            span.end();
        }
    }

    /**
     * Creates embeddings with tracing.
     * Uses reflection to call the underlying client's embed method.
     *
     * @param request the embed text request (EmbedTextRequest)
     * @param <T> the response type
     * @return the embed response
     */
    @SuppressWarnings("unchecked")
    public <T> T embedText(Object request) {
        Span span = tracer.startSpan("Watsonx Embed", FISpanKind.EMBEDDING);

        try (Scope scope = span.makeCurrent()) {
            // Set system attributes
            span.setAttribute(SemanticConventions.LLM_SYSTEM, "watsonx");
            span.setAttribute(SemanticConventions.LLM_PROVIDER, "ibm");

            // Extract model from request
            String modelId = extractField(request, "modelId");
            if (modelId != null) {
                span.setAttribute(SemanticConventions.EMBEDDING_MODEL_NAME, modelId);
            }

            // Extract project_id and space_id
            String projectId = extractField(request, "projectId");
            if (projectId != null) {
                span.setAttribute("watsonx.project_id", projectId);
            }
            String spaceId = extractField(request, "spaceId");
            if (spaceId != null) {
                span.setAttribute("watsonx.space_id", spaceId);
            }

            // Extract input texts
            List<?> inputs = extractFieldList(request, "inputs");
            if (inputs != null) {
                span.setAttribute(SemanticConventions.EMBEDDING_VECTOR_COUNT, (long) inputs.size());

                // Capture sample of input texts
                StringBuilder inputBuilder = new StringBuilder();
                int maxTexts = Math.min(inputs.size(), 5);
                for (int i = 0; i < maxTexts; i++) {
                    if (i > 0) inputBuilder.append("\n---\n");
                    inputBuilder.append(inputs.get(i).toString());
                }
                if (inputs.size() > 5) {
                    inputBuilder.append("\n... and ").append(inputs.size() - 5).append(" more");
                }
                tracer.setInputValue(span, inputBuilder.toString());
            }

            // Extract truncate_input_tokens if available
            Object truncateTokens = extractFieldObject(request, "truncateInputTokens");
            if (truncateTokens == null) {
                truncateTokens = extractFieldObject(request, "truncate_input_tokens");
            }
            if (truncateTokens != null) {
                span.setAttribute("watsonx.truncate_input_tokens", ((Number) truncateTokens).longValue());
            }

            // Capture raw input
            tracer.setRawInput(span, request);

            // Execute request using reflection
            T result = (T) invokeMethod(client, "embedText", request);

            // Extract embeddings from response
            List<?> results = extractFieldList(result, "results");
            if (results != null && !results.isEmpty()) {
                span.setAttribute(SemanticConventions.EMBEDDING_VECTOR_COUNT, (long) results.size());

                // Get embedding dimensions from first result
                Object firstResult = results.get(0);
                List<?> embedding = extractFieldList(firstResult, "embedding");
                if (embedding != null && !embedding.isEmpty()) {
                    span.setAttribute(SemanticConventions.EMBEDDING_DIMENSIONS, (long) embedding.size());
                }
            }

            // Extract token count from response if available
            Object inputTokenCount = extractFieldObject(result, "inputTokenCount");
            if (inputTokenCount == null) {
                inputTokenCount = extractFieldObject(result, "input_token_count");
            }
            if (inputTokenCount != null) {
                span.setAttribute(SemanticConventions.LLM_TOKEN_COUNT_PROMPT, ((Number) inputTokenCount).longValue());
            }

            // Capture raw output
            tracer.setRawOutput(span, result);

            span.setStatus(StatusCode.OK);
            return result;

        } catch (Exception e) {
            tracer.setError(span, e);
            throw new RuntimeException("Watsonx embed failed", e);
        } finally {
            span.end();
        }
    }

    /**
     * Gets the underlying WatsonxAI client.
     *
     * @return the wrapped WatsonxAI client
     */
    public Object unwrap() {
        return client;
    }

    // Helper methods for extracting token counts

    private void extractAndSetTokenCounts(Span span, Object obj) {
        // Try to extract input_token_count and generated_token_count
        Object inputTokenCount = extractFieldObject(obj, "inputTokenCount");
        if (inputTokenCount == null) {
            inputTokenCount = extractFieldObject(obj, "input_token_count");
        }

        Object generatedTokenCount = extractFieldObject(obj, "generatedTokenCount");
        if (generatedTokenCount == null) {
            generatedTokenCount = extractFieldObject(obj, "generated_token_count");
        }

        if (inputTokenCount != null || generatedTokenCount != null) {
            int input = inputTokenCount != null ? ((Number) inputTokenCount).intValue() : 0;
            int output = generatedTokenCount != null ? ((Number) generatedTokenCount).intValue() : 0;
            tracer.setTokenCounts(span, input, output, input + output);
        }
    }

    private String extractChatMessageContent(Object message) {
        // Try to get content directly
        String content = extractField(message, "content");
        if (content != null) {
            return content;
        }

        // Try to get from content list (if it's a list of content blocks)
        List<?> contentList = extractFieldList(message, "content");
        if (contentList != null && !contentList.isEmpty()) {
            StringBuilder sb = new StringBuilder();
            for (Object block : contentList) {
                if (block instanceof String) {
                    sb.append(block);
                } else {
                    String text = extractField(block, "text");
                    if (text != null) {
                        sb.append(text);
                    }
                }
            }
            return sb.toString();
        }

        return null;
    }

    // Reflection utility methods

    private String extractField(Object obj, String fieldName) {
        Object value = extractFieldObject(obj, fieldName);
        return value != null ? value.toString() : null;
    }

    private Object extractFieldObject(Object obj, String fieldName) {
        if (obj == null) return null;
        try {
            // Try getter method first (getFieldName)
            String getterName = "get" + Character.toUpperCase(fieldName.charAt(0)) + fieldName.substring(1);
            try {
                return obj.getClass().getMethod(getterName).invoke(obj);
            } catch (NoSuchMethodException e) {
                // Try direct method (fieldName())
                try {
                    return obj.getClass().getMethod(fieldName).invoke(obj);
                } catch (NoSuchMethodException e2) {
                    // Try snake_case getter (for fields like input_token_count)
                    String snakeGetterName = "get" + toCamelCase(fieldName);
                    try {
                        return obj.getClass().getMethod(snakeGetterName).invoke(obj);
                    } catch (NoSuchMethodException e3) {
                        // Try field access
                        try {
                            var field = obj.getClass().getDeclaredField(fieldName);
                            field.setAccessible(true);
                            return field.get(obj);
                        } catch (NoSuchFieldException e4) {
                            return null;
                        }
                    }
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

    private String toCamelCase(String snakeCase) {
        StringBuilder result = new StringBuilder();
        boolean capitalizeNext = true;
        for (char c : snakeCase.toCharArray()) {
            if (c == '_') {
                capitalizeNext = true;
            } else if (capitalizeNext) {
                result.append(Character.toUpperCase(c));
                capitalizeNext = false;
            } else {
                result.append(c);
            }
        }
        return result.toString();
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
