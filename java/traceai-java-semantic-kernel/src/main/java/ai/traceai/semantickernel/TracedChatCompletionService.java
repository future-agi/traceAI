package ai.traceai.semantickernel;

import ai.traceai.FISpanKind;
import ai.traceai.FITracer;
import ai.traceai.SemanticConventions;
import ai.traceai.TraceAI;
import com.microsoft.semantickernel.Kernel;
import com.microsoft.semantickernel.orchestration.InvocationContext;
import com.microsoft.semantickernel.orchestration.PromptExecutionSettings;
import com.microsoft.semantickernel.services.chatcompletion.AuthorRole;
import com.microsoft.semantickernel.services.chatcompletion.ChatCompletionService;
import com.microsoft.semantickernel.services.chatcompletion.ChatHistory;
import com.microsoft.semantickernel.services.chatcompletion.ChatMessageContent;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.context.Scope;
import reactor.core.publisher.Flux;
import reactor.core.publisher.Mono;

import javax.annotation.Nullable;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.Map;
import java.util.concurrent.atomic.AtomicReference;

/**
 * Instrumentation wrapper for Semantic Kernel ChatCompletionService.
 * Wraps the ChatCompletionService to provide automatic tracing of chat interactions.
 *
 * <p>Usage:</p>
 * <pre>
 * ChatCompletionService chatService = OpenAIChatCompletion.builder()
 *     .withModelId("gpt-4")
 *     .withOpenAIAsyncClient(client)
 *     .build();
 *
 * TracedChatCompletionService tracedService = new TracedChatCompletionService(
 *     chatService, "gpt-4", "openai"
 * );
 *
 * ChatHistory history = new ChatHistory();
 * history.addUserMessage("Hello!");
 *
 * List&lt;ChatMessageContent&lt;?&gt;&gt; response = tracedService.getChatMessageContentsAsync(
 *     history, null, null
 * ).block();
 * </pre>
 */
public class TracedChatCompletionService implements ChatCompletionService {

    private static final String LLM_SYSTEM = "semantic-kernel";

    private final ChatCompletionService delegate;
    private final String modelName;
    private final String provider;
    private final FITracer tracer;

    /**
     * Creates a new traced ChatCompletionService with the given service, model name, provider, and tracer.
     *
     * @param delegate the ChatCompletionService to wrap
     * @param modelName the model name (e.g., "gpt-4", "gpt-3.5-turbo")
     * @param provider the provider name (e.g., "openai", "azure")
     * @param tracer the FITracer for instrumentation
     */
    public TracedChatCompletionService(ChatCompletionService delegate, String modelName, String provider, FITracer tracer) {
        this.delegate = delegate;
        this.modelName = modelName;
        this.provider = provider;
        this.tracer = tracer;
    }

    /**
     * Creates a new traced ChatCompletionService with the given service, model name, and provider.
     * Uses the global TraceAI tracer.
     *
     * @param delegate the ChatCompletionService to wrap
     * @param modelName the model name
     * @param provider the provider name
     */
    public TracedChatCompletionService(ChatCompletionService delegate, String modelName, String provider) {
        this(delegate, modelName, provider, TraceAI.getTracer());
    }

    /**
     * Creates a new traced ChatCompletionService with the given service and model name.
     * Provider defaults to "openai".
     *
     * @param delegate the ChatCompletionService to wrap
     * @param modelName the model name
     */
    public TracedChatCompletionService(ChatCompletionService delegate, String modelName) {
        this(delegate, modelName, "openai");
    }

    @Override
    public Mono<List<ChatMessageContent<?>>> getChatMessageContentsAsync(
            ChatHistory chatHistory,
            @Nullable Kernel kernel,
            @Nullable InvocationContext invocationContext) {

        Span span = tracer.startSpan("Semantic Kernel Chat Completion", FISpanKind.LLM);

        try (Scope scope = span.makeCurrent()) {
            // Set system attributes
            span.setAttribute(SemanticConventions.LLM_SYSTEM, LLM_SYSTEM);
            span.setAttribute(SemanticConventions.LLM_PROVIDER, provider);
            span.setAttribute(SemanticConventions.LLM_MODEL_NAME, modelName);
            span.setAttribute(SemanticConventions.LLM_REQUEST_MODEL, modelName);

            // Extract execution settings if available
            if (invocationContext != null && invocationContext.getPromptExecutionSettings() != null) {
                captureExecutionSettings(span, invocationContext.getPromptExecutionSettings());
            }

            // Capture input messages
            captureInputMessages(span, chatHistory);

            // Execute the request
            return delegate.getChatMessageContentsAsync(chatHistory, kernel, invocationContext)
                .doOnSuccess(messages -> {
                    // Capture output messages
                    captureOutputMessages(span, messages);
                    span.setStatus(StatusCode.OK);
                })
                .doOnError(error -> {
                    tracer.setError(span, error);
                })
                .doFinally(signalType -> {
                    span.end();
                });
        }
    }

    @Override
    public Flux<ChatMessageContent<?>> getStreamingChatMessageContentsAsync(
            ChatHistory chatHistory,
            @Nullable Kernel kernel,
            @Nullable InvocationContext invocationContext) {

        Span span = tracer.startSpan("Semantic Kernel Chat Completion (Stream)", FISpanKind.LLM);

        try (Scope scope = span.makeCurrent()) {
            // Set system attributes
            span.setAttribute(SemanticConventions.LLM_SYSTEM, LLM_SYSTEM);
            span.setAttribute(SemanticConventions.LLM_PROVIDER, provider);
            span.setAttribute(SemanticConventions.LLM_MODEL_NAME, modelName);
            span.setAttribute(SemanticConventions.LLM_REQUEST_MODEL, modelName);

            // Capture input messages
            captureInputMessages(span, chatHistory);

            // Track streaming content
            AtomicReference<StringBuilder> contentBuilder = new AtomicReference<>(new StringBuilder());

            return delegate.getStreamingChatMessageContentsAsync(chatHistory, kernel, invocationContext)
                .doOnNext(message -> {
                    if (message.getContent() != null) {
                        contentBuilder.get().append(message.getContent());
                    }
                })
                .doOnComplete(() -> {
                    String fullContent = contentBuilder.get().toString();
                    if (!fullContent.isEmpty()) {
                        tracer.setOutputValue(span, fullContent);
                        tracer.setOutputMessages(span, Collections.singletonList(FITracer.message("assistant", fullContent)));
                    }
                    span.setStatus(StatusCode.OK);
                })
                .doOnError(error -> {
                    tracer.setError(span, error);
                })
                .doFinally(signalType -> {
                    span.end();
                });
        }
    }

    @Override
    public String getModelId() {
        return delegate.getModelId();
    }

    @Override
    public String getServiceId() {
        return delegate.getServiceId();
    }

    /**
     * Gets the underlying ChatCompletionService.
     *
     * @return the wrapped ChatCompletionService
     */
    public ChatCompletionService unwrap() {
        return delegate;
    }

    /**
     * Gets the tracer being used.
     *
     * @return the FITracer
     */
    public FITracer getTracer() {
        return tracer;
    }

    private void captureExecutionSettings(Span span, PromptExecutionSettings settings) {
        if (settings == null) {
            return;
        }

        Double temperature = settings.getTemperature();
        if (temperature != null) {
            span.setAttribute(SemanticConventions.LLM_REQUEST_TEMPERATURE, temperature);
        }

        Double topP = settings.getTopP();
        if (topP != null) {
            span.setAttribute(SemanticConventions.LLM_REQUEST_TOP_P, topP);
        }

        Integer maxTokens = settings.getMaxTokens();
        if (maxTokens != null) {
            span.setAttribute(SemanticConventions.LLM_REQUEST_MAX_TOKENS, maxTokens.longValue());
        }
    }

    private void captureInputMessages(Span span, ChatHistory chatHistory) {
        if (chatHistory == null) {
            return;
        }

        List<ChatMessageContent<?>> messages = chatHistory.getMessages();
        StringBuilder inputBuilder = new StringBuilder();
        List<Map<String, String>> inputMessages = new ArrayList<>();

        for (ChatMessageContent<?> message : messages) {
            String role = mapRole(message.getAuthorRole());
            String content = message.getContent();

            inputMessages.add(FITracer.message(role, content));

            if (inputBuilder.length() > 0) {
                inputBuilder.append("\n");
            }
            inputBuilder.append(role).append(": ").append(content);
        }

        tracer.setInputMessages(span, inputMessages);

        if (inputBuilder.length() > 0) {
            tracer.setInputValue(span, inputBuilder.toString());
        }

        // Capture raw input
        tracer.setRawInput(span, messages);
    }

    private void captureOutputMessages(Span span, List<ChatMessageContent<?>> messages) {
        if (messages == null || messages.isEmpty()) {
            return;
        }

        StringBuilder outputBuilder = new StringBuilder();
        List<Map<String, String>> outputMessages = new ArrayList<>();

        for (ChatMessageContent<?> message : messages) {
            String role = mapRole(message.getAuthorRole());
            String content = message.getContent();

            outputMessages.add(FITracer.message(role, content));

            if (outputBuilder.length() > 0) {
                outputBuilder.append("\n");
            }
            outputBuilder.append(content != null ? content : "");

            // Try to extract token usage from metadata
            if (message.getMetadata() != null) {
                extractTokenUsage(span, message.getMetadata());
            }
        }

        tracer.setOutputMessages(span, outputMessages);

        if (outputBuilder.length() > 0) {
            tracer.setOutputValue(span, outputBuilder.toString());
        }

        // Capture raw output
        tracer.setRawOutput(span, messages);
    }

    private String mapRole(AuthorRole role) {
        if (role == null) {
            return "unknown";
        }

        switch (role) {
            case USER:
                return "user";
            case ASSISTANT:
                return "assistant";
            case SYSTEM:
                return "system";
            case TOOL:
                return "tool";
            default:
                return role.toString().toLowerCase();
        }
    }

    private void extractTokenUsage(Span span, Object metadata) {
        if (metadata == null) {
            return;
        }

        try {
            // Try to extract usage from metadata map
            if (metadata instanceof java.util.Map) {
                @SuppressWarnings("unchecked")
                java.util.Map<String, Object> metaMap = (java.util.Map<String, Object>) metadata;

                Object usage = metaMap.get("usage");
                if (usage != null) {
                    extractUsageFromObject(span, usage);
                }

                // Also try direct token fields
                Object promptTokens = metaMap.get("promptTokens");
                Object completionTokens = metaMap.get("completionTokens");
                Object totalTokens = metaMap.get("totalTokens");

                if (promptTokens != null && completionTokens != null) {
                    int prompt = ((Number) promptTokens).intValue();
                    int completion = ((Number) completionTokens).intValue();
                    int total = totalTokens != null ? ((Number) totalTokens).intValue() : (prompt + completion);
                    tracer.setTokenCounts(span, prompt, completion, total);
                }
            }
        } catch (Exception e) {
            // Ignore token extraction failures
        }
    }

    private void extractUsageFromObject(Span span, Object usage) {
        try {
            Integer promptTokens = extractField(usage, "promptTokens", Integer.class);
            Integer completionTokens = extractField(usage, "completionTokens", Integer.class);
            Integer totalTokens = extractField(usage, "totalTokens", Integer.class);

            if (promptTokens != null && completionTokens != null) {
                int total = totalTokens != null ? totalTokens : (promptTokens + completionTokens);
                tracer.setTokenCounts(span, promptTokens, completionTokens, total);
            }
        } catch (Exception e) {
            // Ignore extraction failures
        }
    }

    @SuppressWarnings("unchecked")
    private <T> T extractField(Object obj, String fieldName, Class<T> type) {
        if (obj == null) {
            return null;
        }

        try {
            String getterName = "get" + Character.toUpperCase(fieldName.charAt(0)) + fieldName.substring(1);
            try {
                Object value = obj.getClass().getMethod(getterName).invoke(obj);
                return type.isInstance(value) ? (T) value : null;
            } catch (NoSuchMethodException e) {
                try {
                    Object value = obj.getClass().getMethod(fieldName).invoke(obj);
                    return type.isInstance(value) ? (T) value : null;
                } catch (NoSuchMethodException e2) {
                    return null;
                }
            }
        } catch (Exception e) {
            return null;
        }
    }
}
