package ai.traceai.langchain4j;

import dev.langchain4j.agent.tool.ToolExecutionRequest;
import dev.langchain4j.service.tool.ToolExecutor;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.api.trace.Tracer;
import io.opentelemetry.context.Scope;

/**
 * Stand-in for the traceai-java-langchain4j wrapper of the same name, for the agent's ToolApp: it opens its own TOOL span
 * around the delegate, as the real wrapper does, so the agent must not add a second span for it.
 */
public final class TracedToolExecutor implements ToolExecutor {
    private final ToolExecutor delegate; private final Tracer tracer;
    public TracedToolExecutor(ToolExecutor delegate, Tracer tracer) { this.delegate = delegate; this.tracer = tracer; }
    @Override public String execute(ToolExecutionRequest request, Object memoryId) {
        Span span = tracer.spanBuilder("execute_tool " + request.name()).startSpan();
        span.setAttribute("fi.span.kind", "TOOL"); span.setAttribute("gen_ai.operation.name", "execute_tool"); span.setAttribute("gen_ai.tool.name", request.name());
        try (Scope ignored = span.makeCurrent()) { return delegate.execute(request, memoryId); } finally { span.end(); }
    }
}
