package ai.traceai.spring;

import ai.traceai.*;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.context.Scope;
import org.springframework.ai.chat.model.ChatModel;
import org.springframework.ai.chat.model.ChatResponse;
import org.springframework.ai.chat.prompt.ChatOptions;
import org.springframework.ai.chat.prompt.Prompt;
import org.springframework.ai.chat.messages.Message;
import reactor.core.publisher.Flux;

import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.Map;

/**
 * Traced wrapper for Spring AI ChatModel.
 * Implements the ChatModel interface to provide automatic tracing.
 *
 * <p>Usage:</p>
 * <pre>
 * ChatModel model = new OpenAiChatModel(api);
 * ChatModel tracedModel = new TracedChatModel(model, "openai");
 *
 * ChatResponse response = tracedModel.call(new Prompt("Hello!"));
 * </pre>
 */
public class TracedChatModel implements ChatModel {

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
        this.delegate = delegate;
        this.tracer = tracer;
        this.provider = provider;
    }

    /**
     * Creates a new traced chat model using the global TraceAI tracer.
     *
     * @param delegate the underlying model to wrap
     * @param provider the provider name
     */
    public TracedChatModel(ChatModel delegate, String provider) {
        this(delegate, TraceAI.getTracer(), provider);
    }

    @Override
    public ChatResponse call(Prompt prompt) {
        Span span = tracer.startSpan("Spring AI Chat", FISpanKind.LLM);

        try (Scope scope = span.makeCurrent()) {
            // Set system attributes
            span.setAttribute(SemanticConventions.LLM_SYSTEM, "spring-ai");
            span.setAttribute(SemanticConventions.LLM_PROVIDER, provider);

            // Capture input messages
            if (prompt != null && prompt.getInstructions() != null) {
                List<Message> messages = prompt.getInstructions();
                List<Map<String, String>> inputMessages = new ArrayList<>();
                for (Message msg : messages) {
                    String role = msg.getMessageType().getValue();
                    String content = msg.getContent();
                    inputMessages.add(FITracer.message(role, content));
                }
                tracer.setInputMessages(span, inputMessages);
            }

            // Capture prompt options if available
            if (prompt != null && prompt.getOptions() != null) {
                var options = prompt.getOptions();
                if (options.getModel() != null) {
                    span.setAttribute(SemanticConventions.LLM_MODEL_NAME, options.getModel());
                    span.setAttribute(SemanticConventions.LLM_REQUEST_MODEL, options.getModel());
                }
                if (options.getTemperature() != null) {
                    span.setAttribute(SemanticConventions.LLM_REQUEST_TEMPERATURE, options.getTemperature());
                }
                if (options.getTopP() != null) {
                    span.setAttribute(SemanticConventions.LLM_REQUEST_TOP_P, options.getTopP());
                }
            }

            // Execute call
            ChatResponse response = delegate.call(prompt);

            // Capture output
            if (response != null && response.getResult() != null) {
                var output = response.getResult().getOutput();
                if (output != null) {
                    tracer.setOutputValue(span, output.getContent());
                    tracer.setOutputMessages(span, Collections.singletonList(FITracer.message("assistant", output.getContent())));
                }
            }

            // Token usage
            if (response != null && response.getMetadata() != null && response.getMetadata().getUsage() != null) {
                var usage = response.getMetadata().getUsage();
                tracer.setTokenCounts(
                    span,
                    usage.getPromptTokens(),
                    usage.getGenerationTokens(),
                    usage.getTotalTokens()
                );
            }

            span.setStatus(StatusCode.OK);
            return response;

        } catch (Exception e) {
            tracer.setError(span, e);
            throw e;
        } finally {
            span.end();
        }
    }

    @Override
    public Flux<ChatResponse> stream(Prompt prompt) {
        Span span = tracer.startSpan("Spring AI Chat (Stream)", FISpanKind.LLM);

        try {
            // Set system attributes
            span.setAttribute(SemanticConventions.LLM_SYSTEM, "spring-ai");
            span.setAttribute(SemanticConventions.LLM_PROVIDER, provider);

            // Capture input messages
            if (prompt != null && prompt.getInstructions() != null) {
                List<Message> messages = prompt.getInstructions();
                List<Map<String, String>> inputMessages = new ArrayList<>();
                for (Message msg : messages) {
                    String role = msg.getMessageType().getValue();
                    String content = msg.getContent();
                    inputMessages.add(FITracer.message(role, content));
                }
                tracer.setInputMessages(span, inputMessages);
            }

            // Get the stream
            Flux<ChatResponse> stream = delegate.stream(prompt);

            // Wrap the stream to capture output
            StringBuilder content = new StringBuilder();

            return stream
                .doOnNext(response -> {
                    if (response != null && response.getResult() != null) {
                        var output = response.getResult().getOutput();
                        if (output != null && output.getContent() != null) {
                            content.append(output.getContent());
                        }
                    }
                })
                .doOnComplete(() -> {
                    tracer.setOutputValue(span, content.toString());
                    tracer.setOutputMessages(span, Collections.singletonList(FITracer.message("assistant", content.toString())));
                    span.setStatus(StatusCode.OK);
                    span.end();
                })
                .doOnError(e -> {
                    tracer.setError(span, e);
                    span.end();
                });

        } catch (Exception e) {
            tracer.setError(span, e);
            span.end();
            throw e;
        }
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
}
