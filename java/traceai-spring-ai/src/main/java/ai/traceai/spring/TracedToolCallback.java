package ai.traceai.spring;

import ai.traceai.FISpanKind;
import ai.traceai.FITracer;
import ai.traceai.SemanticConventions;
import ai.traceai.TraceAI;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.context.Scope;
import org.springframework.ai.chat.model.ToolContext;
import org.springframework.ai.tool.ToolCallback;
import org.springframework.ai.tool.definition.ToolDefinition;
import org.springframework.ai.tool.metadata.ToolMetadata;

import java.util.ArrayList;
import java.util.List;
import java.util.Objects;
import java.util.function.Supplier;

/**
 * A {@link ToolCallback} that records one {@code execute_tool <name>} TOOL span per call, for both
 * {@code call} overloads.
 *
 * <p>Wrap the callbacks you hand to the model or {@code ChatClient}:</p>
 * <pre>
 * List&lt;ToolCallback&gt; callbacks = TracedToolCallback.wrapAll(
 *     List.of(MethodToolCallbackProvider.builder().toolObjects(new WeatherTools()).build().getToolCallbacks()), tracer);
 * </pre>
 *
 * <p>The span is a child of the current span and is current while the tool runs. It carries
 * {@code gen_ai.operation.name=execute_tool}, {@code gen_ai.tool.name}, {@code gen_ai.tool.description}, the
 * tool arguments as {@code input.value} and the result as {@code output.value}. Spring AI does not pass a call id
 * to a callback, so {@code gen_ai.tool.call.id} is not set. Content goes through {@link FITracer}, so the TraceConfig
 * hide settings apply. A failure is recorded and the original exception is rethrown unchanged.</p>
 *
 * <p>This wrapper is for applications that do not use the native observation path
 * ({@link SpringAIObservationFilter}), which records tool calls itself. With the zero-code agent also attached there
 * is still one span: the agent skips a call that already runs under a TOOL span named for the same tool.</p>
 */
public class TracedToolCallback implements ToolCallback {

    private final ToolCallback delegate;
    private final FITracer tracer;

    public TracedToolCallback(ToolCallback delegate, FITracer tracer) {
        this.delegate = Objects.requireNonNull(delegate, "delegate ToolCallback must not be null");
        this.tracer = Objects.requireNonNull(tracer, "FITracer must not be null");
    }

    /** Wraps a callback using the global TraceAI tracer. */
    public TracedToolCallback(ToolCallback delegate) {
        this(delegate, TraceAI.getTracer());
    }

    /** Wraps every callback that is not already traced. */
    public static List<ToolCallback> wrapAll(List<? extends ToolCallback> callbacks, FITracer tracer) {
        List<ToolCallback> wrapped = new ArrayList<>(callbacks.size());
        for (ToolCallback callback : callbacks) {
            wrapped.add(callback instanceof TracedToolCallback ? callback : new TracedToolCallback(callback, tracer));
        }
        return wrapped;
    }

    @Override
    public ToolDefinition getToolDefinition() {
        return delegate.getToolDefinition();
    }

    @Override
    public ToolMetadata getToolMetadata() {
        return delegate.getToolMetadata();
    }

    @Override
    public String call(String toolInput) {
        return traced(toolInput, () -> delegate.call(toolInput));
    }

    @Override
    public String call(String toolInput, ToolContext toolContext) {
        return traced(toolInput, () -> delegate.call(toolInput, toolContext));
    }

    private String traced(String toolInput, Supplier<String> call) {
        ToolDefinition definition = delegate.getToolDefinition();
        String name = definition == null ? null : definition.name();
        Span span = tracer.startSpan("execute_tool " + name, FISpanKind.TOOL);
        try (Scope ignored = span.makeCurrent()) {
            span.setAttribute(SemanticConventions.GEN_AI_OPERATION_NAME, "execute_tool");
            if (name != null) {
                span.setAttribute(SemanticConventions.GEN_AI_TOOL_NAME, name);
            }
            if (definition != null && definition.description() != null) {
                tracer.setAttribute(span, SemanticConventions.GEN_AI_TOOL_DESCRIPTION, definition.description());
            }
            tracer.setInputValue(span, toolInput);

            String result = call.get();

            tracer.setOutputValue(span, result);
            span.setStatus(StatusCode.OK);
            return result;
        } catch (Throwable error) {
            tracer.setContentError(span, error);
            throw error;
        } finally {
            span.end();
        }
    }
}
