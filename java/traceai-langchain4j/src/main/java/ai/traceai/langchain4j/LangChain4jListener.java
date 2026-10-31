package ai.traceai.langchain4j;

import ai.traceai.FISpanKind;
import ai.traceai.FITracer;
import ai.traceai.SemanticConventions;
import ai.traceai.TraceAI;
import dev.langchain4j.model.chat.listener.ChatModelErrorContext;
import dev.langchain4j.model.chat.listener.ChatModelListener;
import dev.langchain4j.model.chat.listener.ChatModelRequestContext;
import dev.langchain4j.model.chat.listener.ChatModelResponseContext;
import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.api.common.Attributes;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.context.Context;
import io.opentelemetry.context.ContextKey;

import java.util.Map;
import java.util.Objects;

/**
 * Explicitly registered official model listener for LangChain4j 0.35 and 1.x.
 * Attach one instance with {@code builder.listeners(List.of(listener))} to a
 * model that supports the official listener API. Works for synchronous requests
 * and streaming/async requests that report a terminal response or error.
 *
 * <p>Content and error details are off by default. Opt-in capture still respects
 * all FITracer hide flags. No global SDK is installed and no model is wrapped.
 * Use a batch span processor in production so callbacks do not perform network I/O.</p>
 *
 * <p>State lives in the framework's per-request attribute map, not thread locals.
 * The parent is captured at onRequest; no scope is left open across threads.
 * This observes model calls, not service/tool execution. A cancelled or abandoned
 * request for which the framework emits no terminal callback cannot be finished
 * by this SPI. Do not combine with another model tracing integration.</p>
 */
public final class LangChain4jListener implements ChatModelListener {
    static final ContextKey<Boolean> LEGACY_SPAN = ContextKey.named("traceai.langchain4j.legacy");
    // Shared across instances: accidental duplicate registration still emits only one span.
    private static final Object STATE_KEY = new Object();
    private final FITracer tracer;
    private final String provider;
    private final boolean captureContent;

    /**
     * Creates a metadata-only listener using the initialized TraceAI tracer.
     * @param provider model provider name, not the framework name
     */
    public LangChain4jListener(String provider) { this(TraceAI.getTracer(), provider); }

    /**
     * Creates a metadata-only listener without changing the supplied tracer or SDK.
     * @param tracer instrumentation tracer
     * @param provider model provider name
     */
    public LangChain4jListener(FITracer tracer, String provider) { this(tracer, provider, false); }

    /**
     * Creates a listener with explicit content capture policy.
     * @param tracer instrumentation tracer; its hide flags always take precedence
     * @param provider model provider name
     * @param captureContent opt in to message, tool-call and error-detail capture
     */
    public LangChain4jListener(FITracer tracer, String provider, boolean captureContent) {
        this.tracer = Objects.requireNonNull(tracer, "tracer");
        this.provider = Objects.requireNonNull(provider, "provider");
        this.captureContent = captureContent;
    }

    @Override
    public void onRequest(ChatModelRequestContext context) {
        if (Boolean.TRUE.equals(Context.current().get(LEGACY_SPAN))) return;
        synchronized (context.attributes()) {
            if (context.attributes().containsKey(STATE_KEY)) return;
            Span span = tracer.startSpan("LangChain4j Chat", FISpanKind.LLM, Context.current());
            context.attributes().put(STATE_KEY, new State(this, span));
            span.setAttribute(SemanticConventions.LLM_PROVIDER, provider);
            try {
                Object request = ListenerPayload.request(context);
                span.setAttribute(SemanticConventions.LLM_REQUEST_MODEL, ListenerPayload.requestModel(request));
                if (captureContent) ChatSpanAttributes.input(tracer, span, ListenerPayload.messages(request));
            } catch (RuntimeException | LinkageError e) {
                instrumentationError(span, e);
            }
        }
    }

    @Override
    public void onResponse(ChatModelResponseContext context) {
        State state = take(context.attributes());
        if (state == null) return;
        try {
            Object response = ListenerPayload.response(context);
            state.span.setAttribute(SemanticConventions.LLM_RESPONSE_MODEL, ListenerPayload.responseModel(response));
            state.span.setAttribute(SemanticConventions.LLM_RESPONSE_ID, ListenerPayload.responseId(response));
            if (state.owner.captureContent)
                ChatSpanAttributes.output(state.owner.tracer, state.span, ListenerPayload.message(response));
            ChatSpanAttributes.usage(state.span, ListenerPayload.usage(response), ListenerPayload.finishReason(response));
            state.span.setStatus(StatusCode.OK);
        } catch (RuntimeException | LinkageError e) {
            instrumentationError(state.span, e);
        } finally {
            state.span.end();
        }
    }

    @Override
    public void onError(ChatModelErrorContext context) {
        State state = take(context.attributes());
        if (state == null) return;
        try {
            if (state.owner.captureContent) {
                state.owner.tracer.setError(state.span, context.error());
            } else {
                String type = context.error().getClass().getName();
                state.span.setStatus(StatusCode.ERROR);
                state.span.setAttribute(SemanticConventions.ERROR_TYPE, type);
                state.span.addEvent("exception", Attributes.of(AttributeKey.stringKey("exception.type"), type));
            }
        } finally {
            state.span.end();
        }
    }

    private static void instrumentationError(Span span, Throwable error) {
        // Fail open, but leave diagnostics. Exception messages/causes may contain model content;
        // log only the exception type and stack locations, never the original throwable.
        span.setAttribute("traceai.instrumentation.error", error.getClass().getName());
        IllegalStateException diagnostic = new IllegalStateException("LangChain4j telemetry capture failed: "
                + error.getClass().getName());
        diagnostic.setStackTrace(error.getStackTrace());
        System.getLogger(LangChain4jListener.class.getName()).log(System.Logger.Level.WARNING,
                "LangChain4j telemetry capture failed", diagnostic);
    }

    private static State take(Map<Object, Object> attributes) {
        synchronized (attributes) { return (State) attributes.remove(STATE_KEY); }
    }

    private static final class State {
        private final LangChain4jListener owner;
        private final Span span;
        private State(LangChain4jListener owner, Span span) { this.owner = owner; this.span = span; }
    }
}
