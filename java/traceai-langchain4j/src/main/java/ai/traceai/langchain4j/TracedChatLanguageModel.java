package ai.traceai.langchain4j;

import ai.traceai.FISpanKind;
import ai.traceai.FITracer;
import ai.traceai.SemanticConventions;
import ai.traceai.TraceAI;
import dev.langchain4j.agent.tool.ToolSpecification;
import dev.langchain4j.data.message.AiMessage;
import dev.langchain4j.data.message.ChatMessage;
import dev.langchain4j.data.message.UserMessage;
import dev.langchain4j.model.chat.ChatLanguageModel;
import dev.langchain4j.model.output.Response;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.context.Context;
import io.opentelemetry.context.Scope;

import java.util.List;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.function.Supplier;

/**
 * Compatibility wrapper for the LangChain4j 0.35 ChatLanguageModel API.
 *
 * @deprecated Attach {@link LangChain4jListener} to the model builder instead. The
 * listener also works with current ChatModel/StreamingChatModel implementations;
 * this legacy class requires the old ChatLanguageModel interface at runtime.
 */
@Deprecated
public class TracedChatLanguageModel implements ChatLanguageModel {
    private static final AtomicBoolean WARNED = new AtomicBoolean();
    private final ChatLanguageModel delegate;
    private final FITracer tracer;
    private final String provider;

    /**
     * Creates a compatibility wrapper.
     * @param delegate underlying model
     * @param tracer instrumentation tracer
     * @param provider model provider name
     */
    public TracedChatLanguageModel(ChatLanguageModel delegate, FITracer tracer, String provider) {
        this.delegate = delegate;
        this.tracer = tracer;
        this.provider = provider;
        if (WARNED.compareAndSet(false, true)) {
            System.getLogger(TracedChatLanguageModel.class.getName()).log(System.Logger.Level.WARNING,
                    "TracedChatLanguageModel is deprecated; attach LangChain4jListener through model builder.listeners(...) instead. "
                            + "The compatibility wrapper remains available for LangChain4j 0.35.");
        }
    }

    /**
     * Creates a compatibility wrapper using the global TraceAI tracer.
     * @param delegate underlying model
     * @param provider model provider name
     */
    public TracedChatLanguageModel(ChatLanguageModel delegate, String provider) {
        this(delegate, TraceAI.getTracer(), provider);
    }

    @Override
    public Response<AiMessage> generate(List<ChatMessage> messages) {
        return trace(messages, () -> delegate.generate(messages));
    }

    @Override
    public Response<AiMessage> generate(List<ChatMessage> messages, List<ToolSpecification> tools) {
        return trace(messages, () -> delegate.generate(messages, tools));
    }

    @Override
    public Response<AiMessage> generate(List<ChatMessage> messages, ToolSpecification tool) {
        return trace(messages, () -> delegate.generate(messages, tool));
    }

    private Response<AiMessage> trace(List<ChatMessage> messages, Supplier<Response<AiMessage>> call) {
        // An inner legacy wrapper or model listener must not trace the same call again.
        if (Boolean.TRUE.equals(Context.current().get(LangChain4jListener.LEGACY_SPAN))) return call.get();
        Span span = tracer.startSpan("LangChain4j Chat", FISpanKind.LLM);
        try (Scope scope = Context.current().with(span).with(LangChain4jListener.LEGACY_SPAN, true).makeCurrent()) {
            span.setAttribute(SemanticConventions.LLM_PROVIDER, provider);
            ChatSpanAttributes.input(tracer, span, messages);
            Response<AiMessage> response = call.get();
            if (response != null) {
                ChatSpanAttributes.output(tracer, span, response.content());
                ChatSpanAttributes.usage(span, response.tokenUsage(), response.finishReason());
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
        Response<AiMessage> response = generate(List.of(UserMessage.from(text)));
        return response.content() != null ? response.content().text() : null;
    }

    /**
     * Returns the original model.
     * @return the original model
     */
    public ChatLanguageModel unwrap() { return delegate; }
}
