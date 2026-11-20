package ai.traceai.googleadk;

import ai.traceai.FISpanKind;
import ai.traceai.FITracer;
import ai.traceai.TraceAI;
import com.google.adk.models.LlmRequest;
import com.google.adk.tools.BaseTool;
import com.google.adk.tools.ToolContext;
import com.google.genai.types.FunctionDeclaration;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.context.Scope;
import io.reactivex.rxjava3.core.Completable;
import io.reactivex.rxjava3.core.Single;

import java.util.Map;
import java.util.Optional;

/**
 * Traces every call an ADK agent makes to the wrapped {@link BaseTool}: one TOOL span with the tool's name and
 * description, its arguments and its result.
 *
 * <pre>
 * LlmAgent.builder().tools(new TracedTool(FunctionTool.create(Weather.class, "forecast")))
 * </pre>
 *
 * Every overridable member of {@link BaseTool} reaches the wrapped tool: its declaration, its call, and the way it
 * changes the model request, so a built-in tool (Google Search, code execution, retrieval) that adds itself to the request
 * keeps doing so when wrapped. A function tool, which only has a declaration, is added to the request as before.
 */
public final class TracedTool extends BaseTool {
    private final BaseTool delegate;
    private final FITracer tracer;

    public TracedTool(BaseTool delegate, FITracer tracer) {
        super(delegate.name(), delegate.description(), delegate.longRunning());
        this.delegate = delegate;
        this.tracer = tracer;
    }

    public TracedTool(BaseTool delegate) {
        this(delegate, TraceAI.getTracer());
    }

    @Override
    public Optional<FunctionDeclaration> declaration() {
        return delegate.declaration();
    }

    @Override
    public Completable processLlmRequest(LlmRequest.Builder llmRequestBuilder, ToolContext toolContext) {
        // Without an override the wrapped tool would add the same declaration this wrapper adds; with one (a built-in tool),
        // the override is what puts the tool into the model request.
        if (!overridesProcessLlmRequest(delegate)) return super.processLlmRequest(llmRequestBuilder, toolContext);
        // A tool that overrides this and calls super registers itself in the request's tool map, and ADK runs the tool it finds
        // there: put the wrapper back, so the call is still traced.
        return delegate.processLlmRequest(llmRequestBuilder, toolContext).doOnComplete(() -> registerWrapper(llmRequestBuilder));
    }

    private void registerWrapper(LlmRequest.Builder builder) {
        try {
            Map<String, BaseTool> tools = new java.util.LinkedHashMap<>(builder.build().tools());
            boolean changed = false;
            for (Map.Entry<String, BaseTool> entry : tools.entrySet()) {
                if (entry.getValue() == delegate) {
                    entry.setValue(this);
                    changed = true;
                }
            }
            if (changed) builder.tools(tools);
        } catch (Throwable ignored) {
            // The request keeps what the wrapped tool registered; only this call's span is lost.
        }
    }

    @Override
    public com.google.common.collect.ImmutableMap<String, Object> customMetadata() {
        return delegate.customMetadata();
    }

    @Override
    public void setCustomMetadata(String key, Object value) {
        delegate.setCustomMetadata(key, value);
    }

    private static boolean overridesProcessLlmRequest(BaseTool tool) {
        try {
            return tool.getClass().getMethod("processLlmRequest", LlmRequest.Builder.class, ToolContext.class)
                .getDeclaringClass() != BaseTool.class;
        } catch (NoSuchMethodException e) {
            return false;
        }
    }

    @Override
    public Single<Map<String, Object>> runAsync(Map<String, Object> args, ToolContext toolContext) {
        return Single.defer(() -> {
            Span span = tracer.startSpan("execute_tool " + name(), FISpanKind.TOOL);
            Single<Map<String, Object>> upstream;
            try (Scope ignored = span.makeCurrent()) {
                upstream = delegate.runAsync(args, toolContext);
            } catch (RuntimeException e) {
                finish(span, args, null, e, toolContext);
                throw e;
            }
            return upstream
                .doOnSuccess(result -> finish(span, args, result, null, toolContext))
                .doOnError(error -> finish(span, args, null, error, toolContext))
                .doOnDispose(() -> finish(span, args, null, null, toolContext));
        });
    }

    private void finish(Span span, Map<String, Object> args, Map<String, Object> result, Throwable error, ToolContext toolContext) {
        if (!span.isRecording()) return;
        try {
            try {
                String callId = callId(toolContext);
                SpanAttributes.write(tracer, span, AdkAttributes.tool(name(), description(), delegate.getClass().getSimpleName(),
                    args == null ? Map.of() : args, result, callId));
                if (error != null) tracer.setError(span, error);
                else span.setStatus(StatusCode.OK);
            } catch (Throwable tracingFailure) {
                // Tracing never fails the call: the tool's own result and error are what the application sees.
            }
        } finally {
            try {
                span.end();
            } catch (Throwable ignored) {
                // Nothing to do.
            }
        }
    }

    /** The model's id for this call; null when this ADK version has none. */
    private static String callId(ToolContext toolContext) {
        try {
            return toolContext == null ? null : toolContext.functionCallId().orElse(null);
        } catch (NoSuchMethodError | RuntimeException e) {
            return null;
        }
    }
}
