package ai.traceai.auto;

import io.opentelemetry.api.trace.Span;
import io.opentelemetry.api.trace.SpanContext;
import io.opentelemetry.api.trace.propagation.W3CTraceContextPropagator;
import io.opentelemetry.context.Context;
import io.opentelemetry.context.Scope;
import io.opentelemetry.context.propagation.TextMapGetter;
import net.bytebuddy.agent.builder.AgentBuilder;
import net.bytebuddy.asm.Advice;
import net.bytebuddy.description.type.TypeDescription;
import net.bytebuddy.dynamic.DynamicType;
import net.bytebuddy.utility.JavaModule;
import java.lang.instrument.Instrumentation;
import java.lang.reflect.Method;
import java.util.*;
import static net.bytebuddy.matcher.ElementMatchers.*;

/**
 * Joins the caller's trace on incoming servlet requests.
 *
 * <p>While {@code HttpServlet#service} runs (jakarta.servlet and javax.servlet, so Spring MVC on
 * Tomcat, Jetty and Undertow too), the request's W3C {@code traceparent} and {@code tracestate}
 * become the agent's current context on the request thread. AI calls made there take the remote
 * span as their parent, and the agent's parent-based sampler applies the caller's sampled flag and
 * {@code fi=r:} ratio exactly as for any other remote parent. No SERVER span is created or exported.
 * An absent or invalid {@code traceparent} changes nothing. The previous context is restored when
 * {@code service} returns or throws, including when the request goes asynchronous. An async
 * dispatch runs service again and extracts the headers afresh on its own thread; a forward or include
 * runs inside the original call and keeps its context.
 *
 * <p>The application's own active span still wins: OtelBridge reads it first. Only header values
 * are read; nothing about the request is recorded.
 */
public final class IncomingRequests {
    static final String INTEGRATION = "servlet";
    private static final Set<String> SERVLETS = Set.of("jakarta.servlet.http.HttpServlet", "javax.servlet.http.HttpServlet");
    private static final Set<String> REQUESTS = Set.of("jakarta.servlet.http.HttpServletRequest", "javax.servlet.http.HttpServletRequest");
    /** Set while the outermost service call of a request runs on this thread; nested calls change nothing. */
    private static final ThreadLocal<Boolean> INSIDE = new ThreadLocal<>();
    /** The request's getHeaders(String), looked up on the servlet API interface: request classes are
     * often not public (JDK proxies, container facades), so their own method objects cannot be invoked. */
    private static final ClassValue<Method> HEADERS = new ClassValue<>() {
        @Override protected Method computeValue(Class<?> type) {
            Deque<Class<?>> pending = new ArrayDeque<>(List.of(type));
            Set<Class<?>> seen = new HashSet<>();
            while (!pending.isEmpty()) {
                Class<?> next = pending.pop();
                if (next == null || !seen.add(next)) continue;
                if (next.isInterface() && REQUESTS.contains(next.getName())) {
                    try { return next.getMethod("getHeaders", String.class); } catch (NoSuchMethodException ignored) { return null; }
                }
                pending.addAll(Arrays.asList(next.getInterfaces()));
                pending.add(next.getSuperclass());
            }
            return null;
        }
    };
    private static final TextMapGetter<Object> GETTER = new TextMapGetter<>() {
        @Override public Iterable<String> keys(Object request) { return List.of(); }
        @Override public String get(Object request, String key) {
            List<String> values = headers(request, key);
            if (values.isEmpty()) return null;
            // A repeated traceparent is ambiguous: treat it as invalid, as an absent one. W3C
            // tracestate is a list header whose repeated fields combine in order.
            if (key.equals("traceparent")) return values.size() == 1 ? values.get(0) : null;
            return String.join(",", values);
        }
    };
    /** Undo record for the outermost service call; scope is null when the request carried no valid parent. */
    private record Token(Scope scope) {}

    static void install(Instrumentation instrumentation) {
        try {
            // Shade relocates this property along with ByteBuddy: it cannot alter an application's copy.
            if (Runtime.version().feature() > 24) System.setProperty("net.bytebuddy.experimental", "true");
            new AgentBuilder.Default().disableClassFormatChanges()
                .with(AgentBuilder.RedefinitionStrategy.RETRANSFORMATION)
                // Never match the OpenTelemetry Java agent's own classes, by name or by its class loaders.
                .ignore(nameStartsWith(OtelJavaAgent.PREFIX))
                .or(any(), loader -> loader != null && loader.getClass().getName().startsWith(OtelJavaAgent.PREFIX))
                .with(new AgentBuilder.Listener.Adapter() {
                    @Override public void onTransformation(TypeDescription type, ClassLoader loader, JavaModule module, boolean loaded, DynamicType dynamicType) {
                        TraceAIAuto.integrationInstrumented(INTEGRATION);
                    }
                    @Override public void onError(String type, ClassLoader loader, JavaModule module, boolean loaded, Throwable error) {
                        TraceAIAuto.integrationFailed(INTEGRATION);
                        TraceAIAuto.debug("Transformation failed for " + type + " (" + error.getClass().getSimpleName() + ")");
                    }
                })
                // Only the servlet base classes: every container calls the public service method,
                // and frameworks (Spring's FrameworkServlet) override the protected one beneath it.
                .type(namedOneOf(SERVLETS.toArray(String[]::new)))
                .transform((builder, type, loader, module, domain) -> builder.visit(Advice.to(ServiceAdvice.class).on(named("service").and(isPublic())
                    .and(takesArguments(2)).and(takesArgument(0, namedOneOf("jakarta.servlet.ServletRequest", "javax.servlet.ServletRequest"))))))
                .installOn(instrumentation);
        } catch (Throwable ignored) { TraceAIAuto.integrationFailed(INTEGRATION); }
    }

    /** Called reflectively from the servlet advice; returns an undo token, or null when nothing changed. */
    public static Object enter(Object request) {
        try {
            if (INSIDE.get() != null) return null;
            if (!TraceAIAuto.transportEnabled(INTEGRATION) || TraceAIAuto.besideOtelJavaAgent()) return null;
            Context current = Context.current();
            Context extracted = W3CTraceContextPropagator.getInstance().extract(current, request, GETTER);
            SpanContext remote = Span.fromContext(extracted).getSpanContext();
            Scope scope = extracted != current && remote.isValid() && remote.isRemote() ? extracted.makeCurrent() : null;
            INSIDE.set(Boolean.TRUE);
            return new Token(scope);
        } catch (Throwable ignored) { return null; }
    }
    /** Called reflectively when service returns or throws, on the thread that entered it. */
    public static void exit(Object token) {
        if (!(token instanceof Token undo)) return;
        try { if (undo.scope() != null) undo.scope().close(); }
        catch (Throwable ignored) { }
        finally { INSIDE.remove(); }
    }
    static List<String> headers(Object request, String name) {
        try {
            Method method = request == null ? null : HEADERS.get(request.getClass());
            if (method == null) return List.of();
            Object values = method.invoke(request, name);
            if (!(values instanceof Enumeration<?> e)) return List.of();
            List<String> result = new ArrayList<>(1);
            while (e.hasMoreElements()) { Object value = e.nextElement(); if (value != null) result.add(String.valueOf(value)); }
            return result;
        } catch (Throwable ignored) { return List.of(); }
    }

    public static class ServiceAdvice {
        @Advice.OnMethodEnter(suppress = Throwable.class)
        public static Object enter(@Advice.Argument(0) Object request) throws Exception {
            return ClassLoader.getSystemClassLoader().loadClass("ai.traceai.auto.IncomingRequests").getMethod("enter", Object.class).invoke(null, request);
        }
        @Advice.OnMethodExit(onThrowable = Throwable.class, suppress = Throwable.class)
        public static void exit(@Advice.Enter Object token) throws Exception {
            if (token != null) ClassLoader.getSystemClassLoader().loadClass("ai.traceai.auto.IncomingRequests").getMethod("exit", Object.class).invoke(null, token);
        }
    }
    private IncomingRequests() {}
}
