package ai.traceai.langchain4j;

import ai.traceai.*;
import dev.langchain4j.data.message.*;
import dev.langchain4j.model.chat.ChatLanguageModel;
import dev.langchain4j.model.output.Response;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.context.Scope;

import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.Map;

/**
 * Traced wrapper for LangChain4j ChatLanguageModel.
 * Implements the ChatLanguageModel interface to provide automatic tracing.
 *
 * <p>Usage:</p>
 * <pre>
 * ChatLanguageModel model = OpenAiChatModel.builder()
 *     .apiKey("...")
 *     .modelName("gpt-4")
 *     .build();
 *
 * ChatLanguageModel tracedModel = new TracedChatLanguageModel(model, "openai");
 *
 * Response&lt;AiMessage&gt; response = tracedModel.generate(
 *     List.of(UserMessage.from("Hello!"))
 * );
 * </pre>
 */
public class TracedChatLanguageModel implements ChatLanguageModel {

    private final ChatLanguageModel delegate;
    private final FITracer tracer;
    private final String provider;

    /**
     * Creates a new traced chat language model.
     *
     * @param delegate the underlying model to wrap
     * @param tracer   the FITracer for instrumentation
     * @param provider the provider name (e.g., "openai", "anthropic")
     */
    public TracedChatLanguageModel(ChatLanguageModel delegate, FITracer tracer, String provider) {
        this.delegate = delegate;
        this.tracer = tracer;
        this.provider = provider;
    }

    /**
     * Creates a new traced chat language model using the global TraceAI tracer.
     *
     * @param delegate the underlying model to wrap
     * @param provider the provider name
     */
    public TracedChatLanguageModel(ChatLanguageModel delegate, String provider) {
        this(delegate, TraceAI.getTracer(), provider);
    }

    @Override
    public Response<AiMessage> generate(List<ChatMessage> messages) {
        Span span = tracer.startSpan("LangChain4j Chat", FISpanKind.LLM);

        try (Scope scope = span.makeCurrent()) {
            // Set system attributes
            span.setAttribute(SemanticConventions.LLM_SYSTEM, "langchain4j");
            span.setAttribute(SemanticConventions.LLM_PROVIDER, provider);

            // Capture input messages
            List<Map<String, String>> inputMessages = new ArrayList<>();
            for (ChatMessage msg : messages) {
                String role = getRole(msg);
                String content = getText(msg);
                inputMessages.add(FITracer.message(role, content));
            }
            tracer.setInputMessages(span, inputMessages);

            // Execute generation
            Response<AiMessage> response = delegate.generate(messages);

            // Capture output
            if (response.content() != null) {
                String outputText = response.content().text();
                tracer.setOutputValue(span, outputText);
                tracer.setOutputMessages(span, Collections.singletonList(FITracer.message("assistant", outputText)));

                // Capture tool execution requests if present
                if (response.content().hasToolExecutionRequests()) {
                    List<ToolExecutionRequest> toolRequests = response.content().toolExecutionRequests();
                    for (int i = 0; i < toolRequests.size(); i++) {
                        ToolExecutionRequest request = toolRequests.get(i);
                        span.setAttribute("llm.tool_calls." + i + ".id", request.id());
                        span.setAttribute("llm.tool_calls." + i + ".name", request.name());
                        span.setAttribute("llm.tool_calls." + i + ".arguments", request.arguments());
                    }
                }
            }

            // Token usage
            if (response.tokenUsage() != null) {
                tracer.setTokenCounts(
                    span,
                    response.tokenUsage().inputTokenCount(),
                    response.tokenUsage().outputTokenCount(),
                    response.tokenUsage().totalTokenCount()
                );
            }

            // Finish reason
            if (response.finishReason() != null) {
                span.setAttribute(SemanticConventions.LLM_RESPONSE_FINISH_REASON,
                    response.finishReason().toString());
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
    public String generate(String text) {
        // Delegate to the list-based method for tracing
        Response<AiMessage> response = generate(List.of(UserMessage.from(text)));
        return response.content() != null ? response.content().text() : null;
    }

    /**
     * Gets the underlying model.
     *
     * @return the wrapped ChatLanguageModel
     */
    public ChatLanguageModel unwrap() {
        return delegate;
    }

    private String getRole(ChatMessage message) {
        if (message instanceof UserMessage) return "user";
        if (message instanceof AiMessage) return "assistant";
        if (message instanceof SystemMessage) return "system";
        if (message instanceof ToolExecutionResultMessage) return "tool";
        return "unknown";
    }

    private String getText(ChatMessage message) {
        if (message instanceof UserMessage) {
            UserMessage um = (UserMessage) message;
            // Handle both text and multimodal content
            if (um.hasSingleText()) {
                return um.singleText();
            }
            StringBuilder sb = new StringBuilder();
            um.contents().forEach(content -> {
                if (content instanceof TextContent) {
                    sb.append(((TextContent) content).text());
                }
            });
            return sb.toString();
        }
        if (message instanceof AiMessage) {
            return ((AiMessage) message).text();
        }
        if (message instanceof SystemMessage) {
            return ((SystemMessage) message).text();
        }
        if (message instanceof ToolExecutionResultMessage) {
            return ((ToolExecutionResultMessage) message).text();
        }
        return message.toString();
    }
}
