package ai.traceai.langchain4j;

import ai.traceai.FISpanKind;
import ai.traceai.FITracer;
import ai.traceai.SemanticConventions;
import ai.traceai.TraceAI;
import dev.langchain4j.agent.tool.Tool;
import dev.langchain4j.agent.tool.ToolExecutionRequest;
import dev.langchain4j.agent.tool.ToolSpecification;
import dev.langchain4j.agent.tool.ToolSpecifications;
import dev.langchain4j.service.tool.DefaultToolExecutor;
import dev.langchain4j.service.tool.ToolExecutor;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.context.Scope;

import java.lang.reflect.Method;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.Objects;

/**
 * A {@link ToolExecutor} that records one {@code execute_tool <name>} TOOL span per execution.
 *
 * <p>AiServices builds its own executors for {@code tools(Object...)}, so to trace tool executions
 * hand AiServices the traced map instead:</p>
 * <pre>
 * Assistant assistant = AiServices.builder(Assistant.class)
 *     .chatLanguageModel(model)
 *     .tools(TracedToolExecutor.forTools(TraceAI.getTracer(), new WeatherTools()))
 *     .build();
 * </pre>
 *
 * <p>The span is a child of the current span and is current while the tool runs. It carries
 * {@code gen_ai.operation.name=execute_tool}, {@code gen_ai.tool.name}, {@code gen_ai.tool.call.id} (the
 * model's call id), {@code gen_ai.tool.description} (when the {@link ToolSpecification} is known), the
 * tool arguments as {@code input.value} and the result as {@code output.value}. Content goes through
 * {@link FITracer}, so the TraceConfig hide settings apply: arguments and the description under
 * {@code hideInputs} / {@code hideInputMessages}, the result under {@code hideOutputs} /
 * {@code hideOutputMessages}. A failure is recorded and the original exception is rethrown unchanged.</p>
 *
 * <p>Wrap a tool only once. With the zero-code agent also attached there is still one span: the agent skips a
 * call that already runs under a TOOL span named for the same tool.</p>
 */
public final class TracedToolExecutor implements ToolExecutor {

    private final ToolExecutor delegate;
    private final ToolSpecification specification;
    private final FITracer tracer;

    /**
     * @param delegate      the executor to trace
     * @param specification the tool's specification, for its description; may be null
     * @param tracer        the tracer
     */
    public TracedToolExecutor(ToolExecutor delegate, ToolSpecification specification, FITracer tracer) {
        this.delegate = Objects.requireNonNull(delegate, "delegate ToolExecutor must not be null");
        this.specification = specification;
        this.tracer = Objects.requireNonNull(tracer, "FITracer must not be null");
    }

    /** Traces an executor whose description is not known. */
    public TracedToolExecutor(ToolExecutor delegate, FITracer tracer) {
        this(delegate, null, tracer);
    }

    @Override
    public String execute(ToolExecutionRequest request, Object memoryId) {
        String name = request == null ? null : request.name();
        Span span = tracer.startSpan("execute_tool " + name, FISpanKind.TOOL);
        try (Scope ignored = span.makeCurrent()) {
            span.setAttribute(SemanticConventions.GEN_AI_OPERATION_NAME, "execute_tool");
            if (name != null) {
                span.setAttribute(SemanticConventions.GEN_AI_TOOL_NAME, name);
            }
            if (request != null && request.id() != null) {
                span.setAttribute(SemanticConventions.GEN_AI_TOOL_CALL_ID, request.id());
            }
            if (specification != null && specification.description() != null) {
                tracer.setAttribute(span, SemanticConventions.GEN_AI_TOOL_DESCRIPTION, specification.description());
            }
            String arguments = request == null ? null : request.arguments();
            tracer.setInputValue(span, arguments);

            String result = delegate.execute(request, memoryId);

            tracer.setOutputValue(span, result);
            span.setStatus(StatusCode.OK);
            return result;
        } catch (Throwable error) {
            tracer.setError(span, error);
            throw error;
        } finally {
            span.end();
        }
    }

    /** Traces every executor of an {@code AiServices.tools(Map)} argument, with the global tracer. */
    public static Map<ToolSpecification, ToolExecutor> wrapAll(Map<ToolSpecification, ToolExecutor> tools) {
        return wrapAll(tools, TraceAI.getTracer());
    }

    /** Traces every executor of an {@code AiServices.tools(Map)} argument. */
    public static Map<ToolSpecification, ToolExecutor> wrapAll(Map<ToolSpecification, ToolExecutor> tools, FITracer tracer) {
        Map<ToolSpecification, ToolExecutor> wrapped = new LinkedHashMap<>();
        tools.forEach((specification, executor) -> wrapped.put(specification,
            executor instanceof TracedToolExecutor ? executor : new TracedToolExecutor(executor, specification, tracer)));
        return wrapped;
    }

    /**
     * Builds traced executors for the {@link Tool}-annotated methods of the given objects, the way
     * {@code AiServices.tools(Object...)} does, for {@code AiServices.tools(Map)}.
     */
    public static Map<ToolSpecification, ToolExecutor> forTools(FITracer tracer, Object... toolObjects) {
        Map<ToolSpecification, ToolExecutor> tools = new LinkedHashMap<>();
        for (Object tool : toolObjects) {
            for (Method method : tool.getClass().getDeclaredMethods()) {
                if (method.isAnnotationPresent(Tool.class)) {
                    ToolSpecification specification = ToolSpecifications.toolSpecificationFrom(method);
                    tools.put(specification, new TracedToolExecutor(new DefaultToolExecutor(tool, method), specification, tracer));
                }
            }
        }
        return tools;
    }
}
