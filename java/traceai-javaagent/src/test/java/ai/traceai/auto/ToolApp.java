package ai.traceai.auto;

import dev.langchain4j.agent.tool.ToolExecutionRequest;
import dev.langchain4j.service.tool.ToolExecutor;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.context.Scope;
import io.opentelemetry.sdk.common.CompletableResultCode;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.data.SpanData;
import io.opentelemetry.sdk.trace.export.SimpleSpanProcessor;
import io.opentelemetry.sdk.trace.export.SpanExporter;
import java.util.Collection;
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

    /** A tool that calls itself with the same arguments, the way a recursive tool does: depth levels deep. */
    public static final class RecursiveExecutor implements ToolExecutor {
        int depth = 1;
        @Override public String execute(ToolExecutionRequest request, Object memoryId) { return depth-- > 0 ? execute(request, memoryId) : RESULT; }
    }
    /** Spring AI-style: call(String) delegates to call(String, ToolContext), which recurses through call(String). */
    public static final class RecursiveCallback implements ToolCallback {
        int depth = 1;
        @Override public ToolDefinition getToolDefinition() {
            return new ToolDefinition() { @Override public String name() { return "lookup"; } @Override public String description() { return "d"; } };
        }
        @Override public String call(String toolInput) { return call(toolInput, null); }
        @Override public String call(String toolInput, ToolContext toolContext) { return depth-- > 0 ? call(toolInput) : RESULT; }
    }

    /** Spring AI-style: call(String) delegates to call(String, ToolContext), which recurses through the two-argument overload itself. */
    public static final class RecursiveTwoArgCallback implements ToolCallback {
        int depth = 1;
        @Override public ToolDefinition getToolDefinition() {
            return new ToolDefinition() { @Override public String name() { return "lookup"; } @Override public String description() { return "d"; } };
        }
        @Override public String call(String toolInput) { return call(toolInput, null); }
        @Override public String call(String toolInput, ToolContext toolContext) { return depth-- > 0 ? call(toolInput, toolContext) : RESULT; }
    }
    /** A callback whose equals and hashCode must never run: the agent compares calls by identity. */
    public static final class HostileEquals implements ToolCallback {
        @Override public ToolDefinition getToolDefinition() {
            return new ToolDefinition() { @Override public String name() { return "lookup"; } @Override public String description() { return "d"; } };
        }
        @Override public String call(String toolInput) { return RESULT; }
        @Override public boolean equals(Object other) { throw new IllegalStateException("equals ran"); }
        @Override public int hashCode() { throw new IllegalStateException("hashCode ran"); }
    }
    /** An application subclass of the library's non-final wrapper: still the wrapper, which opens its own TOOL span. */
    public static final class WrapperSubclass extends ai.traceai.spring.TracedToolCallback {
        WrapperSubclass(ToolCallback delegate, io.opentelemetry.api.trace.Tracer tracer) { super(delegate, tracer); }
        @Override public String call(String toolInput) { return super.call(toolInput); }
        @Override public String call(String toolInput, ToolContext toolContext) { return super.call(toolInput, toolContext); }
    }

    public static void main(String[] args) throws Exception {
        String scenario = args[0];
        SpanExporter printer = new SpanExporter() {
            @Override public CompletableResultCode export(Collection<SpanData> spans) {
                for (SpanData span : spans) if (span.getName().startsWith("execute_tool")) System.out.println("APP_SPAN=" + span.getName());
                return CompletableResultCode.ofSuccess();
            }
            @Override public CompletableResultCode flush() { return CompletableResultCode.ofSuccess(); }
            @Override public CompletableResultCode shutdown() { return CompletableResultCode.ofSuccess(); }
        };
        try (SdkTracerProvider provider = SdkTracerProvider.builder().addSpanProcessor(SimpleSpanProcessor.create(printer)).build()) {
            String parentName = scenario.equals("native_name") ? "framework lookup" : scenario.equals("native_spring_ai_tool") ? "spring.ai.tool"
                : scenario.equals("native_tool_call_name") ? "tool_call lookup" : "application-parent";
            Span app = provider.get("tool-app").spanBuilder(parentName).startSpan();
            if (scenario.equals("native_attribute")) app.setAttribute("spring.ai.kind", "tool_call");
            if (scenario.equals("library_span")) {
                // A traceai-java-langchain4j / -spring-ai wrapper's tool span is already current: the agent adds nothing.
                app.setAttribute("fi.span.kind", "TOOL"); app.setAttribute("gen_ai.operation.name", "execute_tool"); app.setAttribute("gen_ai.tool.name", "lookup");
            }
            System.out.println("PARENT=" + app.getSpanContext().getSpanId());
            try (Scope ignored = scenario.equals("no_parent") ? Span.getInvalid().makeCurrent() : app.makeCurrent()) {
                ToolExecutionRequest request = new ToolExecutionRequest("call-9", scenario.equals("failure") ? "failing" : "lookup", ARGS);
                Executor executor = new Executor(); Callback callback = new Callback(scenario.equals("failure") ? "failing" : "lookup");
                if (scenario.equals("wrapped")) {
                    var tracer = provider.get("wrapper");
                    System.out.println("LC4J=" + new ai.traceai.langchain4j.TracedToolExecutor(executor, tracer).execute(request, "memory"));
                    System.out.println("SPRING_ONE=" + new ai.traceai.spring.TracedToolCallback(callback, tracer).call(ARGS));
                } else if (scenario.equals("wrapper_subclass")) {
                    System.out.println("SPRING_ONE=" + new WrapperSubclass(callback, provider.get("wrapper")).call(ARGS));
                    System.out.println("SPRING_TWO=" + new WrapperSubclass(callback, provider.get("wrapper")).call(ARGS, new ToolContext(Map.of())));
                } else if (scenario.equals("recursive_two_arg")) {
                    System.out.println("SPRING_ONE=" + new RecursiveTwoArgCallback().call(ARGS));
                } else if (scenario.equals("hostile_equals")) {
                    System.out.println("SPRING_ONE=" + new HostileEquals().call(ARGS));
                } else if (scenario.startsWith("native_")) {
                    System.out.println("SPRING_ONE=" + callback.call(ARGS));
                } else if (scenario.equals("recursive")) {
                    System.out.println("LC4J=" + new RecursiveExecutor().execute(request, null));
                    System.out.println("SPRING_ONE=" + new RecursiveCallback().call(ARGS));
                } else if (scenario.equals("failure")) {
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
        if (scenario.equals("doctor")) System.out.println("DOCTOR=" + Class.forName("ai.traceai.auto.TraceAIAuto").getMethod("doctor").invoke(null));
        System.out.println("DONE");
    }
}
