package ai.traceai.auto;

import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.api.trace.*;
import io.opentelemetry.sdk.resources.Resource;
import java.util.*;
import java.util.concurrent.TimeUnit;

/**
 * Zero-code execute_tool spans for framework tool executions (LangChain4j ToolExecutor, Spring AI
 * ToolCallback). Advice in the application's classes reaches this class through TraceAIAuto with
 * plain JDK types only. The span is a child of the current context; a provider call that the tool
 * itself makes on the same thread becomes a child of the tool span. Arguments, results and error
 * text follow the agent's content switches and export-time privacy boundary. Fails open.
 */
final class ToolSpans {
    static final String LANGCHAIN4J = "langchain4j", SPRING_AI = "spring-ai";
    static final String LANGCHAIN4J_EXECUTOR = "dev.langchain4j.service.tool.ToolExecutor";
    static final String LANGCHAIN4J_DEFAULT_EXECUTOR = "dev.langchain4j.service.tool.DefaultToolExecutor";
    static final String SPRING_AI_CALLBACK = "org.springframework.ai.tool.ToolCallback";
    private static final String LANGCHAIN4J_REQUEST = "dev.langchain4j.agent.tool.ToolExecutionRequest";
    private static final String LANGCHAIN4J_RESULT = "dev.langchain4j.service.tool.ToolExecutionResult";
    private static final String SPRING_AI_DEFINITION = "org.springframework.ai.tool.definition.ToolDefinition";
    static final String OPERATION = "execute_tool";
    /** Identity attributes, exported ahead of context and content when the attribute budget is short. */
    private static final List<String> ORDER = List.of("gen_ai.span.kind", "gen_ai.operation.name", "gen_ai.tool.name", "error.type",
        "gen_ai.tool.call.id", "gen_ai.tool.type");
    private static final WireCore.Match MATCH = new WireCore.Match(Map.of("operation", OPERATION), "", "", 0, Map.of(), Map.of());

    /** One active tool span on this thread; external is the application's current span when it started. */
    static final class Frame {
        final String framework, name, callId, arguments, external;
        final Object target;
        /** Executors and callbacks that delegated to this invocation (same framework and tool name), by identity. */
        final Set<Object> delegates = Collections.newSetFromMap(new IdentityHashMap<>());
        final Span span;
        final TraceAIAuto.RuntimeState state;
        final OtelBridge.Snapshot context;
        Throwable failure;
        Frame(String framework, Object target, String name, String callId, String arguments, Span span, TraceAIAuto.RuntimeState state, OtelBridge.Snapshot context) {
            this.framework = framework; this.target = target; this.name = name; this.callId = callId; this.arguments = arguments;
            this.span = span; this.state = state; this.context = context; this.external = context.external();
        }
    }
    private static final ThreadLocal<ArrayDeque<Frame>> FRAMES = ThreadLocal.withInitial(ArrayDeque::new);

    /** The innermost tool span on this thread, if any. */
    static Frame current() { ArrayDeque<Frame> frames = FRAMES.get(); return frames.peek(); }

    static Object enter(String framework, Object target, Object argument) {
        TraceAIAuto.RuntimeState state = TraceAIAuto.currentRuntime();
        if (state == null || !state.acceptingResults() || !state.config().enabled(framework) || TraceAIAuto.IN_FLIGHT.get() || TraceAIAuto.besideOtelJavaAgent()) return null;
        String name, callId = null, arguments;
        if (LANGCHAIN4J.equals(framework)) {
            name = string(call(argument, LANGCHAIN4J_REQUEST, "name"));
            callId = string(call(argument, LANGCHAIN4J_REQUEST, "id"));
            arguments = string(call(argument, LANGCHAIN4J_REQUEST, "arguments"));
        } else if (SPRING_AI.equals(framework)) {
            name = string(call(call(target, SPRING_AI_CALLBACK, "getToolDefinition"), SPRING_AI_DEFINITION, "name"));
            arguments = argument instanceof String text ? text : null;
        } else return null;
        if (name == null || name.isEmpty()) return null;
        Frame outer = current();
        // A default method delegating to the implementation, or a wrapper callback delegating to the
        // callback it decorates, is the same tool invocation: one span only.
        if (outer != null && outer.framework.equals(framework) && outer.name.equals(name)) { outer.delegates.add(target); return null; }
        OtelBridge.Snapshot context = OtelBridge.toolSnapshot();
        if (context.suppressed()) return null;
        // Spring AI's DefaultToolCallingManager wraps the call in its own tool_call observation. When
        // Micrometer Tracing bridges that observation to the application's current span, the span is
        // already this tool call (traceai-spring-ai's filter marks it TOOL when it stops): no second span.
        // An agent tool span already open under that same application span means this call is nested in
        // another tool, not the observed invocation itself.
        // The agent marks that span TOOL itself, with the content its own span would carry, so the call keeps
        // a TOOL span without traceai-spring-ai. If the span cannot be marked, the agent span is kept.
        if (SPRING_AI.equals(framework) && !openUnder(context.external())) {
            Observed top = OBSERVED.get().peek();
            // A wrapper callback delegating inside the observed invocation is that same invocation.
            if (top != null && top.external.equals(context.external()) && top.name.equals(name)) return null;
            if (springAiObserved(target, name, context.external())) {
                Observed observed = Observed.mark(state, name, arguments, context.external());
                if (observed != null) { OBSERVED.get().push(observed); return observed; }
            }
            if (OBSERVED.get().isEmpty()) OBSERVED.remove();
        }
        Span span = state.tracer().spanBuilder(bounded(OPERATION + " " + name, 32000)).setSpanKind(SpanKind.INTERNAL).setParent(context.parent())
            .setStartTimestamp(context.startEpochNanos(), TimeUnit.NANOSECONDS).startSpan();
        Frame frame = new Frame(framework, target, name, callId, arguments, span, state, context);
        FRAMES.get().push(frame);
        return frame;
    }

    /** A Spring AI tool call whose own tool_call observation span (the application's current span) the agent marked TOOL. */
    static final class Observed {
        final TraceAIAuto.RuntimeState state;
        final String name, arguments, external;
        final Object span;
        final Class<?> spanApi;
        private Observed(TraceAIAuto.RuntimeState state, String name, String arguments, String external, Object span, Class<?> spanApi) {
            this.state = state; this.name = name; this.arguments = arguments; this.external = external; this.span = span; this.spanApi = spanApi;
        }
        /** Marks the application's current span TOOL, or returns null so that the agent keeps its own span. */
        static Observed mark(TraceAIAuto.RuntimeState state, String name, String arguments, String external) {
            try {
                ClassLoader loader = Thread.currentThread().getContextClassLoader();
                // Build the prefix dynamically so Maven Shade does not rewrite the application API name.
                String prefix = String.join(".", "io", "opentelemetry");
                Class<?> spanApi = Class.forName(prefix + ".api.trace.Span", false, loader);
                Class<?> scApi = Class.forName(prefix + ".api.trace.SpanContext", false, loader);
                Object span = spanApi.getMethod("current").invoke(null);
                Object sc = spanApi.getMethod("getSpanContext").invoke(span);
                if (!external.equals(scApi.getMethod("getTraceId").invoke(sc) + "/" + scApi.getMethod("getSpanId").invoke(sc))) return null;
                if (!Boolean.TRUE.equals(spanApi.getMethod("isRecording").invoke(span))) return null;
                Map<String, Object> identity = project(state.config(), name, null, null, null, null, null, Map.of());
                if (!"TOOL".equals(identity.get("gen_ai.span.kind")) || !name.equals(identity.get("gen_ai.tool.name"))) return null;
                Observed observed = new Observed(state, name, arguments, external, span, spanApi);
                observed.set(identity);
                return observed;
            } catch (Throwable ignored) { return null; }
        }
        /** Spring AI's own convention names the operation when the observation stops; the agent adds the rest. */
        void set(Map<String, Object> attributes) throws ReflectiveOperationException {
            for (Map.Entry<String, Object> entry : attributes.entrySet()) {
                String key = entry.getKey(); Object value = entry.getValue();
                if (key.equals("gen_ai.operation.name")) continue;
                if (value instanceof Boolean flag) spanApi.getMethod("setAttribute", String.class, boolean.class).invoke(span, key, flag);
                else if (value instanceof Double || value instanceof Float) spanApi.getMethod("setAttribute", String.class, double.class).invoke(span, key, ((Number)value).doubleValue());
                else if (value instanceof Number number) spanApi.getMethod("setAttribute", String.class, long.class).invoke(span, key, number.longValue());
                else spanApi.getMethod("setAttribute", String.class, String.class).invoke(span, key, String.valueOf(value));
            }
        }
        /** Content and failure, as on the agent's own span. Spring AI ends the span when the observation stops. */
        void finish(Object returned, Throwable thrown) {
            try {
                AutoConfig config = state.config();
                Throwable failure = thrown == null ? null : unwrap(thrown);
                Map<String, Object> attributes = project(config, name, null, arguments, returned, failure, null, Map.of());
                set(attributes);
                if (failure != null) {
                    Class<?> statusApi = Class.forName(spanApi.getName().substring(0, spanApi.getName().lastIndexOf('.')) + ".StatusCode", false, spanApi.getClassLoader());
                    Object error = null;
                    for (Object constant : statusApi.getEnumConstants()) if ("ERROR".equals(((Enum<?>)constant).name())) error = constant;
                    String description = TraceAIAuto.hidesErrorText(config) ? "" : String.valueOf(failure.getMessage() == null ? "" : failure.getMessage());
                    spanApi.getMethod("setStatus", statusApi, String.class).invoke(span, error, bounded(description, 32000));
                }
            } catch (Throwable ignored) { }
        }
    }
    private static final ThreadLocal<ArrayDeque<Observed>> OBSERVED = ThreadLocal.withInitial(ArrayDeque::new);
    /** True when the agent marked this application span ("traceId/spanId") TOOL for an observed Spring AI call. */
    static boolean markedByAgent(String external) {
        if (external == null) return false;
        ArrayDeque<Observed> observed = OBSERVED.get();
        if (observed.isEmpty()) { OBSERVED.remove(); return false; }
        for (Observed entry : observed) if (entry.external.equals(external)) return true;
        return false;
    }

    private static boolean openUnder(String external) {
        for (Frame frame : FRAMES.get()) if (Objects.equals(frame.external, external)) return true;
        return false;
    }
    private static final String SPRING_AI_OBSERVATION = "org.springframework.ai.tool.observation.ToolCallingObservationContext";
    /** True when the current Micrometer observation is Spring AI's tool_call for this tool and its tracing
     * span is the application's current span (external, "traceId/spanId"). Reflective; false when unsure. */
    static boolean springAiObserved(Object callback, String name, String external) {
        if (external == null) return false;
        try {
            ClassLoader loader = callback.getClass().getClassLoader();
            Class<?> registryApi = Class.forName("io.micrometer.observation.ObservationRegistry", false, loader);
            // The current observation is held per thread by Micrometer and is the same for every registry.
            Object observation = registryApi.getMethod("getCurrentObservation").invoke(registryApi.getMethod("create").invoke(null));
            if (observation == null) return false;
            Object observed = Class.forName("io.micrometer.observation.Observation", false, loader).getMethod("getContext").invoke(observation);
            if (!name.equals(string(call(call(observed, SPRING_AI_OBSERVATION, "getToolDefinition"), SPRING_AI_DEFINITION, "name")))) return false;
            Class<?> contextApi = Class.forName("io.micrometer.observation.Observation$Context", false, loader);
            Class<?> tracingContext = Class.forName("io.micrometer.tracing.handler.TracingObservationHandler$TracingContext", false, loader);
            Object tracing = contextApi.getMethod("get", Object.class).invoke(observed, tracingContext);
            Object span = call(tracing, tracingContext.getName(), "getSpan");
            Object spanContext = call(span, "io.micrometer.tracing.Span", "context");
            Object traceId = call(spanContext, "io.micrometer.tracing.TraceContext", "traceId");
            Object spanId = call(spanContext, "io.micrometer.tracing.TraceContext", "spanId");
            return traceId != null && spanId != null && external.equals(traceId + "/" + spanId);
        } catch (Throwable ignored) { return false; }
    }

    /** A tool failure that the framework handles itself (for example LangChain4j returning the error text to the model). */
    static void failed(Object executor, Throwable error) {
        Frame frame = current();
        // Only the tool method's own exception counts; an access retry inside the framework does not.
        // It belongs to the innermost span only when that span is this executor's own invocation.
        if (frame == null || !(error instanceof java.lang.reflect.InvocationTargetException) || frame.failure != null) return;
        if (frame.target == executor || frame.delegates.contains(executor)) frame.failure = unwrap(error);
    }

    static void exit(Object token, Object returned, Throwable thrown) {
        if (token instanceof Observed observed) {
            ArrayDeque<Observed> stack = OBSERVED.get();
            if (stack.peek() == observed) stack.pop(); else stack.remove(observed);
            if (stack.isEmpty()) OBSERVED.remove();
            observed.finish(returned, thrown);
            return;
        }
        if (!(token instanceof Frame frame)) return;
        ArrayDeque<Frame> frames = FRAMES.get();
        if (frames.peek() == frame) frames.pop(); else frames.remove(frame);
        if (frames.isEmpty()) FRAMES.remove();
        // An asynchronous execution (LangChain4j executeAsync) ends when its future completes.
        if (thrown == null && returned instanceof java.util.concurrent.CompletionStage<?> stage) {
            try { stage.whenComplete((result, error) -> finish(frame, result, error)); return; }
            catch (Throwable ignored) { }
        }
        finish(frame, returned, thrown);
    }
    private static void finish(Frame frame, Object returned, Throwable thrown) {
        TraceAIAuto.RuntimeState state = frame.state;
        long end = frame.context.startEpochNanos() + Math.max(0, System.nanoTime() - frame.context.startNanos());
        Resource resource = frame.context.resource() == null ? state.resource : TraceAIAuto.withRouting(frame.context.resource(), state.resource);
        int limit = Math.min(state.provider().getSpanLimits().getMaxNumberOfAttributes(),
            frame.context.attributeLimit() == null ? Integer.MAX_VALUE : frame.context.attributeLimit());
        Span span = frame.span;
        try {
            Throwable failure = thrown != null ? unwrap(thrown) : frame.failure;
            AutoConfig config = state.config();
            Map<String, Object> projected = project(config, frame.name, frame.callId, frame.arguments, returned, failure, frame.failure, frame.context.attributes());
            boolean error = projected.containsKey("error.type") || failure != null;
            int samplingSlots = span instanceof io.opentelemetry.sdk.trace.ReadableSpan readable
                && readable.getAttribute(AttributeKey.doubleKey("fi.sampling.ratio")) != null ? 1 : 0;
            int budget = Math.max(0, limit - samplingSlots), written = 0;
            for (Map.Entry<String, Object> entry : projected.entrySet()) {
                if (written == budget) break;
                String key = entry.getKey(); Object value = entry.getValue();
                if (value instanceof String text) span.setAttribute(key, text);
                else if (value instanceof Boolean flag) span.setAttribute(key, flag);
                else if (value instanceof Double || value instanceof Float) span.setAttribute(key, ((Number)value).doubleValue());
                else if (value instanceof Number number) span.setAttribute(key, number.longValue());
                else span.setAttribute(key, String.valueOf(value));
                written++;
            }
            String description = failure == null || TraceAIAuto.hidesErrorText(config) ? "" : String.valueOf(failure.getMessage() == null ? "" : failure.getMessage());
            span.setStatus(error ? StatusCode.ERROR : StatusCode.OK, error ? bounded(description, 32000) : "");
        } catch (Throwable ignored) {
        } finally { WireResourceProcessor.end(span, end, resource, limit); }
    }

    /** The tool span's attributes in export priority order (identity, context, content), after redaction and
     * the hide switches. failure is the unwrapped thrown exception; recorded is a failure the framework handled. */
    static Map<String, Object> project(AutoConfig config, String name, String callId, String arguments, Object returned, Throwable failure,
                                       Throwable recorded, Map<String, ?> context) {
        String result = null; boolean error = false;
        if (returned instanceof String text) result = text;
        else if (returned != null) {
            result = string(call(returned, LANGCHAIN4J_RESULT, "resultText"));
            error = Boolean.TRUE.equals(call(returned, LANGCHAIN4J_RESULT, "isError"));
        }
        if (failure == null && recorded != null) failure = recorded;
        error |= failure != null;
        Map<String, Object> attrs = new LinkedHashMap<>();
        attrs.put("gen_ai.span.kind", "TOOL"); attrs.put("gen_ai.operation.name", OPERATION);
        attrs.put("gen_ai.tool.name", name);
        if (error) attrs.put("error.type", failure == null ? "tool_error" : failure.getClass().getName());
        if (callId != null && !callId.isEmpty()) attrs.put("gen_ai.tool.call.id", callId);
        attrs.put("gen_ai.tool.type", "function");
        Map<String, Object> contextAttrs = new LinkedHashMap<>(context);
        Map<String, Object> content = new LinkedHashMap<>();
        if (arguments != null) {
            String input = bounded(arguments, config.maxBodyBytes);
            content.put("gen_ai.tool.call.arguments", input); content.put("input.value", input); content.put("input.mime_type", "application/json");
        }
        // The text a failed tool returns is the framework's error message, not a result.
        if (result != null && !error) {
            String output = bounded(result, config.maxBodyBytes);
            content.put("gen_ai.tool.call.result", output); content.put("output.value", output); content.put("output.mime_type", "text/plain");
        }
        Map<String, Object> candidates = new LinkedHashMap<>(attrs); candidates.putAll(contextAttrs); candidates.putAll(content);
        Map<String, Object> clean = AttributeProjection.sanitize(candidates, config, MATCH);
        List<String> order = new ArrayList<>(ORDER); order.addAll(contextAttrs.keySet()); order.addAll(content.keySet());
        Map<String, Object> projected = new LinkedHashMap<>();
        for (String key : order) {
            Object value = clean.get(key);
            if (value != null && !TraceAIAuto.hidden(key, MATCH, config)) projected.putIfAbsent(key, value);
        }
        return projected;
    }

    /** Wrapper exceptions name the framework's plumbing; the tool's own exception is the cause. */
    static Throwable unwrap(Throwable error) {
        Throwable current = error;
        for (int i = 0; i < 4 && current.getCause() != null && current.getCause() != current; i++) {
            String type = current.getClass().getName();
            if (current instanceof java.lang.reflect.InvocationTargetException || current instanceof java.lang.reflect.UndeclaredThrowableException
                || current instanceof java.util.concurrent.CompletionException || current instanceof java.util.concurrent.ExecutionException
                || type.equals("dev.langchain4j.exception.ToolExecutionException") || type.equals("org.springframework.ai.tool.execution.ToolExecutionException"))
                current = current.getCause();
            else break;
        }
        return current;
    }
    /** Calls a public no-argument method through the framework's public type, never through an implementation class. */
    private static Object call(Object target, String type, String method) {
        if (target == null) return null;
        try {
            ClassLoader loader = target.getClass().getClassLoader();
            Class<?> api = Class.forName(type, false, loader);
            return api.isInstance(target) ? api.getMethod(method).invoke(target) : null;
        } catch (Throwable ignored) { return null; }
    }
    private static String string(Object value) { return value instanceof String text ? text : null; }
    static String bounded(String value, int limit) {
        if (value == null || value.length() <= limit) return value;
        String marker = "...[truncated]";
        return limit <= marker.length() ? value.substring(0, Math.max(0, limit)) : value.substring(0, limit - marker.length()) + marker;
    }
    private ToolSpans() {}
}
