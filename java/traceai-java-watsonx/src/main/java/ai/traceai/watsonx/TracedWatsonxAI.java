package ai.traceai.watsonx;

import ai.traceai.FISpanKind;
import ai.traceai.FITracer;
import ai.traceai.TraceAI;
import com.ibm.watsonx.ai.chat.ChatHandler;
import com.ibm.watsonx.ai.chat.ChatRequest;
import com.ibm.watsonx.ai.chat.ChatResponse;
import com.ibm.watsonx.ai.chat.ChatService;
import com.ibm.watsonx.ai.chat.model.CompletedToolCall;
import com.ibm.watsonx.ai.chat.model.PartialChatResponse;
import com.ibm.watsonx.ai.chat.model.PartialToolCall;
import com.ibm.watsonx.ai.embedding.EmbeddingParameters;
import com.ibm.watsonx.ai.embedding.EmbeddingResponse;
import com.ibm.watsonx.ai.embedding.EmbeddingService;
import com.ibm.watsonx.ai.textgeneration.TextGenerationHandler;
import com.ibm.watsonx.ai.textgeneration.TextGenerationRequest;
import com.ibm.watsonx.ai.textgeneration.TextGenerationResponse;
import com.ibm.watsonx.ai.textgeneration.TextGenerationService;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.context.Context;
import io.opentelemetry.context.Scope;

import java.lang.reflect.InvocationTargetException;
import java.lang.reflect.Method;
import java.util.List;
import java.util.Objects;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.CompletionException;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.function.Supplier;

/**
 * Traces IBM's TextGenerationService, ChatService or EmbeddingService.
 * Wrap each service separately. Calls through the original service are not traced.
 * Supports the released SDK contracts tested at 0.12.0 and 0.40.0.
 * Streaming futures preserve SDK results and forward cancellation, completing after the span ends.
 */
public class TracedWatsonxAI {
    private static final org.slf4j.Logger LOGGER = org.slf4j.LoggerFactory.getLogger(TracedWatsonxAI.class);
    private final Object client;
    private final FITracer tracer;
    private final WatsonxAttributes attributes;

    /** @param client an IBM text generation, chat or embedding service
     *  @param tracer the tracer to use */
    public TracedWatsonxAI(Object client, FITracer tracer) {
        this.client = Objects.requireNonNull(client, "client");
        this.tracer = Objects.requireNonNull(tracer, "tracer");
        this.attributes = new WatsonxAttributes(tracer);
    }

    /** @param client an IBM text generation, chat or embedding service */
    public TracedWatsonxAI(Object client) {
        this(client, TraceAI.getTracer());
    }

    /** Generates text through IBM's typed service API. */
    public TextGenerationResponse generateText(TextGenerationRequest request) {
        return call("Watsonx Text Generation", FISpanKind.LLM, request,
            () -> service(TextGenerationService.class).generate(request), attributes::textResponse);
    }

    /**
     * Compatibility entry point. Prefer the typed overload.
     * @param request an IBM TextGenerationRequest
     * @param <T> TextGenerationResponse
     * @return the unchanged SDK response
     */
    @Deprecated
    @SuppressWarnings("unchecked")
    public <T> T generateText(Object request) {
        return (T) generateText(TextGenerationRequest.class.cast(request));
    }

    /** Sends a chat request through IBM's typed service API. */
    public ChatResponse chat(ChatRequest request) {
        return call("Watsonx Chat", FISpanKind.LLM, request,
            () -> ChatResponse.class.cast(invoke(ChatApi.METHOD, service(ChatService.class), request)), attributes::chatResponse);
    }

    /** Compatibility entry point; prefer {@link #chat(ChatRequest)}. */
    @Deprecated
    @SuppressWarnings("unchecked")
    public <T> T chat(Object request) {
        return (T) chat(ChatRequest.class.cast(request));
    }

    /** Embeds inputs with the service's configured model and project. */
    public EmbeddingResponse embedText(List<String> inputs) {
        return embedText(inputs, null);
    }

    /** Embeds inputs with optional per-request parameters. */
    public EmbeddingResponse embedText(List<String> inputs, EmbeddingParameters parameters) {
        return call("Watsonx Embed", FISpanKind.EMBEDDING, inputs,
            () -> EmbeddingResponse.class.cast(invoke(EmbeddingApi.METHOD, service(EmbeddingService.class), inputs, parameters)),
            attributes::embeddingResponse, parameters);
    }

    /**
     * Compatibility entry point accepting a list of strings, or a current SDK EmbeddingRequest.
     * Prefer {@link #embedText(List, EmbeddingParameters)} for version-independent code.
     */
    @Deprecated
    @SuppressWarnings("unchecked")
    public <T> T embedText(Object request) {
        Object inputs = request instanceof List<?> ? request : WatsonxAttributes.field(request, "inputs");
        if (!(inputs instanceof List<?> list) || list.stream().anyMatch(value -> !(value instanceof String))) {
            throw new IllegalArgumentException("Expected a list of strings or an IBM EmbeddingRequest");
        }
        Object parameters = request instanceof List<?> ? null : WatsonxAttributes.field(request, "parameters");
        if (parameters != null && !(parameters instanceof EmbeddingParameters)) {
            throw new IllegalArgumentException("Use embedText(List<String>, EmbeddingParameters)");
        }
        return (T) embedText((List<String>) list, (EmbeddingParameters) parameters);
    }

    /** Streams text and forwards all SDK callbacks under the operation's trace context. */
    public CompletableFuture<Void> generateTextStream(TextGenerationRequest request, TextGenerationHandler handler) {
        Objects.requireNonNull(handler, "handler");
        Operation operation = new Operation("Watsonx Text Generation (Stream)", FISpanKind.LLM, request, null);
        TextGenerationHandler traced = new TextGenerationHandler() {
            @Override public void onPartialResponse(String text) {
                operation.callback(() -> handler.onPartialResponse(text));
            }
            @Override public void onCompleteResponse(TextGenerationResponse response) {
                operation.capture(() -> attributes.textResponse(operation.span, response));
                operation.callback(() -> handler.onCompleteResponse(response));
            }
            @Override public void onError(Throwable error) {
                operation.error(error);
                operation.callback(() -> handler.onError(error));
            }
            @Override public boolean failOnFirstError() { return handler.failOnFirstError(); }
        };
        return operation.stream(() -> service(TextGenerationService.class).generateStreaming(request, traced));
    }

    /**
     * Compatibility entry point returning CompletableFuture&lt;Void&gt; (not an iterator).
     * Use the handler overload to receive text as it arrives.
     */
    @Deprecated
    @SuppressWarnings("unchecked")
    public <T> T generateTextStream(Object request) {
        return (T) generateTextStream(TextGenerationRequest.class.cast(request), new TextGenerationHandler() {
            @Override public void onPartialResponse(String text) { }
            @Override public void onCompleteResponse(TextGenerationResponse response) { }
            @Override public void onError(Throwable error) { }
        });
    }

    /**
     * Streams chat, forwarding tool-call and thinking callbacks without suppressing SDK behavior.
     * The future result is null with IBM 0.12.0 and ChatResponse with IBM 0.40.0.
     * @param request the IBM chat request
     * @param handler the consumer of IBM streaming events
     * @return a future with the SDK result that forwards cancellation and completes after the span ends
     */
    public CompletableFuture<?> chatStream(ChatRequest request, ChatHandler handler) {
        Objects.requireNonNull(handler, "handler");
        Operation operation = new Operation("Watsonx Chat (Stream)", FISpanKind.LLM, request, null);
        ChatHandler traced = new ChatHandler() {
            @Override public void onPartialResponse(String text, PartialChatResponse response) {
                operation.callback(() -> handler.onPartialResponse(text, response));
            }
            @Override public void onCompleteResponse(ChatResponse response) {
                operation.capture(() -> attributes.chatResponse(operation.span, response));
                operation.callback(() -> handler.onCompleteResponse(response));
            }
            @Override public void onError(Throwable error) {
                operation.error(error);
                operation.callback(() -> handler.onError(error));
            }
            @Override public void onPartialToolCall(PartialToolCall tool) {
                operation.callback(() -> handler.onPartialToolCall(tool));
            }
            @Override public void onCompleteToolCall(CompletedToolCall tool) {
                operation.callback(() -> handler.onCompleteToolCall(tool));
            }
            @Override public void onPartialThinking(String text, PartialChatResponse response) {
                operation.callback(() -> handler.onPartialThinking(text, response));
            }
            @Override public boolean failOnFirstError() { return handler.failOnFirstError(); }
        };
        return operation.stream(() -> service(ChatService.class).chatStreaming(request, traced));
    }

    /** @return the original IBM service */
    public Object unwrap() { return client; }

    private <T> T service(Class<T> type) {
        if (!type.isInstance(client)) throw new IllegalArgumentException("This operation requires " + type.getSimpleName());
        return type.cast(client);
    }

    private <T> T call(String name, FISpanKind kind, Object request, Supplier<T> action,
                       java.util.function.BiConsumer<Span, T> response) {
        return call(name, kind, request, action, response, null);
    }

    private <T> T call(String name, FISpanKind kind, Object request, Supplier<T> action,
                       java.util.function.BiConsumer<Span, T> response, Object parameters) {
        Operation operation = new Operation(name, kind, request, parameters);
        try (Scope ignored = operation.context.makeCurrent()) {
            T result = action.get();
            operation.capture(() -> response.accept(operation.span, result));
            return result;
        } catch (RuntimeException | Error error) {
            operation.error(error);
            throw error;
        } finally {
            operation.finish(null);
        }
    }

    /** The future is authoritative: onError can be non-terminal when failOnFirstError is false. */
    private final class Operation {
        private final Span span;
        private final Context context;
        private boolean ended;
        private final AtomicBoolean failed = new AtomicBoolean();

        Operation(String name, FISpanKind kind, Object request, Object parameters) {
            span = tracer.startSpan(name, kind);
            context = Context.current().with(span);
            capture(() -> attributes.request(span, request, parameters));
        }

        void capture(Runnable capture) {
            try { capture.run(); } catch (RuntimeException error) {
                // Fail open, but do not leak SDK payloads into diagnostics when error details are hidden.
                if (tracer.getConfig().isHideErrorDetails()) {
                    LOGGER.debug("Unable to collect Watsonx span metadata ({})", error.getClass().getName());
                } else {
                    LOGGER.debug("Unable to collect Watsonx span metadata", error);
                }
            }
        }

        void error(Throwable error) {
            failed.set(true);
            capture(() -> tracer.setError(span, error));
        }

        void callback(Runnable callback) {
            try (Scope ignored = context.makeCurrent()) {
                callback.run();
            } catch (RuntimeException | Error error) {
                error(error);
                throw error;
            }
        }

        synchronized void finish(Throwable error) {
            if (ended) return;
            ended = true;
            if (error != null) {
                if (error instanceof CompletionException && error.getCause() != null) error = error.getCause();
                error(error);
            }
            capture(() -> span.setStatus(failed.get() ? StatusCode.ERROR : StatusCode.OK));
            capture(span::end);
        }

        <T> CompletableFuture<T> stream(Supplier<CompletableFuture<T>> action) {
            try (Scope ignored = context.makeCurrent()) {
                CompletableFuture<T> future = Objects.requireNonNull(action.get(), "IBM streaming future");
                CompletableFuture<T> traced = new CompletableFuture<>() {
                    @Override public boolean cancel(boolean mayInterruptIfRunning) {
                        if (!future.cancel(mayInterruptIfRunning)) return false;
                        finish(new java.util.concurrent.CancellationException());
                        return super.cancel(mayInterruptIfRunning);
                    }
                };
                future.whenComplete((result, error) -> {
                    // End before releasing callers that may immediately flush/shut down tracing.
                    finish(error);
                    if (error == null) traced.complete(result);
                    else traced.completeExceptionally(error);
                });
                return traced;
            } catch (RuntimeException | Error error) {
                finish(error);
                throw error;
            }
        }
    }

    private static Object invoke(Method method, Object service, Object... arguments) {
        try {
            return method.invoke(service, arguments);
        } catch (InvocationTargetException e) {
            if (e.getCause() instanceof RuntimeException failure) throw failure;
            if (e.getCause() instanceof Error failure) throw failure;
            throw new IllegalStateException("IBM service call failed", e.getCause());
        } catch (IllegalAccessException e) {
            throw new IllegalStateException("Cannot access IBM service API", e);
        }
    }

    /** IBM changed chat(ChatRequest)'s return descriptor to TextChatResponse, breaking JVM linkage. */
    private static final class ChatApi {
        static final Method METHOD = resolve();
        private static Method resolve() {
            try {
                return ChatService.class.getMethod("chat", ChatRequest.class);
            } catch (NoSuchMethodException unsupported) {
                throw new IllegalStateException("Unsupported IBM ChatService API", unsupported);
            }
        }
    }

    /** Exact released signatures, not name/arity guessing. IBM renamed embedding to embed. */
    private static final class EmbeddingApi {
        static final Method METHOD = resolve();
        private static Method resolve() {
            try {
                return EmbeddingService.class.getMethod("embed", List.class, EmbeddingParameters.class);
            } catch (NoSuchMethodException beforeRename) {
                try {
                    return EmbeddingService.class.getMethod("embedding", List.class, EmbeddingParameters.class);
                } catch (NoSuchMethodException unsupported) {
                    throw new IllegalStateException("Unsupported IBM EmbeddingService API", unsupported);
                }
            }
        }
    }
}
