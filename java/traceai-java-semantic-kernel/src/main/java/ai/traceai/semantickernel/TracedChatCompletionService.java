package ai.traceai.semantickernel;

import ai.traceai.FISpanKind;
import ai.traceai.FITracer;
import ai.traceai.SemanticConventions;
import ai.traceai.TraceAI;
import com.microsoft.semantickernel.Kernel;
import com.microsoft.semantickernel.orchestration.FunctionResultMetadata;
import com.microsoft.semantickernel.orchestration.InvocationContext;
import com.microsoft.semantickernel.orchestration.PromptExecutionSettings;
import com.microsoft.semantickernel.services.chatcompletion.AuthorRole;
import com.microsoft.semantickernel.services.chatcompletion.ChatCompletionService;
import com.microsoft.semantickernel.services.chatcompletion.ChatHistory;
import com.microsoft.semantickernel.services.chatcompletion.ChatMessageContent;
import com.microsoft.semantickernel.services.chatcompletion.StreamingChatContent;
import io.opentelemetry.api.trace.Span;
import reactor.core.publisher.Flux;
import reactor.core.publisher.Mono;

import javax.annotation.Nullable;
import java.util.ArrayList;
import java.util.Collections;
import java.util.IdentityHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;

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
    private static final int MAX_STREAM_CONTENT = 32000;

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

    /**
     * Traces one chat request per subscription.
     *
     * <p>Semantic Kernel's native spans own model calls and tokens. When native spans are present,
     * this boundary is a CHAIN that captures content and covers cancellation. For custom services
     * without native telemetry it remains an LLM span and uses returned usage metadata.</p>
     */
    @Override
    public Mono<List<ChatMessageContent<?>>> getChatMessageContentsAsync(
            ChatHistory chatHistory,
            @Nullable Kernel kernel,
            @Nullable InvocationContext invocationContext) {

        return ReactiveTracing.mono(tracer, "Semantic Kernel Chat Completion", FISpanKind.LLM, true,
            invocation -> {
                Span span = invocation.span();
                setModelAttributes(span);
                if (invocationContext != null && invocationContext.getPromptExecutionSettings() != null) {
                    captureExecutionSettings(span, invocationContext.getPromptExecutionSettings());
                }
                captureInputMessages(span, chatHistory);
                return delegate.getChatMessageContentsAsync(
                    chatHistory, kernel, invocation.route(invocationContext));
            },
            (invocation, messages) -> captureOutputMessages(
                invocation.span(), messages, !invocation.hasNativeModel()));
    }

    /**
     * Traces one streaming chat request per subscription. The span ends when the stream
     * completes, fails or is cancelled; received text is recorded as the output.
     */
    @Override
    public Flux<StreamingChatContent<?>> getStreamingChatMessageContentsAsync(
            ChatHistory chatHistory,
            @Nullable Kernel kernel,
            @Nullable InvocationContext invocationContext) {

        return ReactiveTracing.flux(tracer, "Semantic Kernel Chat Completion (Stream)", FISpanKind.LLM, true,
            invocation -> {
                Span span = invocation.span();
                setModelAttributes(span);
                if (invocationContext != null && invocationContext.getPromptExecutionSettings() != null) {
                    captureExecutionSettings(span, invocationContext.getPromptExecutionSettings());
                }
                captureInputMessages(span, chatHistory);

                StringBuilder content = new StringBuilder();
                invocation.onFinish(signal -> {
                    String fullContent;
                    synchronized (content) {
                        fullContent = content.toString();
                    }
                    if (!fullContent.isEmpty()) {
                        tracer.setOutputValue(span, fullContent);
                        tracer.setOutputMessages(span,
                            Collections.singletonList(FITracer.message("assistant", fullContent)));
                    }
                });
                return delegate.getStreamingChatMessageContentsAsync(
                        chatHistory, kernel, invocation.route(invocationContext))
                    .doOnNext(message -> {
                        if (!tracer.getConfig().isHideOutputs()
                                && message != null && message.getContent() != null) {
                            synchronized (content) {
                                String chunk = message.getContent();
                                int remaining = MAX_STREAM_CONTENT - content.length();
                                if (remaining > 0) {
                                    content.append(chunk, 0, Math.min(remaining, chunk.length()));
                                }
                                if (chunk.length() > remaining) {
                                    span.setAttribute("semantic_kernel.output_truncated", true);
                                }
                            }
                        }
                    });
            });
    }

    @Override
    public Mono<List<ChatMessageContent<?>>> getChatMessageContentsAsync(
            String prompt,
            @Nullable Kernel kernel,
            @Nullable InvocationContext invocationContext) {

        ChatHistory chatHistory = new ChatHistory();
        chatHistory.addUserMessage(prompt);
        return getChatMessageContentsAsync(chatHistory, kernel, invocationContext);
    }

    @Override
    public Flux<StreamingChatContent<?>> getStreamingChatMessageContentsAsync(
            String prompt,
            @Nullable Kernel kernel,
            @Nullable InvocationContext invocationContext) {

        ChatHistory chatHistory = new ChatHistory();
        chatHistory.addUserMessage(prompt);
        return getStreamingChatMessageContentsAsync(chatHistory, kernel, invocationContext);
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

    private void setModelAttributes(Span span) {
        span.setAttribute(SemanticConventions.LLM_SYSTEM, LLM_SYSTEM);
        if (provider != null) {
            span.setAttribute(SemanticConventions.LLM_PROVIDER, provider);
        }
        if (modelName != null) {
            span.setAttribute(SemanticConventions.LLM_MODEL_NAME, modelName);
            span.setAttribute(SemanticConventions.LLM_REQUEST_MODEL, modelName);
        }
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

        // getMaxTokens returns primitive int on 1.3 but Integer on 1.4+. The method
        // descriptors differ, so a direct call would fail to link for existing 1.3 users.
        try {
            Object maxTokens = settings.getClass().getMethod("getMaxTokens").invoke(settings);
            if (maxTokens instanceof Number) {
                span.setAttribute(SemanticConventions.LLM_REQUEST_MAX_TOKENS,
                    ((Number) maxTokens).longValue());
            }
        } catch (ReflectiveOperationException ignored) {
            // An unavailable optional setting must not affect model execution.
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

    private void captureOutputMessages(Span span, List<ChatMessageContent<?>> messages, boolean captureUsage) {
        if (messages == null || messages.isEmpty()) {
            return;
        }

        StringBuilder outputBuilder = new StringBuilder();
        List<Map<String, String>> outputMessages = new ArrayList<>();
        Set<Object> usages = Collections.newSetFromMap(new IdentityHashMap<>());

        for (ChatMessageContent<?> message : messages) {
            String role = mapRole(message.getAuthorRole());
            String content = message.getContent();

            outputMessages.add(FITracer.message(role, content));

            if (outputBuilder.length() > 0) {
                outputBuilder.append("\n");
            }
            outputBuilder.append(content != null ? content : "");

            Object usage = usageOf(message.getMetadata());
            if (usage != null) {
                usages.add(usage);
            }
        }

        // Messages from one model response share its usage record. Count each record once, so
        // automatic tool-call rounds add up without counting a response per returned message.
        if (captureUsage) {
            recordTokenUsage(span, usages);
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

    private Object usageOf(Object metadata) {
        if (metadata instanceof FunctionResultMetadata) {
            return ((FunctionResultMetadata<?>) metadata).getUsage();
        }
        if (metadata instanceof Map) {
            Map<?, ?> metaMap = (Map<?, ?>) metadata;
            Object usage = metaMap.get("usage");
            if (usage != null) {
                return usage;
            }
            if (metaMap.get("promptTokens") instanceof Number
                    && metaMap.get("completionTokens") instanceof Number) {
                return metaMap;
            }
        }
        return null;
    }

    private void recordTokenUsage(Span span, Set<Object> usages) {
        long prompt = 0;
        long completion = 0;
        long total = 0;
        boolean found = false;
        for (Object usage : usages) {
            Number promptTokens = firstNumber(usage, "promptTokens", "inputTokens");
            Number completionTokens = firstNumber(usage, "completionTokens", "outputTokens");
            if (promptTokens == null || completionTokens == null) {
                continue;
            }
            Number totalTokens = firstNumber(usage, "totalTokens");
            prompt += promptTokens.longValue();
            completion += completionTokens.longValue();
            total += totalTokens != null
                ? totalTokens.longValue() : promptTokens.longValue() + completionTokens.longValue();
            found = true;
        }
        if (found) {
            tracer.setTokenCounts(span, prompt, completion, total);
        }
    }

    private Number firstNumber(Object usage, String... fieldNames) {
        for (String fieldName : fieldNames) {
            Number value = usage instanceof Map
                ? asNumber(((Map<?, ?>) usage).get(fieldName))
                : extractField(usage, fieldName, Number.class);
            if (value != null) {
                return value;
            }
        }
        return null;
    }

    private static Number asNumber(Object value) {
        return value instanceof Number ? (Number) value : null;
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
