package ai.traceai.auto;

import net.bytebuddy.agent.builder.AgentBuilder;
import net.bytebuddy.asm.Advice;
import net.bytebuddy.description.type.TypeDescription;
import net.bytebuddy.dynamic.DynamicType;
import net.bytebuddy.utility.JavaModule;
import java.lang.instrument.Instrumentation;
import java.util.Map;
import static net.bytebuddy.matcher.ElementMatchers.*;

/** No application code is loaded from premain, and no exception escapes into the launcher. */
public final class TraceAIAgent {
    public static void premain(String args, Instrumentation instrumentation) { boot(instrumentation); }
    public static void agentmain(String args, Instrumentation instrumentation) { boot(instrumentation); }
    private static void boot(Instrumentation instrumentation) {
        try { TraceAIAuto.start(Map.of(), instrumentation); }
        catch (Throwable ignored) { TraceAIAuto.warn("Agent startup failed; application runs without auto-instrumentation"); }
    }
    static void install(Instrumentation instrumentation) {
        // Shade relocates this property along with ByteBuddy: it cannot alter an application's copy.
        if (Runtime.version().feature() > 24) System.setProperty("net.bytebuddy.experimental", "true");
        new AgentBuilder.Default().disableClassFormatChanges()
            .with(AgentBuilder.RedefinitionStrategy.RETRANSFORMATION)
            .ignore(nameStartsWith("ai.traceai.auto.").and(not(hasSuperType(namedOneOf("okio.Source", "okhttp3.ResponseBody"))))
                .or(nameStartsWith("net.bytebuddy.")))
            .with(new AgentBuilder.Listener.Adapter() {
                @Override public void onTransformation(TypeDescription type, ClassLoader loader, JavaModule module, boolean loaded, DynamicType dynamicType) {
                    TraceAIAuto.integrationInstrumented(type.getName().startsWith("okhttp3") ? "okhttp" : "java.net.http");
                }
                @Override public void onError(String type, ClassLoader loader, JavaModule module, boolean loaded, Throwable error) {
                    TraceAIAuto.integrationFailed(type.startsWith("okhttp3") ? "okhttp" : "java.net.http");
                    TraceAIAuto.debug("Transformation failed for " + type + " (" + error.getClass().getSimpleName() + ")");
                }
            })
            // Construct the application name dynamically so Shade does not relocate the matcher.
            .type(named(String.join(".", "io", "opentelemetry", "sdk", "trace", "TracerSharedState")))
            .transform((builder, type, loader, module, domain) -> builder.visit(Advice.to(ApplicationProcessorAdvice.class).on(named("getActiveSpanProcessor"))))
            .type(named("jdk.internal.net.http.HttpRequestImpl"))
            .transform((builder, type, loader, module, domain) -> builder
                .visit(Advice.to(JdkHeaderAdvice.class).on(namedOneOf("setSystemHeader", "addSystemHeader").and(takesArguments(String.class, String.class)))))
            .type(named("jdk.internal.net.http.ResponseSubscribers$HttpResponseInputStream"))
            .transform((builder, type, loader, module, domain) -> builder
                .visit(Advice.to(JdkBodyAdvice.class).on(named("getBody").and(takesArguments(0))))
                .visit(Advice.to(JdkCloseAdvice.class).on(named("close").and(takesArguments(0)))))
            .type(named("jdk.internal.net.http.HttpClientImpl"))
            .transform((builder, type, loader, module, domain) -> builder.visit(Advice.to(JdkAdvice.class).on(namedOneOf("send", "sendAsync").and(takesArguments(2).or(takesArguments(3))))))
            .type(hasSuperType(named("okhttp3.ResponseBody")).and(not(isInterface())))
            .transform((builder, type, loader, module, domain) -> builder.visit(Advice.to(OkHttpSourceAdvice.class).on(named("source").and(takesArguments(0)))))
            .type(hasSuperType(named("okio.Source")).and(not(isInterface())))
            .transform((builder, type, loader, module, domain) -> builder
                .visit(Advice.to(OkHttpReadAdvice.class).on(named("read").and(takesArguments(2)).and(takesArgument(0, named("okio.Buffer"))).and(returns(long.class))))
                .visit(Advice.to(OkHttpCloseAdvice.class).on(named("close").and(takesArguments(0)))))
            .type(named("okhttp3.internal.http.RealInterceptorChain"))
            .transform((builder, type, loader, module, domain) -> builder.visit(Advice.to(OkHttpHeadersAdvice.class).on(named("proceed").and(takesArgument(0, named("okhttp3.Request"))))))
            .type(namedOneOf("okhttp3.RealCall", "okhttp3.internal.connection.RealCall"))
            .transform((builder, type, loader, module, domain) -> builder.visit(Advice.to(OkHttpCallAdvice.class).on(namedOneOf("execute", "enqueue"))))
            .type(named("okhttp3.OkHttpClient"))
            .transform((builder, type, loader, module, domain) -> builder.visit(Advice.to(OkHttpAdvice.class).on(isConstructor().and(takesArguments(1)).and(takesArgument(0, named("okhttp3.OkHttpClient$Builder"))))))
            .installOn(instrumentation);
    }
    public static class ApplicationProcessorAdvice {
        @Advice.OnMethodExit(suppress = Throwable.class)
        public static void exit(@Advice.Return(readOnly = false, typing = net.bytebuddy.implementation.bytecode.assign.Assigner.Typing.DYNAMIC) Object processor) throws Exception {
            processor = ClassLoader.getSystemClassLoader().loadClass("ai.traceai.auto.TraceAIAuto").getMethod("applicationProcessor", Object.class).invoke(null, processor);
        }
    }
    public static class JdkHeaderAdvice {
        @Advice.OnMethodEnter(suppress = Throwable.class)
        public static void enter(@Advice.This Object request, @Advice.Argument(0) String name, @Advice.Argument(1) String value) throws Exception {
            ClassLoader.getSystemClassLoader().loadClass("ai.traceai.auto.TraceAIAuto").getMethod("jdkRequestHeader", Object.class, String.class, String.class).invoke(null, request, name, value);
        }
    }
    public static class JdkBodyAdvice {
        @Advice.OnMethodEnter(suppress = Throwable.class)
        public static void enter(@Advice.This Object stream) throws Exception {
            ClassLoader.getSystemClassLoader().loadClass("ai.traceai.auto.TraceAIAuto").getMethod("jdkBody", Object.class).invoke(null, stream);
        }
    }
    public static class JdkCloseAdvice {
        @Advice.OnMethodEnter(suppress = Throwable.class)
        public static void enter(@Advice.This Object stream) throws Exception {
            ClassLoader.getSystemClassLoader().loadClass("ai.traceai.auto.TraceAIAuto").getMethod("jdkClosed", Object.class).invoke(null, stream);
        }
    }
    public static class JdkAdvice {
        @Advice.OnMethodEnter(suppress = Throwable.class)
        public static Object enter(@Advice.Argument(value = 0, readOnly = false) java.net.http.HttpRequest request,
                                   @Advice.Argument(value = 1, readOnly = false) java.net.http.HttpResponse.BodyHandler<?> handler) throws Exception {
            Class<?> bridge = ClassLoader.getSystemClassLoader().loadClass("ai.traceai.auto.TraceAIAuto");
            Object[] state = (Object[])bridge.getMethod("enterJdk", Object.class, Object.class).invoke(null, request, handler);
            if (state != null) { request = (java.net.http.HttpRequest)state[0]; handler = (java.net.http.HttpResponse.BodyHandler<?>)state[1]; }
            return state;
        }
        @Advice.OnMethodExit(onThrowable = Throwable.class, suppress = Throwable.class)
        public static void exit(@Advice.Enter Object state, @Advice.Return(typing = net.bytebuddy.implementation.bytecode.assign.Assigner.Typing.DYNAMIC) Object returned,
                                @Advice.Thrown Throwable error) throws Exception {
            if (state != null) ClassLoader.getSystemClassLoader().loadClass("ai.traceai.auto.TraceAIAuto").getMethod("exitJdk", Object.class, Object.class, Throwable.class).invoke(null, state, returned, error);
        }
    }
    public static class OkHttpSourceAdvice {
        @Advice.OnMethodExit(suppress = Throwable.class)
        public static void exit(@Advice.This Object body, @Advice.Return(typing = net.bytebuddy.implementation.bytecode.assign.Assigner.Typing.DYNAMIC) Object source) throws Exception {
            ClassLoader.getSystemClassLoader().loadClass("ai.traceai.auto.TraceAIAuto").getMethod("okhttpSource", Object.class, Object.class).invoke(null, body, source);
        }
    }
    public static class OkHttpReadAdvice {
        @Advice.OnMethodEnter(suppress = Throwable.class)
        public static Object enter(@Advice.This Object source) throws Exception {
            return ClassLoader.getSystemClassLoader().loadClass("ai.traceai.auto.TraceAIAuto").getMethod("okhttpReading", Object.class).invoke(null, source);
        }
        @Advice.OnMethodExit(onThrowable = Throwable.class, suppress = Throwable.class)
        public static void exit(@Advice.Enter Object token, @Advice.Argument(0) Object sink, @Advice.Return long count, @Advice.Thrown Throwable error) throws Exception {
            long observed = System.nanoTime();
            if (token != null) ClassLoader.getSystemClassLoader().loadClass("ai.traceai.auto.TraceAIAuto").getMethod("okhttpRead", Object.class, Object.class, long.class, Throwable.class, long.class).invoke(null, token, sink, count, error, observed);
        }
    }
    public static class OkHttpCloseAdvice {
        @Advice.OnMethodEnter(suppress = Throwable.class)
        public static Object enter(@Advice.This Object source) throws Exception {
            return ClassLoader.getSystemClassLoader().loadClass("ai.traceai.auto.TraceAIAuto").getMethod("okhttpReading", Object.class).invoke(null, source);
        }
        @Advice.OnMethodExit(onThrowable = Throwable.class, suppress = Throwable.class)
        public static void exit(@Advice.Enter Object token, @Advice.Thrown Throwable error) throws Exception {
            if (token != null) ClassLoader.getSystemClassLoader().loadClass("ai.traceai.auto.TraceAIAuto").getMethod("okhttpClosed", Object.class, Throwable.class).invoke(null, token, error);
        }
    }
    public static class OkHttpHeadersAdvice {
        @Advice.OnMethodEnter(suppress = Throwable.class)
        public static void enter(@Advice.Argument(0) Object request) throws Exception {
            ClassLoader.getSystemClassLoader().loadClass("ai.traceai.auto.TraceAIAuto").getMethod("okhttpRequestHeaders", Object.class).invoke(null, request);
        }
    }
    public static class OkHttpCallAdvice {
        @Advice.OnMethodEnter(suppress = Throwable.class)
        public static void enter(@Advice.This Object call) throws Exception {
            ClassLoader.getSystemClassLoader().loadClass("ai.traceai.auto.TraceAIAuto").getMethod("requestOkHttp", Object.class).invoke(null, call);
        }
    }
    public static class OkHttpAdvice {
        @Advice.OnMethodEnter(suppress = Throwable.class)
        public static void enter(@Advice.Argument(0) Object builder) throws Exception {
            ClassLoader.getSystemClassLoader().loadClass("ai.traceai.auto.TraceAIAuto").getMethod("installOkHttp", Object.class).invoke(null, builder);
        }
    }
    private TraceAIAgent() {}
}
