package ai.traceai.spring;

import ai.traceai.FISpanKind;
import ai.traceai.FITracer;
import ai.traceai.SemanticConventions;
import ai.traceai.TraceAI;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.context.Context;
import io.opentelemetry.context.Scope;
import org.springframework.ai.chat.messages.AssistantMessage;
import org.springframework.ai.chat.messages.Message;
import org.springframework.ai.chat.metadata.ChatResponseMetadata;
import org.springframework.ai.chat.metadata.Usage;
import org.springframework.ai.chat.model.ChatModel;
import org.springframework.ai.chat.model.ChatResponse;
import org.springframework.ai.chat.model.Generation;
import org.springframework.ai.chat.prompt.ChatOptions;
import org.springframework.ai.chat.prompt.Prompt;
import reactor.core.CoreSubscriber;
import reactor.core.publisher.Flux;

import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.Map;
import java.util.Objects;

/**
 * Traced wrapper for a Spring AI 1.1.x {@link ChatModel}.
 *
 * <p>The wrapper passes the exact {@link Prompt} to the delegate and returns the delegate's exact
 * {@link ChatResponse} (or stream elements). It never retries, substitutes or caches responses.</p>
 *
 * <p>Usage:</p>
 * <pre>
 * ChatModel model = OllamaChatModel.builder()...build();
 * ChatModel tracedModel = new TracedChatModel(model, tracer, "ollama");
 *
 * ChatResponse response = tracedModel.call(new Prompt("Hello!"));
 * </pre>
 *
 * <p>Streaming: {@link #stream(Prompt)} starts no span and does not call the delegate until a subscriber
 * subscribes. Each subscription owns one span that ends exactly once on complete, error or cancel. Streamed
 * token usage is not aggregated. To bind a parent span and TraceAI session/user attributes to a stream that
 * is subscribed on another thread, use {@link SpringAITracingContext#withContext}.</p>
 */
public class TracedChatModel implements ChatModel {

    static final String SPAN_NAME_CALL = "Spring AI Chat";
    static final String SPAN_NAME_STREAM = "Spring AI Chat (Stream)";

    private final ChatModel delegate;
    private final FITracer tracer;
    private final String provider;

    /**
     * Creates a new traced chat model.
     *
     * @param delegate the underlying model to wrap
     * @param tracer   the FITracer for instrumentation
     * @param provider the provider name (e.g., "openai", "anthropic")
     */
    public TracedChatModel(ChatModel delegate, FITracer tracer, String provider) {
        this.delegate = Objects.requireNonNull(delegate, "delegate ChatModel must not be null");
        this.tracer = Objects.requireNonNull(tracer, "FITracer must not be null");
        this.provider = provider;
    }

    /**
     * Creates a new traced chat model using the global TraceAI tracer.
     *
     * @param delegate the underlying model to wrap
     * @param provider the provider name
     * @throws IllegalStateException if {@link TraceAI#init} has not been called
     */
    public TracedChatModel(ChatModel delegate, String provider) {
        this(delegate, TraceAI.getTracer(), provider);
    }

    @Override
    public ChatResponse call(Prompt prompt) {
        Span span = SpringAITracingContext.startSpan(tracer, SPAN_NAME_CALL, FISpanKind.LLM, SpringAITracingContext.capture());
        boolean completed = false;
        boolean errorRecorded = false;
        try (Scope ignored = span.makeCurrent()) {
            captureRequest(span, prompt);

            ChatResponse response = delegate.call(prompt);

            captureResponse(span, prompt, response);
            span.setStatus(StatusCode.OK);
            completed = true;
            return response;
        } catch (RuntimeException e) {
            errorRecorded = true;
            tracer.setError(span, e);
            throw e;
        } finally {
            if (!completed && !errorRecorded) {
                // Fatal (non-RuntimeException) termination: never report success, let the Error propagate.
                span.setStatus(StatusCode.ERROR, "abnormal termination");
            }
            span.end();
        }
    }

    @Override
    public Flux<ChatResponse> stream(Prompt prompt) {
        SpringAITracingContext.Snapshot assembly = SpringAITracingContext.capture();
        return new TracedStream(prompt, assembly);
    }

    @Override
    public ChatOptions getDefaultOptions() {
        return delegate.getDefaultOptions();
    }

    /**
     * Gets the underlying model.
     *
     * @return the wrapped ChatModel
     */
    public ChatModel unwrap() {
        return delegate;
    }

    // ---------------------------------------------------------------------------------------------
    // Capture helpers (null-safe for every Spring AI accessor the wrapper reads)
    // ---------------------------------------------------------------------------------------------

    private void captureRequest(Span span, Prompt prompt) {
        span.setAttribute(SemanticConventions.LLM_SYSTEM, "spring-ai");
        if (provider != null) {
            span.setAttribute(SemanticConventions.LLM_PROVIDER, provider);
        }
        if (prompt == null) {
            return;
        }
        List<Message> messages = prompt.getInstructions();
        if (messages != null && !messages.isEmpty()) {
            List<Map<String, String>> inputMessages = new ArrayList<>(messages.size());
            StringBuilder inputValue = new StringBuilder();
            for (Message message : messages) {
                if (message == null) {
                    continue;
                }
                String role = message.getMessageType() != null ? message.getMessageType().getValue() : null;
                String text = message.getText();
                inputMessages.add(FITracer.message(role, text));
                if (text != null) {
                    if (inputValue.length() > 0) {
                        inputValue.append('\n');
                    }
                    inputValue.append(text);
                }
            }
            tracer.setInputMessages(span, inputMessages);
            tracer.setInputValue(span, inputValue.toString());
        }
        ChatOptions options = prompt.getOptions();
        if (options != null) {
            if (options.getModel() != null) {
                span.setAttribute(SemanticConventions.LLM_REQUEST_MODEL, options.getModel());
            }
            if (options.getTemperature() != null) {
                span.setAttribute(SemanticConventions.LLM_REQUEST_TEMPERATURE, options.getTemperature());
            }
            if (options.getTopP() != null) {
                span.setAttribute(SemanticConventions.LLM_REQUEST_TOP_P, options.getTopP());
            }
            if (options.getMaxTokens() != null) {
                span.setAttribute(SemanticConventions.LLM_REQUEST_MAX_TOKENS, options.getMaxTokens().longValue());
            }
        }
    }

    private void captureResponse(Span span, Prompt prompt, ChatResponse response) {
        if (response == null) {
            return;
        }
        ChatResponseMetadata metadata = response.getMetadata();
        if (metadata != null) {
            String responseModel = metadata.getModel();
            if (responseModel != null && !responseModel.isEmpty()) {
                span.setAttribute(SemanticConventions.LLM_RESPONSE_MODEL, responseModel);
                boolean requestModelKnown = prompt != null && prompt.getOptions() != null && prompt.getOptions().getModel() != null;
                if (!requestModelKnown) {
                    span.setAttribute(SemanticConventions.LLM_REQUEST_MODEL, responseModel);
                }
            }
        }

        Generation generation = response.getResult();
        if (generation != null) {
            AssistantMessage output = generation.getOutput();
            if (output != null) {
                String text = output.getText();
                if (text != null) {
                    tracer.setOutputValue(span, text);
                    tracer.setOutputMessages(span, Collections.singletonList(FITracer.message("assistant", text)));
                }
            }
        }

        if (metadata != null) {
            applyUsage(span, metadata.getUsage());
        }
    }

    /**
     * Reads the three boxed counts once and sets each attribute only when the provider supplied it.
     * Zero is preserved; missing counts stay absent (never synthesized).
     *
     * <p>Spring AI's {@code EmptyUsage} (the default when metadata carries no usage) returns zero for every
     * count and an empty map from {@code getNativeUsage()}. That is "no usage reported", not a measured zero,
     * so it is recorded as absent. A real measured zero keeps a provider-native usage object.</p>
     */
    static void applyUsage(Span span, Usage usage) {
        if (usage == null) {
            return;
        }
        Integer prompt = usage.getPromptTokens();
        Integer completion = usage.getCompletionTokens();
        Integer total = usage.getTotalTokens();
        Object nativeUsage = usage.getNativeUsage();
        boolean emptyPlaceholder = nativeUsage instanceof java.util.Map && ((java.util.Map<?, ?>) nativeUsage).isEmpty()
            && Integer.valueOf(0).equals(prompt) && Integer.valueOf(0).equals(completion) && Integer.valueOf(0).equals(total);
        if (emptyPlaceholder) {
            return;
        }
        if (prompt != null) {
            span.setAttribute(SemanticConventions.LLM_TOKEN_COUNT_PROMPT, prompt.longValue());
        }
        if (completion != null) {
            span.setAttribute(SemanticConventions.LLM_TOKEN_COUNT_COMPLETION, completion.longValue());
        }
        if (total != null) {
            span.setAttribute(SemanticConventions.LLM_TOKEN_COUNT_TOTAL, total.longValue());
        }
    }

    // ---------------------------------------------------------------------------------------------
    // Deferred, per-subscription traced stream
    // ---------------------------------------------------------------------------------------------

    private final class TracedStream extends Flux<ChatResponse> {
        private final Prompt prompt;
        private final SpringAITracingContext.Snapshot assembly;

        TracedStream(Prompt prompt, SpringAITracingContext.Snapshot assembly) {
            this.prompt = prompt;
            this.assembly = assembly;
        }

        @Override
        public void subscribe(CoreSubscriber<? super ChatResponse> actual) {
            SpringAITracingContext.Snapshot selected = SpringAITracingContext.select(actual.currentContext(), assembly);
            Span span = SpringAITracingContext.startSpan(tracer, SPAN_NAME_STREAM, FISpanKind.LLM, selected);
            TracingSubscriber subscriber = new TracingSubscriber(actual, tracer, span, new StreamOutputCapture(tracer));
            try (Scope ignored = Context.current().with(span).makeCurrent()) {
                captureRequest(span, prompt);
                Flux<ChatResponse> upstream = delegate.stream(prompt);
                if (upstream == null) {
                    throw new NullPointerException("delegate.stream(prompt) returned null");
                }
                upstream.subscribe(subscriber);
            } catch (RuntimeException e) {
                subscriber.failBeforeSubscription(e);
            } catch (Error e) {
                // Fatal creation/subscribe failure: close the span without claiming success, then propagate.
                if (subscriber.state() == TracingSubscriber.OPEN) {
                    span.setStatus(StatusCode.ERROR, "abnormal termination");
                    span.end();
                }
                throw e;
            }
        }
    }
}
