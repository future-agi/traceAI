package ai.traceai.spring;

import io.opentelemetry.api.trace.Span;
import io.opentelemetry.api.trace.Tracer;
import io.opentelemetry.context.Scope;
import org.springframework.ai.chat.model.ToolContext;
import org.springframework.ai.tool.ToolCallback;
import org.springframework.ai.tool.definition.ToolDefinition;

/** Stand-in for the traceai-java-spring-ai wrapper of the same name, for the agent's ToolApp (see the LangChain4j one). */
public final class TracedToolCallback implements ToolCallback {
    private final ToolCallback delegate; private final Tracer tracer;
    public TracedToolCallback(ToolCallback delegate, Tracer tracer) { this.delegate = delegate; this.tracer = tracer; }
    @Override public ToolDefinition getToolDefinition() { return delegate.getToolDefinition(); }
    @Override public String call(String toolInput) { return traced(() -> delegate.call(toolInput)); }
    @Override public String call(String toolInput, ToolContext toolContext) { return traced(() -> delegate.call(toolInput, toolContext)); }
    private String traced(java.util.function.Supplier<String> call) {
        String name = delegate.getToolDefinition().name();
        Span span = tracer.spanBuilder("execute_tool " + name).startSpan();
        span.setAttribute("fi.span.kind", "TOOL"); span.setAttribute("gen_ai.operation.name", "execute_tool"); span.setAttribute("gen_ai.tool.name", name);
        try (Scope ignored = span.makeCurrent()) { return call.get(); } finally { span.end(); }
    }
}
