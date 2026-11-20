package ai.traceai.auto;

import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.api.common.Attributes;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.api.trace.SpanKind;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.context.Context;
import io.opentelemetry.sdk.resources.Resource;
import net.bytebuddy.agent.builder.AgentBuilder;
import net.bytebuddy.asm.Advice;
import net.bytebuddy.description.type.TypeDescription;
import net.bytebuddy.dynamic.DynamicType;
import net.bytebuddy.utility.JavaModule;
import java.lang.instrument.Instrumentation;
import java.lang.reflect.Method;
import java.lang.reflect.Modifier;
import java.time.Instant;
import java.util.*;
import java.util.concurrent.TimeUnit;
import static net.bytebuddy.matcher.ElementMatchers.*;

/**
 * Adds one {@code execute_tool <name>} INTERNAL span per tool execution of LangChain4j and Spring AI, with no code change.
 *
 * <p>LangChain4j: {@code ToolExecutor#execute(ToolExecutionRequest, Object)} and, in LangChain4j versions that have it,
 * {@code executeWithContext}, on every class that implements {@code dev.langchain4j.service.tool.ToolExecutor} (this
 * covers {@code DefaultToolExecutor}, which AiServices uses for {@code @Tool} methods). Spring AI: both {@code call}
 * overloads of every class that implements {@code org.springframework.ai.tool.ToolCallback}. The classes are matched by
 * name, so nothing from either library is loaded by the agent.
 *
 * <p>The span's parent is the application's active span, as for every agent span. It carries the operation, tool name, call
 * id (LangChain4j, which passes it), description (Spring AI, which exposes it), arguments as {@code input.value} and the result as
 * {@code output.value}. Content is withheld by the same hide switches as the rest of the agent, and a failure is recorded
 * with the exception rethrown unchanged to the application.
 *
 * <p>No second span: a call nested directly in another advised call of the same tool and arguments on the thread (for
 * example {@code call(String)} delegating to {@code call(String, ToolContext)}) is not traced again, nor is a call made
 * under an application TOOL span for the same tool (a traceai-java-langchain4j or traceai-java-spring-ai wrapper).
 * {@code FI_DISABLED_INSTRUMENTATIONS=langchain4j} or {@code spring-ai} turns each off, and an {@code FI_INSTRUMENTATIONS}
 * allow list without the name does the same.
 */
public final class ToolSpans {
    static final String LANGCHAIN4J = "langchain4j", SPRING_AI = "spring-ai";
    private static final String EXECUTOR = "dev.langchain4j.service.tool.ToolExecutor", REQUEST = "dev.langchain4j.agent.tool.ToolExecutionRequest";
    private static final String CALLBACK = "org.springframework.ai.tool.ToolCallback";
    /** Fingerprints of the advised calls running on this thread, innermost first. */
    private static final ThreadLocal<Deque<String>> ACTIVE = ThreadLocal.withInitial(ArrayDeque::new);
    private record Token(String integration, String fingerprint, String name, String callId, String description, String input, OtelBridge.Snapshot context, TraceAIAuto.RuntimeState state) {}

    static void install(Instrumentation instrumentation) {
        AutoConfig config = TraceAIAuto.config();
        boolean langchain4j = config != null && config.enabled(LANGCHAIN4J), springAi = config != null && config.enabled(SPRING_AI);
        if (!langchain4j && !springAi) return;
        try {
            // Shade relocates this property along with ByteBuddy: it cannot alter an application's copy.
            if (Runtime.version().feature() > 24) System.setProperty("net.bytebuddy.experimental", "true");
            AgentBuilder builder = new AgentBuilder.Default().disableClassFormatChanges()
                .with(AgentBuilder.RedefinitionStrategy.RETRANSFORMATION)
                // Never match the OpenTelemetry Java agent's own classes, by name or by its class loaders.
                .ignore(nameStartsWith(OtelJavaAgent.PREFIX).or(nameStartsWith(String.join(".", "net", "bytebuddy", ""))))
                .or(any(), loader -> loader != null && loader.getClass().getName().startsWith(OtelJavaAgent.PREFIX))
                .with(new AgentBuilder.Listener.Adapter() {
                    @Override public void onTransformation(TypeDescription type, ClassLoader loader, JavaModule module, boolean loaded, DynamicType dynamicType) {
                        TraceAIAuto.toolIntegrationInstrumented(integration(type));
                    }
                    @Override public void onError(String type, ClassLoader loader, JavaModule module, boolean loaded, Throwable error) {
                        TraceAIAuto.integrationFailed(type.startsWith("org.springframework") ? SPRING_AI : LANGCHAIN4J);
                        TraceAIAuto.debug("Transformation failed for " + type + " (" + error.getClass().getSimpleName() + ")");
                    }
                });
            if (langchain4j) builder = builder.type(hasSuperType(named(EXECUTOR)).and(not(isInterface())))
                .transform((b, type, loader, module, domain) -> b.visit(Advice.to(LangChain4jAdvice.class)
                    .on(namedOneOf("execute", "executeWithContext").and(takesArguments(2)).and(takesArgument(0, named(REQUEST))))));
            if (springAi) builder = builder.type(hasSuperType(named(CALLBACK)).and(not(isInterface())))
                .transform((b, type, loader, module, domain) -> b.visit(Advice.to(SpringAiAdvice.class)
                    .on(named("call").and(takesArguments(1).or(takesArguments(2))).and(takesArgument(0, String.class)))));
            builder.installOn(instrumentation);
        } catch (Throwable ignored) { TraceAIAuto.integrationFailed(langchain4j ? LANGCHAIN4J : SPRING_AI); }
    }
    private static String integration(TypeDescription type) {
        try { return type.getInterfaces().asErasures().stream().anyMatch(t -> t.getName().equals(CALLBACK)) || hasSuper(type, CALLBACK) ? SPRING_AI : LANGCHAIN4J; }
        catch (Throwable ignored) { return LANGCHAIN4J; }
    }
    private static boolean hasSuper(TypeDescription type, String name) {
        Deque<TypeDescription.Generic> pending = new ArrayDeque<>();
        if (type.getSuperClass() != null) pending.add(type.getSuperClass());
        pending.addAll(type.getInterfaces());
        while (!pending.isEmpty()) {
            TypeDescription next = pending.pop().asErasure();
            if (next.getName().equals(name)) return true;
            if (next.getSuperClass() != null) pending.add(next.getSuperClass());
            pending.addAll(next.getInterfaces());
        }
        return false;
    }

    /** Called reflectively from the advice; returns an opaque token, or null when this call is not traced. */
    public static Object enter(String integration, Object callee, Object[] arguments) {
        try {
            AutoConfig config = TraceAIAuto.config(); TraceAIAuto.RuntimeState state = TraceAIAuto.currentRuntime();
            if (config == null || state == null || !config.enabled(integration) || TraceAIAuto.besideOtelJavaAgent() || !state.acceptingResults()) return null;
            if (TraceAIAuto.IN_FLIGHT.get() || Boolean.TRUE.equals(Context.current().get(TraceAIAuto.SUPPRESS))) return null;
            String name, callId = null, description = null, input;
            if (integration.equals(LANGCHAIN4J)) {
                Object request = arguments[0];
                name = text(invoke(request, "name")); callId = text(invoke(request, "id")); input = text(invoke(request, "arguments"));
            } else {
                Object definition = invoke(callee, "getToolDefinition");
                name = text(invoke(definition, "name")); description = text(invoke(definition, "description")); input = (String)arguments[0];
            }
            if (name == null) return null;
            String fingerprint = integration + '\0' + name + '\0' + input;
            Deque<String> active = ACTIVE.get();
            if (fingerprint.equals(active.peek())) return null;
            OtelBridge.Snapshot context = OtelBridge.snapshot();
            if (OtelBridge.toolSpanActive(name)) return null;
            active.push(fingerprint);
            return new Token(integration, fingerprint, name, callId, description, input, context, state);
        } catch (Throwable ignored) { return null; }
    }

    /** Called reflectively when the advised call returns or throws, on the thread that entered it. */
    public static void exit(Object token, Object returned, Throwable error) {
        if (!(token instanceof Token tool)) return;
        Deque<String> active = ACTIVE.get();
        active.remove(tool.fingerprint());
        if (active.isEmpty()) ACTIVE.remove();
        TraceAIAuto.RuntimeState state = tool.state();
        boolean finalizing = false;
        try {
            if (!state.startFinalizing()) return;
            finalizing = true;
            long endEpochNanos = epochNanos(Instant.now());
            AutoConfig config = state.config();
            Resource resource = tool.context().resource() == null ? state.resource : TraceAIAuto.withRouting(tool.context().resource(), state.resource);
            int limit = Math.min(state.provider().getSpanLimits().getMaxNumberOfAttributes(),
                tool.context().attributeLimit() == null ? Integer.MAX_VALUE : tool.context().attributeLimit());
            Span span = state.tracer().spanBuilder("execute_tool " + tool.name()).setSpanKind(SpanKind.INTERNAL).setParent(tool.context().parent())
                .setStartTimestamp(tool.context().startEpochNanos(), TimeUnit.NANOSECONDS).startSpan();
            try {
                tool.context().attributes().forEach(span::setAttribute);
                span.setAttribute("fi.span.kind", "TOOL"); span.setAttribute("gen_ai.span.kind", "tool");
                span.setAttribute("gen_ai.operation.name", "execute_tool"); span.setAttribute("gen_ai.tool.name", tool.name());
                if (tool.callId() != null) span.setAttribute("gen_ai.tool.call.id", tool.callId());
                String description = content(config, "gen_ai.tool.description", tool.description());
                if (description != null) span.setAttribute("gen_ai.tool.description", description);
                String input = content(config, "input.value", tool.input());
                if (input != null) span.setAttribute("input.value", input);
                if (error == null) {
                    String output = content(config, "output.value", result(returned));
                    if (output != null) span.setAttribute("output.value", output);
                    span.setStatus(StatusCode.OK);
                } else {
                    span.setAttribute("error.type", error.getClass().getName());
                    if (TraceAIAuto.hidesErrorText(config)) {
                        span.setStatus(StatusCode.ERROR);
                        span.addEvent("exception", Attributes.of(AttributeKey.stringKey("exception.type"), error.getClass().getName()));
                    } else {
                        span.setStatus(StatusCode.ERROR, String.valueOf(error.getMessage()));
                        span.recordException(error);
                        if (error.getMessage() != null) span.setAttribute("error.message", error.getMessage());
                    }
                }
            } finally { WireResourceProcessor.end(span, endEpochNanos, resource, limit); }
        } catch (Throwable ignored) {
        } finally { if (finalizing) state.endFinalizing(); }
    }

    /** Content goes through the shared key table first, then the agent's own switches, so hidden text never reaches the span. */
    private static String content(AutoConfig config, String key, String value) {
        if (value == null) return null;
        String masked = config.masking.maskAttribute(key, value);
        return masked == null || TraceAIAuto.hidden(key, value, null, config) ? null : masked;
    }
    private static String result(Object returned) {
        if (returned == null) return null;
        if (returned instanceof String text) return text;
        // LangChain4j versions with executeWithContext return a ToolExecutionResult holding the text.
        String text = text(invoke(returned, "resultText"));
        return text;
    }
    private static String text(Object value) { return value == null ? null : String.valueOf(value); }
    private static long epochNanos(Instant instant) { return instant.getEpochSecond() * 1_000_000_000L + instant.getNano(); }

    /** Calls a public no-argument method through the first public type that declares it: the object's class may not be public. */
    private static Object invoke(Object target, String name) {
        if (target == null) return null;
        try {
            Deque<Class<?>> pending = new ArrayDeque<>(List.of(target.getClass()));
            Set<Class<?>> seen = new HashSet<>();
            while (!pending.isEmpty()) {
                Class<?> type = pending.pop();
                if (type == null || !seen.add(type)) continue;
                if (Modifier.isPublic(type.getModifiers())) {
                    try { Method method = type.getMethod(name); if (Modifier.isPublic(method.getDeclaringClass().getModifiers())) return method.invoke(target); }
                    catch (NoSuchMethodException ignored) { }
                }
                pending.addAll(Arrays.asList(type.getInterfaces())); pending.add(type.getSuperclass());
            }
        } catch (Throwable ignored) { }
        return null;
    }

    public static class LangChain4jAdvice {
        @Advice.OnMethodEnter(suppress = Throwable.class)
        public static Object enter(@Advice.This Object executor, @Advice.AllArguments Object[] arguments) throws Exception {
            return ClassLoader.getSystemClassLoader().loadClass("ai.traceai.auto.ToolSpans").getMethod("enter", String.class, Object.class, Object[].class).invoke(null, "langchain4j", executor, arguments);
        }
        @Advice.OnMethodExit(onThrowable = Throwable.class, suppress = Throwable.class)
        public static void exit(@Advice.Enter Object token, @Advice.Return(typing = net.bytebuddy.implementation.bytecode.assign.Assigner.Typing.DYNAMIC) Object returned,
                                @Advice.Thrown Throwable error) throws Exception {
            if (token != null) ClassLoader.getSystemClassLoader().loadClass("ai.traceai.auto.ToolSpans").getMethod("exit", Object.class, Object.class, Throwable.class).invoke(null, token, returned, error);
        }
    }
    public static class SpringAiAdvice {
        @Advice.OnMethodEnter(suppress = Throwable.class)
        public static Object enter(@Advice.This Object callback, @Advice.AllArguments Object[] arguments) throws Exception {
            return ClassLoader.getSystemClassLoader().loadClass("ai.traceai.auto.ToolSpans").getMethod("enter", String.class, Object.class, Object[].class).invoke(null, "spring-ai", callback, arguments);
        }
        @Advice.OnMethodExit(onThrowable = Throwable.class, suppress = Throwable.class)
        public static void exit(@Advice.Enter Object token, @Advice.Return(typing = net.bytebuddy.implementation.bytecode.assign.Assigner.Typing.DYNAMIC) Object returned,
                                @Advice.Thrown Throwable error) throws Exception {
            if (token != null) ClassLoader.getSystemClassLoader().loadClass("ai.traceai.auto.ToolSpans").getMethod("exit", Object.class, Object.class, Throwable.class).invoke(null, token, returned, error);
        }
    }
    private ToolSpans() {}
}
