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

import java.lang.reflect.InvocationTargetException;
import java.lang.reflect.Proxy;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.Objects;
import java.util.concurrent.CancellationException;
import java.util.concurrent.CompletableFuture;

/**
 * Explicitly registered official model listener for LangChain4j 0.35 and 1.x.
 * Attach one instance with {@code builder.listeners(List.of(listener))} to a
 * model that supports the official listener API. Works for synchronous requests
 * and streaming/async requests that report a terminal response or error.
 *
 * <p>Content and error details are off by default. Opt-in capture still respects
 * all FITracer hide flags. No global SDK is installed. Use a batch span processor
 * in production so callbacks do not perform network I/O.</p>
 *
 * <p>Span state lives in the framework's per-request attribute map. The parent is
 * captured at onRequest; no scope is left open across threads. For framework
 * versions that omit terminal callbacks on cancellation or synchronous streaming
 * setup errors, opt into {@link #guardLifecycle(Class, Object)} at the model boundary.
 * An abandoned stream with no terminal callback still cannot be detected.
 * This observes model calls, not service/tool execution. Do not combine with
 * another model tracing integration.</p>
 */
public final class LangChain4jListener implements ChatModelListener {
    static final ContextKey<Boolean> LEGACY_SPAN = ContextKey.named("traceai.langchain4j.legacy");
    // Shared across instances: accidental duplicate registration still emits only one span.
    private static final Object STATE_KEY = new Object();
    // Only correlates the synchronous onRequest call to its invocation boundary;
    // never carries a span or scope across an asynchronous request.
    private static final ThreadLocal<Invocation> INVOCATION = new ThreadLocal<>();
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

    /**
     * Opt-in boundary for models with a listener already attached. Preserve the
     * original model's callbacks and return values while closing spans if the
     * framework omits onError for cancelled futures or a synchronous throw.
     * Pass the public model interface (e.g. ChatModel.class on 1.x) and use the
     * returned model in place of the original, including in AiServices.
     *
     * <p>This cannot detect a stream abandoned without a terminal callback or
     * throwing call. It does not add listeners to an uninstrumented model.</p>
     * @param modelInterface public interface implemented by the model
     * @param model model with this listener registered through its builder
     * @return interface proxy with lifecycle fallback
     * @param <T> public model interface
     */
    public static <T> T guardLifecycle(Class<T> modelInterface, T model) {
        Objects.requireNonNull(modelInterface, "modelInterface");
        Objects.requireNonNull(model, "model");
        if (!modelInterface.isInterface() || !modelInterface.isInstance(model))
            throw new IllegalArgumentException("modelInterface must be implemented by model");
        Object proxy = Proxy.newProxyInstance(modelInterface.getClassLoader(), new Class<?>[]{modelInterface},
                (ignored, method, args) -> {
                    Invocation previous = INVOCATION.get();
                    Invocation invocation = new Invocation();
                    INVOCATION.set(invocation);
                    try {
                        Object result = method.invoke(model, args);
                        if (result instanceof CompletableFuture<?> future) {
                            future.whenComplete((value, error) -> {
                                if (future.isCancelled()) invocation.fail(new CancellationException());
                            });
                        }
                        return result;
                    } catch (InvocationTargetException e) {
                        invocation.fail(e.getCause());
                        throw e.getCause();
                    } finally {
                        if (previous == null) INVOCATION.remove();
                        else INVOCATION.set(previous);
                    }
                });
        return modelInterface.cast(proxy);
    }

    @Override
    public void onRequest(ChatModelRequestContext context) {
        if (Boolean.TRUE.equals(Context.current().get(LEGACY_SPAN))) return;
        synchronized (context.attributes()) {
            if (context.attributes().containsKey(STATE_KEY)) return;
            Span span = tracer.startSpan("LangChain4j Chat", FISpanKind.LLM, Context.current());
            context.attributes().put(STATE_KEY, new State(this, span));
            Invocation invocation = INVOCATION.get();
            if (invocation != null) invocation.requests.add(context.attributes());
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
        finishError(context.attributes(), context.error());
    }

    private static void finishError(Map<Object, Object> attributes, Throwable error) {
        State state = take(attributes);
        if (state == null) return;
        try {
            if (state.owner.captureContent) {
                state.owner.tracer.setError(state.span, error);
            } else {
                String type = error.getClass().getName();
                state.span.setStatus(StatusCode.ERROR);
                state.span.setAttribute(SemanticConventions.ERROR_TYPE, type);
                state.span.addEvent("exception", Attributes.of(AttributeKey.stringKey("exception.type"), type));
            }
        } finally {
            state.span.end();
        }
    }

    private static final class Invocation {
        private final List<Map<Object, Object>> requests = new ArrayList<>();
        private void fail(Throwable error) {
            for (Map<Object, Object> attributes : requests) finishError(attributes, error);
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
