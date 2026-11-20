package ai.traceai.auto;

import dev.langchain4j.agent.tool.ToolExecutionRequest;
import dev.langchain4j.service.tool.ToolExecutor;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.context.Scope;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import org.springframework.ai.chat.model.ToolContext;
import org.springframework.ai.tool.ToolCallback;
import org.springframework.ai.tool.definition.ToolDefinition;
import java.util.Map;

/**
 * Application for ToolSpansE2ETest, run in a child JVM with the agent attached. It calls tools through the
 * stand-in LangChain4j ToolExecutor and Spring AI ToolCallback types (the agent matches them by name) and
 * prints one line per outcome. Argument one picks the scenario.
 */
public final class ToolApp {
    static final String ARGS = "{\"city\":\"PRIVATE_TOOL_ARGUMENTS\"}", RESULT = "PRIVATE_TOOL_RESULT";
    static final IllegalStateException FAILURE = new IllegalStateException("PRIVATE_TOOL_FAILURE");

    /** A LangChain4j-style executor. */
    public static final class Executor implements ToolExecutor {
        @Override public String execute(ToolExecutionRequest request, Object memoryId) {
            if (request.name().equals("failing")) throw FAILURE;
            return RESULT;
        }
    }
    /** A Spring AI-style callback whose one-argument call delegates to the two-argument one, as the framework's own do. */
    public static final class Callback implements ToolCallback {
        final String name;
        Callback(String name) { this.name = name; }
        @Override public ToolDefinition getToolDefinition() {
            return new ToolDefinition() {
                @Override public String name() { return name; }
                @Override public String description() { return "PRIVATE_TOOL_DESCRIPTION"; }
            };
        }
        @Override public String call(String toolInput) { return call(toolInput, null); }
        @Override public String call(String toolInput, ToolContext toolContext) {
            if (name.equals("failing")) throw FAILURE;
            return RESULT;
        }
    }

    public static void main(String[] args) throws Exception {
        String scenario = args[0];
        try (SdkTracerProvider provider = SdkTracerProvider.builder().build()) {
            Span app = provider.get("tool-app").spanBuilder("application-parent").startSpan();
            if (scenario.equals("library_span")) {
                // A traceai-java-langchain4j / -spring-ai wrapper's tool span is already current: the agent adds nothing.
                app.setAttribute("fi.span.kind", "TOOL"); app.setAttribute("gen_ai.operation.name", "execute_tool"); app.setAttribute("gen_ai.tool.name", "lookup");
            }
            System.out.println("PARENT=" + app.getSpanContext().getSpanId());
            try (Scope ignored = scenario.equals("no_parent") ? Span.getInvalid().makeCurrent() : app.makeCurrent()) {
                ToolExecutionRequest request = new ToolExecutionRequest("call-9", scenario.equals("failure") ? "failing" : "lookup", ARGS);
                Executor executor = new Executor(); Callback callback = new Callback(scenario.equals("failure") ? "failing" : "lookup");
                if (scenario.equals("failure")) {
                    for (Object tool : new Object[]{executor, callback}) {
                        try { if (tool == executor) executor.execute(request, null); else callback.call(ARGS); System.out.println("NO_FAILURE"); }
                        catch (IllegalStateException error) { System.out.println("SAME=" + (error == FAILURE)); }
                    }
                } else {
                    System.out.println("LC4J=" + executor.execute(request, "memory"));
                    System.out.println("SPRING_ONE=" + callback.call(ARGS));
                    System.out.println("SPRING_TWO=" + callback.call(ARGS, new ToolContext(Map.of())));
                }
            } finally { app.end(); }
        }
        Class.forName("ai.traceai.auto.TraceAIAuto").getMethod("flush").invoke(null);
        System.out.println("DONE");
    }
}
