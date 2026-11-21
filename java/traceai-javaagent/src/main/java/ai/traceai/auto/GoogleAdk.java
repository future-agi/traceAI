package ai.traceai.auto;

import ai.traceai.googleadk.AdkAttributes;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.api.trace.SpanKind;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.context.Context;
import net.bytebuddy.agent.builder.AgentBuilder;
import net.bytebuddy.asm.Advice;
import net.bytebuddy.description.type.TypeDescription;
import net.bytebuddy.dynamic.DynamicType;
import net.bytebuddy.utility.JavaModule;

import java.lang.instrument.Instrumentation;
import java.lang.reflect.InvocationHandler;
import java.lang.reflect.InvocationTargetException;
import java.lang.reflect.Method;
import java.lang.reflect.Proxy;
import java.time.Instant;
import java.util.*;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;

import static net.bytebuddy.matcher.ElementMatchers.*;

/**
 * Traces Google ADK for Java (1.0.0 to 1.15.0) with no application code.
 *
 * <p>ADK starts its own OpenTelemetry spans (invocation, invoke_agent, call_llm, execute_tool) from the tracer
 * {@code GlobalOpenTelemetry.getTracer("gcp.vertex.agent")}, and reports each span's content to
 * {@code com.google.adk.telemetry.Tracing}'s {@code trace*} methods. The agent does two things:
 *
 * <ul>
 * <li>it hands ADK a tracer that mirrors every span ADK starts: the span the application's own OpenTelemetry setup
 * would record is unchanged (or a no-op, when the application has none), and the agent records its own span beside
 * it, with the same parent, start and end times;
 * <li>it reads the content ADK passes to {@code Tracing.traceCallLlm}, {@code traceToolExecution} and
 * {@code traceAgentInvocation} (ADK writes it to its own spans only when ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS is set),
 * and maps it to the shared traceAI attributes ({@link AdkAttributes}).
 * </ul>
 *
 * <p>The agent's span is exported through the agent's route with its privacy rules; the application's spans are not
 * touched. While an ADK model call runs, the mirrored span is the application's current span, so the model's HTTP
 * call is not traced a second time by the wire capture.
 */
public final class GoogleAdk {
    static final String INTEGRATION = "google-adk";
    private static final String SCOPE = "gcp.vertex.agent";
    private static final String OTEL = String.join(".", "io", "opentelemetry");

    private GoogleAdk() {}

    static void install(Instrumentation instrumentation) {
        try {
            if (Runtime.version().feature() > 24) System.setProperty("net.bytebuddy.experimental", "true");
            new AgentBuilder.Default().disableClassFormatChanges()
                .with(AgentBuilder.RedefinitionStrategy.RETRANSFORMATION)
                .ignore(nameStartsWith(OtelJavaAgent.PREFIX))
                .or(any(), loader -> loader != null && loader.getClass().getName().startsWith(OtelJavaAgent.PREFIX))
                .with(new AgentBuilder.Listener.Adapter() {
                    @Override public void onTransformation(TypeDescription type, ClassLoader loader, JavaModule module, boolean loaded, DynamicType dynamicType) {
                        if (type.getName().startsWith("com.google.adk.") && !TraceAIAuto.besideOtelJavaAgent()) TraceAIAuto.integrationInstrumented(INTEGRATION);
                    }
                    @Override public void onError(String type, ClassLoader loader, JavaModule module, boolean loaded, Throwable error) {
                        if (type.startsWith("com.google.adk.") || type.equals(OTEL + ".api.GlobalOpenTelemetry")) {
                            TraceAIAuto.integrationFailed(INTEGRATION);
                            TraceAIAuto.debug("Transformation failed for " + type + " (" + error.getClass().getSimpleName() + ")");
                        }
                    }
                })
                // The tracer ADK asks for by its instrumentation scope name.
                .type(named(OTEL + ".api.GlobalOpenTelemetry"))
                .transform((builder, type, loader, module, domain) -> builder.visit(Advice.to(TracerAdvice.class)
                    .on(named("getTracer").and(isStatic()).and(takesArgument(0, String.class)).and(returns(named(OTEL + ".api.trace.Tracer"))))))
                // The content ADK reports for each span.
                .type(named("com.google.adk.telemetry.Tracing"))
                .transform((builder, type, loader, module, domain) -> builder.visit(Advice.to(TraceAdvice.class)
                    .on(namedOneOf("traceAgentInvocation", "traceToolExecution", "traceCallLlm").and(isStatic()).and(isPublic()))))
                .installOn(instrumentation);
        } catch (Throwable ignored) { TraceAIAuto.integrationFailed(INTEGRATION); }
    }

    public static class TracerAdvice {
        @Advice.OnMethodExit(suppress = Throwable.class)
        public static void exit(@Advice.Argument(0) String scope, @Advice.Origin Class<?> origin,
                                @Advice.Return(readOnly = false, typing = net.bytebuddy.implementation.bytecode.assign.Assigner.Typing.DYNAMIC) Object tracer) throws Exception {
            if (tracer == null || !"gcp.vertex.agent".equals(scope)) return;
            Object wrapped = ClassLoader.getSystemClassLoader().loadClass("ai.traceai.auto.GoogleAdk")
                .getMethod("wrapTracer", Object.class, ClassLoader.class).invoke(null, tracer, origin.getClassLoader());
            if (wrapped != null) tracer = wrapped;
        }
    }

    public static class TraceAdvice {
        @Advice.OnMethodEnter(suppress = Throwable.class)
        public static void enter(@Advice.Origin("#m") String method, @Advice.AllArguments Object[] arguments) throws Exception {
            ClassLoader.getSystemClassLoader().loadClass("ai.traceai.auto.GoogleAdk").getMethod("trace", String.class, Object[].class).invoke(null, method, arguments);
        }
    }

    // ---- The mirrored tracer ---------------------------------------------------------------------------------

    /** The application's OpenTelemetry API, read by name from the class loader ADK uses. */
    private record Api(Class<?> tracer, Class<?> spanBuilder, Class<?> span, Class<?> context, Method fromContext, Method current) {
        static Api of(ClassLoader loader) throws ReflectiveOperationException {
            Class<?> span = Class.forName(OTEL + ".api.trace.Span", false, loader), context = Class.forName(OTEL + ".context.Context", false, loader);
            return new Api(Class.forName(OTEL + ".api.trace.Tracer", false, loader), Class.forName(OTEL + ".api.trace.SpanBuilder", false, loader), span, context,
                span.getMethod("fromContext", context), context.getMethod("current"));
        }
    }
    private static final Map<ClassLoader, Api> APIS = new ConcurrentHashMap<>();

    /** Called reflectively from the tracer advice: a tracer for ADK that mirrors its spans. */
    public static Object wrapTracer(Object tracer, ClassLoader loader) {
        try {
            if (TraceAIAuto.besideOtelJavaAgent()) return null;
            AutoConfig config = TraceAIAuto.config();
            if (config != null && !config.enabled(INTEGRATION)) return null;
            ClassLoader useful = loader == null ? ClassLoader.getSystemClassLoader() : loader;
            Api api = APIS.computeIfAbsent(useful, key -> { try { return Api.of(key); } catch (ReflectiveOperationException e) { return null; } });
            if (api == null) return null;
            return Proxy.newProxyInstance(useful, new Class<?>[]{api.tracer()}, new TracerHandler(tracer, api));
        } catch (Throwable ignored) { return null; }
    }

    private static Object call(Method method, Object target, Object[] args) throws Throwable {
        try { return method.invoke(target, args); }
        catch (InvocationTargetException error) { throw error.getCause(); }
    }

    private static Object defaultMethod(Object proxy, Method method, Object[] args) throws Throwable {
        return InvocationHandler.invokeDefault(proxy, method, args);
    }

    private record TracerHandler(Object delegate, Api api) implements InvocationHandler {
        @Override public Object invoke(Object proxy, Method method, Object[] args) throws Throwable {
            Object result = call(method, delegate, args);
            if (method.getName().equals("spanBuilder") && result != null && method.getParameterCount() == 1) {
                String name = String.valueOf(args[0]);
                return Proxy.newProxyInstance(api.spanBuilder().getClassLoader(), new Class<?>[]{api.spanBuilder()}, new BuilderHandler(result, name, api));
            }
            return result;
        }
    }

    private static final class BuilderHandler implements InvocationHandler {
        private final Object delegate;
        private final String name;
        private final Api api;
        private Object parent;
        private boolean noParent;
        private long startEpochNanos = -1;
        private final Map<String, Object> attributes = new LinkedHashMap<>();

        BuilderHandler(Object delegate, String name, Api api) { this.delegate = delegate; this.name = name; this.api = api; }

        @Override public Object invoke(Object proxy, Method method, Object[] args) throws Throwable {
            if (method.getDeclaringClass() == Object.class) return method.invoke(delegate, args);
            String methodName = method.getName();
            if (methodName.equals("startSpan")) return start(proxy, method, args);
            if (method.isDefault()) return defaultMethod(proxy, method, args);
            switch (methodName) {
                case "setParent" -> { parent = args[0]; noParent = false; }
                case "setNoParent" -> { parent = null; noParent = true; }
                case "setStartTimestamp" -> startEpochNanos = epoch(args);
                case "setAttribute" -> { if (args.length == 2) record(attributes, args[0], args[1]); }
                default -> { }
            }
            Object result = call(method, delegate, args);
            return result != null && method.getReturnType() == api.spanBuilder() ? proxy : result;
        }

        private Object start(Object proxy, Method method, Object[] args) throws Throwable {
            long startNanos = System.nanoTime();
            Object span = call(method, delegate, args);
            try {
                long epoch = startEpochNanos >= 0 ? startEpochNanos : Instant.now().getEpochSecond() * 1_000_000_000L + Instant.now().getNano();
                Object context = parent != null ? parent : noParent ? null : api.current().invoke(null);
                AdkSpan handler = new AdkSpan(span, name, api, context, epoch, startNanos, attributes);
                try { return Proxy.newProxyInstance(api.span().getClassLoader(), new Class<?>[]{api.span()}, handler); }
                catch (Throwable failed) { handler.deregister(); throw failed; }
            } catch (Throwable ignored) { return span; }
        }
    }

    private static long epoch(Object[] args) {
        if (args.length == 2 && args[0] instanceof Number n && args[1] instanceof TimeUnit unit) return unit.toNanos(n.longValue());
        if (args.length == 1 && args[0] instanceof Instant instant) return instant.getEpochSecond() * 1_000_000_000L + instant.getNano();
        return Instant.now().getEpochSecond() * 1_000_000_000L;
    }

    private static void record(Map<String, Object> attributes, Object key, Object value) {
        try {
            if (key instanceof String name) { attributes.put(name, value); return; }
            Object name = key.getClass().getMethod("getKey").invoke(key);
            attributes.put(String.valueOf(name), value);
        } catch (Throwable ignored) { }
    }

    /** The application-visible span ADK holds; every call also reaches the span the application's SDK made. */
    static final class AdkSpan implements InvocationHandler {
        private final Object delegate;
        private final Api api;
        final String name;
        final String kind;
        private final AdkSpan parent;
        private final OtelBridge.Snapshot snapshot;
        private final long startEpochNanos;
        private final Map<String, Object> attributes;
        private final AtomicBoolean ended = new AtomicBoolean();
        /** The agent's own span; null when the agent is not recording this one. */
        private final Span shadow;
        private final TraceAIAuto.RuntimeState state;
        /** What the in-flight registry holds for this span: an identity, never any content. */
        private final Object flight = new Object();
        private volatile boolean answered;
        /** True for a call made through TracedLlm or TracedTool while the skip option is on: the agent records no span of its own for it. */
        private volatile boolean skipped;
        /** True once a span the agent records was parented to this span's own span: it must then be exported even if the call is skipped. */
        private volatile boolean shadowUsed;
        private volatile Map<String, Object> request, response;
        private volatile String toolName, toolDescription, toolType, agentName, agentDescription, sessionId, userId;
        private volatile Object toolArguments, toolResult;
        private volatile String toolCallId, errorText;
        private volatile boolean failed;
        private volatile String inputText, outputText;
        private volatile Throwable thrown;

        AdkSpan(Object delegate, String name, Api api, Object context, long startEpochNanos, long startNanos, Map<String, Object> started) {
            this.delegate = delegate; this.name = name; this.api = api; this.startEpochNanos = startEpochNanos;
            this.attributes = new LinkedHashMap<>(started);
            this.kind = kindOf(name);
            AdkSpan parentSpan = null;
            try {
                Object holder = context == null ? null : api.fromContext().invoke(null, context);
                if (holder != null && Proxy.isProxyClass(holder.getClass()) && Proxy.getInvocationHandler(holder) instanceof AdkSpan adk) parentSpan = adk;
            } catch (Throwable ignored) { }
            this.parent = parentSpan;
            OtelBridge.Snapshot captured = null; Span span = null; TraceAIAuto.RuntimeState runtime = null;
            try {
                runtime = TraceAIAuto.currentRuntime();
                if (runtime != null && kind != null && TraceAIAuto.integrationEnabled(INTEGRATION) && !TraceAIAuto.besideOtelJavaAgent()) {
                    captured = parentSpan != null ? parentSpan.snapshot : OtelBridge.snapshot(false);
                    if (!captured.suppressed() && runtime.acceptingResults()) {
                        // Under the nearest enclosing span the agent records: a skipped call's own span is never exported.
                        AdkSpan recorded = parentSpan;
                        while (recorded != null && recorded.skipped) recorded = recorded.parent;
                        if (recorded != null && recorded.shadow != null) recorded.shadowUsed = true;
                        Context parentContext = recorded != null && recorded.shadow != null ? Context.root().with(recorded.shadow) : captured.parent();
                        span = runtime.tracer().spanBuilder(bounded(name)).setSpanKind(SpanKind.INTERNAL).setParent(parentContext)
                            .setStartTimestamp(startEpochNanos, TimeUnit.NANOSECONDS).startSpan();
                    }
                }
            } catch (Throwable ignored) { span = null; }
            this.snapshot = captured; this.shadow = span; this.state = runtime;
            // Only a call the agent records is registered, before ADK assembles the request, so a client that sends at assembly
            // time or on a thread of its own executor is covered; removed when ADK ends the span, or if no proxy is returned.
            if (span != null && "LLM".equals(kind)) registerInFlight(flight, parentSpan);
        }

        void deregister() { IN_FLIGHT_LLM.remove(flight); }

        private static String bounded(String value) { return value == null || value.length() <= 200 ? value : value.substring(0, 200); }

        /** The span kind for an ADK span name, across the names ADK versions have used. */
        static String kindOf(String name) {
            if (name == null) return null;
            if (name.equals("call_llm")) return "LLM";
            if (name.equals("invocation")) return "CHAIN";
            if (name.startsWith("invoke_agent") || name.startsWith("agent_run")) return "AGENT";
            if (name.startsWith("execute_tool") || name.startsWith("tool_call")) return "TOOL";
            return null;
        }

        @Override public Object invoke(Object proxy, Method method, Object[] args) throws Throwable {
            if (method.getDeclaringClass() == Object.class) {
                return switch (method.getName()) {
                    case "equals" -> proxy == args[0];
                    case "hashCode" -> System.identityHashCode(proxy);
                    default -> method.invoke(delegate, args);
                };
            }
            String methodName = method.getName();
            switch (methodName) {
                case "end" -> { long end = args == null || args.length == 0 ? now() : epoch(args); try { return call(method, delegate, args); } finally { finish(end); } }
                case "updateName", "getSpanContext", "isRecording" -> { return call(method, delegate, args); }
                default -> { }
            }
            if (method.isDefault()) return defaultMethod(proxy, method, args);
            try {
                switch (methodName) {
                    case "setAttribute" -> { if (args.length == 2) synchronized (attributes) { record(attributes, args[0], args[1]); } }
                    case "setStatus" -> { if (args.length >= 1 && args[0] != null && String.valueOf(args[0]).equals("ERROR")) { failed = true; if (args.length == 2) errorText = String.valueOf(args[1]); } }
                    case "recordException" -> { if (args.length >= 1 && args[0] instanceof Throwable t) { failed = true; thrown = t; } }
                    default -> { }
                }
            } catch (Throwable ignored) { }
            Object result = call(method, delegate, args);
            return result != null && method.getReturnType() == api.span() ? proxy : result;
        }

        private static long now() {
            Instant instant = Instant.now();
            return instant.getEpochSecond() * 1_000_000_000L + instant.getNano();
        }

        // ---- the content ADK reports ------------------------------------------------------------------------

        void llm(Map<String, Object> request, Map<String, Object> response, String session, Throwable error) {
            // The model has answered: its HTTP call is done, so nothing after this is that call's.
            answered = true; IN_FLIGHT_LLM.remove(flight);
            this.request = request; this.response = response;
            if (session != null) sessionId = session;
            if (error != null) { failed = true; thrown = error; }
        }
        void tool(String name, String description, String type, Object arguments, Object result, String callId, Throwable error) {
            toolName = name; toolDescription = description; toolType = type; toolArguments = arguments; toolResult = result; toolCallId = callId;
            if (error != null) { failed = true; thrown = error; }
        }
        void skip() { skipped = true; }
        void agent(String name, String description, String session, String user) {
            agentName = name; agentDescription = description;
            if (session != null) sessionId = session;
            if (user != null) userId = user;
        }

        // ---- the agent's own span ---------------------------------------------------------------------------

        private void finish(long endEpochNanos) {
            if (!ended.compareAndSet(false, true)) return;
            IN_FLIGHT_LLM.remove(flight);
            try {
                Map<String, Object> mapped = withheld(mapped());
                if (parent != null) parent.child(this, mapped);
                if (skipped && !shadowUsed) {
                    // No span of the agent's refers to it: it is closed and never exported.
                    if (shadow != null) WireResourceProcessor.drop(shadow, endEpochNanos);
                    return;
                }
                // A skipped call that already has children under its own span is recorded after all: no exported span refers to an unexported one.
                if (shadow == null || state == null || !state.startFinalizing()) return;
                try { emit(mapped, endEpochNanos); } finally { state.endFinalizing(); }
            } catch (Throwable ignored) { }
        }

        /**
         * A failed call's output can carry the error's own text. Under hide-error-details it is withheld from the span, from every output
         * message, and from what this span tells its ancestors (their input and output text).
         */
        private Map<String, Object> withheld(Map<String, Object> attrs) {
            boolean fails = failed || failure() != null || "TOOL".equals(kind) && AdkAttributes.toolResultFailed(toolResult);
            if (!fails || state == null || !TraceAIAuto.hidesErrorText(state.config())) return attrs;
            Map<String, Object> out = AdkAttributes.withoutFailedOutput(attrs);
            outputText = null;
            return out;
        }

        /** What a finished child tells its ancestors: the session, the first prompt and the last answer. */
        private void child(AdkSpan child, Map<String, Object> mapped) {
            if (sessionId == null && child.sessionId != null) sessionId = child.sessionId;
            if (userId == null && child.userId != null) userId = child.userId;
            String prompt = null, answer = null;
            if (child.kind.equals("LLM")) {
                prompt = mapped.get("input.value") instanceof String s ? s : null;
                answer = mapped.get("output.value") instanceof String s && !"application/json".equals(mapped.get("output.mime_type")) ? s : null;
            } else if (child.kind.equals("AGENT") || child.kind.equals("CHAIN")) {
                prompt = child.inputText; answer = child.outputText;
            }
            if (inputText == null && prompt != null) inputText = prompt;
            if (answer != null) outputText = answer;
        }

        private Map<String, Object> mapped() {
            Map<String, Object> out;
            switch (kind) {
                case "LLM" -> out = AdkAttributes.llm(request, response);
                case "TOOL" -> out = AdkAttributes.tool(toolName != null ? toolName : toolNameFromSpan(), toolDescription, toolType, toolArguments, toolResult, toolCallId);
                case "AGENT" -> out = AdkAttributes.agent(agentName != null ? agentName : agentNameFromSpan(), agentDescription);
                default -> out = AdkAttributes.invocation();
            }
            out = new LinkedHashMap<>(out);
            if (kind.equals("AGENT") || kind.equals("CHAIN")) {
                if (inputText != null) { out.put("input.value", inputText); out.put("input.mime_type", "text/plain"); }
                if (outputText != null) { out.put("output.value", outputText); out.put("output.mime_type", "text/plain"); }
            }
            out.putAll(AdkAttributes.identity(sessionId, userId));
            out.put("traceai.capture.layer", INTEGRATION);
            if (snapshot != null) snapshot.attributes().forEach(out::putIfAbsent);
            return out;
        }

        private String toolNameFromSpan() {
            int open = name.indexOf('['), space = name.indexOf(' ');
            if (open >= 0 && name.endsWith("]")) return name.substring(open + 1, name.length() - 1);
            return space >= 0 ? name.substring(space + 1) : name;
        }

        private String agentNameFromSpan() {
            int open = name.indexOf('['), space = name.indexOf(' ');
            if (open >= 0 && name.endsWith("]")) return name.substring(open + 1, name.length() - 1);
            return space >= 0 ? name.substring(space + 1) : name;
        }

        private void emit(Map<String, Object> attrs, long endEpochNanos) {
            AutoConfig config = state.config();
            var resource = snapshot.resource() == null ? state.resource : TraceAIAuto.withRouting(snapshot.resource(), state.resource);
            int limit = Math.min(state.provider().getSpanLimits().getMaxNumberOfAttributes(), snapshot.attributeLimit() == null ? Integer.MAX_VALUE : snapshot.attributeLimit());
            String failure = failure();
            boolean fails = failed || failure != null;
            if ((fails || "TOOL".equals(kind) && AdkAttributes.toolResultFailed(toolResult)) && TraceAIAuto.hidesErrorText(config)) {
                // A failed call's output can carry the error's own text; withheld as on the wire path.
                attrs = AdkAttributes.withoutFailedOutput(attrs);
            }
            try {
                int samplingSlots = shadow instanceof io.opentelemetry.sdk.trace.ReadableSpan readable
                    && readable.getAttribute(io.opentelemetry.api.common.AttributeKey.doubleKey("fi.sampling.ratio")) != null ? 1 : 0;
                Map<String, Object> clean = AttributeProjection.select(AttributeProjection.sanitize(attrs, config, null), Math.max(0, limit - samplingSlots), "chat");
                for (Map.Entry<String, Object> e : clean.entrySet()) {
                    String k = e.getKey(); Object v = e.getValue();
                    if (TraceAIAuto.hidden(k, v, null, config)) continue;
                    if (v instanceof String s) shadow.setAttribute(k, s);
                    else if (v instanceof Boolean b) shadow.setAttribute(k, b);
                    else if (v instanceof Float || v instanceof Double) shadow.setAttribute(k, ((Number) v).doubleValue());
                    else if (v instanceof Number n) shadow.setAttribute(k, n.longValue());
                    else if (v instanceof List<?> list) shadow.setAttribute(io.opentelemetry.api.common.AttributeKey.stringArrayKey(k),
                        list.stream().map(item -> item instanceof String s ? s : item == null ? "null" : item.getClass().getName()).toList());
                }
                if (fails) {
                    boolean hide = TraceAIAuto.hidesErrorText(config);
                    String text = failure != null ? failure : errorText;
                    shadow.setStatus(StatusCode.ERROR, hide || text == null ? "" : text);
                    if (thrown != null) shadow.setAttribute("error.type", thrown.getClass().getName());
                } else shadow.setStatus(StatusCode.OK);
            } finally { WireResourceProcessor.end(shadow, endEpochNanos, resource, limit); }
        }

        private String failure() {
            Map<String, Object> r = response;
            return AdkAttributes.llmError(r);
        }
    }

    // ---- Content reported by ADK ------------------------------------------------------------------------------

    /** Called reflectively from the Tracing advice, before ADK writes the content to its own span. */
    public static void trace(String method, Object[] args) {
        try {
            if (args == null || args.length == 0 || args[0] == null || !Proxy.isProxyClass(args[0].getClass())) return;
            if (!(Proxy.getInvocationHandler(args[0]) instanceof AdkSpan span)) return;
            switch (method) {
                case "traceCallLlm" -> {
                    // (span, invocationContext, eventId, llmRequest, llmResponse[, error])
                    Throwable error = args.length > 5 && args[5] instanceof Throwable t ? t : null;
                    ClassLoader loader = args[3].getClass().getClassLoader();
                    span.llm(adkJson(requestShape(args[3]), loader), adkJson(args[4], loader), sessionOf(args[1]), error);
                    if (skipWrappedCalls() && modelIsWrapper(args[1])) span.skip();
                    String user = userOf(args[1]);
                    if (user != null) span.agent(null, null, null, user);
                }
                case "traceToolExecution" -> {
                    // (span, toolName, toolDescription, toolType, args, functionResponseEvent, error)
                    Throwable error = args.length > 6 && args[6] instanceof Throwable t ? t : null;
                    Object result = null; String callId = null;
                    if (args.length > 5 && args[5] != null) {
                        Map<String, Object> event = adkJson(args[5], args[5].getClass().getClassLoader());
                        Object content = event.get("content");
                        Object parts = content instanceof Map<?, ?> c ? c.get("parts") : null;
                        if (parts instanceof List<?> list) for (Object part : list) {
                            if (part instanceof Map<?, ?> p && p.get("functionResponse") instanceof Map<?, ?> response) {
                                result = response.get("response"); callId = response.get("id") == null ? null : String.valueOf(response.get("id")); break;
                            }
                        }
                    }
                    span.tool(String.valueOf(args[1]), args[2] == null ? null : String.valueOf(args[2]), args[3] == null ? null : String.valueOf(args[3]), args[4], result, callId, error);
                    if (skipWrappedCalls() && takeWrappedTool(args[4], args[3] == null ? null : String.valueOf(args[3]))) span.skip();
                }
                case "traceAgentInvocation" -> span.agent(args[1] == null ? null : String.valueOf(args[1]), args[2] == null ? null : String.valueOf(args[2]),
                    args.length > 3 ? sessionOf(args[3]) : null, args.length > 3 ? userOf(args[3]) : null);
                default -> { }
            }
        } catch (Throwable error) { TraceAIAuto.debug("google-adk content not read (" + error.getClass().getSimpleName() + ")"); }
    }

    /** ADK's own trace shape of a request: the model, the generation config and the contents. */
    private static Object requestShape(Object llmRequest) throws ReflectiveOperationException {
        Map<String, Object> shape = new LinkedHashMap<>();
        shape.put("model", optional(invoke(llmRequest, "model")));
        Object config = optional(invoke(llmRequest, "config"));
        if (config != null) shape.put("config", config);
        shape.put("contents", invoke(llmRequest, "contents"));
        return shape;
    }

    /**
     * Calls a public no-argument method through the first public class that declares it: ADK's value classes are
     * often generated, non-public subclasses, whose own method objects cannot be invoked.
     */
    private static Object invoke(Object target, String name) throws ReflectiveOperationException {
        for (Class<?> type = target.getClass(); type != null; type = type.getSuperclass()) {
            if (!java.lang.reflect.Modifier.isPublic(type.getModifiers())) continue;
            try { return type.getMethod(name).invoke(target); } catch (NoSuchMethodException ignored) { }
        }
        throw new NoSuchMethodException(name);
    }

    private static Object optional(Object value) throws ReflectiveOperationException {
        return value instanceof Optional<?> o ? o.orElse(null) : value;
    }

    @SuppressWarnings("unchecked")
    private static Map<String, Object> adkJson(Object value, ClassLoader loader) {
        try {
            Class<?> base = Class.forName("com.google.adk.JsonBaseModel", false, loader);
            Object mapper = base.getMethod("getMapper").invoke(null);
            String json = (String) mapper.getClass().getMethod("writeValueAsString", Object.class).invoke(mapper, value);
            Object parsed = AdkAttributes.parseJson(json);
            return parsed instanceof Map<?, ?> m ? (Map<String, Object>) m : Map.of();
        } catch (Throwable ignored) { return Map.of(); }
    }

    private static String sessionOf(Object invocationContext) {
        try {
            Object session = invoke(invocationContext, "session");
            Object id = session == null ? null : invoke(session, "id");
            return id == null ? null : String.valueOf(id);
        } catch (Throwable ignored) { return null; }
    }

    private static String userOf(Object invocationContext) {
        try {
            Object id = invoke(invocationContext, "userId");
            return id == null ? null : String.valueOf(id);
        } catch (Throwable ignored) { return null; }
    }

    // ---- Calls made through the traceai wrappers --------------------------------------------------------------

    static final String SKIP_OPTION = "FI_GOOGLE_ADK_SKIP_WRAPPED_CALLS";
    /** Built from parts: the build relocates this jar's own copy of the module's mapping classes, and a whole name here would be rewritten with it. */
    private static final String WRAPPERS = String.join(".", "ai", "traceai", "googleadk");
    private static final String TRACED_LLM = WRAPPERS + ".TracedLlm", TRACED_TOOL = WRAPPERS + ".TracedTool";

    /** True when the option is on: the agent records no span for a model or tool call made through TracedLlm or TracedTool. */
    private static boolean skipWrappedCalls() {
        AutoConfig config = TraceAIAuto.config();
        return config != null && config.flag(SKIP_OPTION);
    }

    /** True when the object's class, or one of its superclasses, is the named class. */
    private static boolean isInstanceOfClassNamed(Object object, String name) {
        for (Class<?> type = object == null ? null : object.getClass(); type != null; type = type.getSuperclass()) if (type.getName().equals(name)) return true;
        return false;
    }

    /**
     * True when the model of the agent that ADK is calling is a TracedLlm: ADK's model call goes through the agent's resolved model.
     * Any API difference, or a model that cannot be read, is false, so the call is recorded.
     */
    private static boolean modelIsWrapper(Object invocationContext) {
        try {
            Object resolved = invoke(invoke(invocationContext, "agent"), "resolvedModel");
            Object llm = optional(invoke(resolved, "model"));
            return isInstanceOfClassNamed(llm, TRACED_LLM);
        } catch (Throwable error) { TraceAIAuto.debug("google-adk model not read (" + error.getClass().getSimpleName() + ")"); return false; }
    }

    /**
     * ADK reports a tool call with the tool's name, description and its class's simple name, not the tool. TracedTool tells the agent about each
     * call it receives (the arguments map ADK passes it, and itself) before it runs the tool; the report that carries the same arguments map
     * and the simple name of that wrapper's class is that call's.
     */
    private record WrappedTool(java.lang.ref.WeakReference<Object> arguments, String type, long registered) {}
    private static final List<WrappedTool> WRAPPED_TOOLS = new ArrayList<>();

    /** Called reflectively by TracedTool. Registers nothing unless the agent traces ADK and the option is on. */
    public static void wrappedTool(Object arguments, Object tool) {
        try {
            if (arguments == null || !isInstanceOfClassNamed(tool, TRACED_TOOL) || !skipWrappedCalls() || TraceAIAuto.besideOtelJavaAgent() || !TraceAIAuto.integrationEnabled(INTEGRATION)) return;
            long now = System.nanoTime();
            synchronized (WRAPPED_TOOLS) {
                WRAPPED_TOOLS.removeIf(w -> w.arguments().get() == null || now - w.registered() > STALE_NANOS);
                boolean known = false;
                for (WrappedTool w : WRAPPED_TOOLS) if (w.arguments().get() == arguments) { known = true; break; }
                if (!known && WRAPPED_TOOLS.size() < MAX_IN_FLIGHT) WRAPPED_TOOLS.add(new WrappedTool(new java.lang.ref.WeakReference<>(arguments), tool.getClass().getSimpleName(), now));
            }
            skipCurrentTool(tool);
        } catch (Throwable ignored) { }
    }

    /**
     * ADK reports a tool call after the tool has run, but spans started while it ran are already under the call's own span: when
     * the application's current span is the ADK tool span, mark it skipped now, before the tool runs.
     */
    private static void skipCurrentTool(Object tool) {
        try {
            ClassLoader loader = tool.getClass().getClassLoader() == null ? ClassLoader.getSystemClassLoader() : tool.getClass().getClassLoader();
            Api api = APIS.computeIfAbsent(loader, key -> { try { return Api.of(key); } catch (ReflectiveOperationException e) { return null; } });
            if (api == null) return;
            Object current = api.fromContext().invoke(null, api.current().invoke(null));
            if (current != null && Proxy.isProxyClass(current.getClass()) && Proxy.getInvocationHandler(current) instanceof AdkSpan adk && "TOOL".equals(adk.kind)) adk.skip();
        } catch (Throwable ignored) { }
    }

    private static boolean takeWrappedTool(Object arguments, String type) {
        if (arguments == null || type == null) return false;
        synchronized (WRAPPED_TOOLS) {
            for (java.util.Iterator<WrappedTool> it = WRAPPED_TOOLS.iterator(); it.hasNext(); ) {
                WrappedTool w = it.next();
                if (w.arguments().get() == arguments && w.type().equals(type)) { it.remove(); return true; }
            }
        }
        return false;
    }

    // ---- Read by the wire capture -----------------------------------------------------------------------------

    /**
     * ADK model calls the agent records, started and not yet ended: an identity and a start time each, never request or response
     * content. Bounded, and pruned whenever a span starts and whenever the wire capture asks.
     */
    private record Flight(long started, Object[] ancestors) {}
    private static final Map<Object, Flight> IN_FLIGHT_LLM = new ConcurrentHashMap<>();
    private static final int MAX_IN_FLIGHT = 256;
    /** A model call ADK never ended must not silence the wire capture for good. */
    private static final long STALE_NANOS = TimeUnit.MINUTES.toNanos(15);

    private static void prune() {
        long now = System.nanoTime();
        IN_FLIGHT_LLM.values().removeIf(flight -> now - flight.started() > STALE_NANOS);
    }

    /** Past the cap a call is not registered: the dedup fails open, so the request is traced rather than lost. */
    private static void registerInFlight(Object flight, AdkSpan parent) {
        prune();
        if (IN_FLIGHT_LLM.size() >= MAX_IN_FLIGHT) return;
        // The identities of the enclosing ADK spans (agent, invocation), nothing else.
        List<Object> ancestors = new ArrayList<>();
        for (AdkSpan up = parent; up != null && ancestors.size() < 16; up = up.parent) ancestors.add(up.flight);
        IN_FLIGHT_LLM.put(flight, new Flight(System.nanoTime(), ancestors.toArray()));
    }

    /**
     * True while a model call is waiting for its answer inside the ADK span the application sees as current: a request made under
     * the agent or invocation span that encloses it, while ADK assembles that call's request, belongs to it. So does one made
     * under an earlier span of the same agent that has already ended or answered, which older ADK versions leave current. A request
     * under any other ADK span (a tool's, another session's) never does.
     */
    static boolean modelCallInFlightNear(Object external) {
        try {
            if (external == null || IN_FLIGHT_LLM.isEmpty() || !Proxy.isProxyClass(external.getClass())
                || !(Proxy.getInvocationHandler(external) instanceof AdkSpan adk)) return false;
            prune();
            // A span that has ended or answered is only left current by an older ADK; what encloses it is what the thread is in.
            boolean spent = adk.ended.get() || "LLM".equals(adk.kind) && adk.answered;
            for (Flight flight : IN_FLIGHT_LLM.values())
                for (Object ancestor : flight.ancestors()) {
                    if (ancestor == adk.flight) return true;
                    if (spent) for (AdkSpan up = adk.parent; up != null; up = up.parent) if (ancestor == up.flight) return true;
                }
        } catch (Throwable ignored) { }
        return false;
    }

    /** False once an ADK model-call span has its answer: a request made after that is not the model call's own. */
    static boolean awaitingAnswer(Object external) {
        try {
            if (external != null && Proxy.isProxyClass(external.getClass()) && Proxy.getInvocationHandler(external) instanceof AdkSpan adk) return !adk.answered;
        } catch (Throwable ignored) { }
        return true;
    }

    /** The span kind of an ADK span the application sees as its current span, or null for any other span. */
    static String kindOf(Object external) {
        try {
            if (external != null && Proxy.isProxyClass(external.getClass()) && Proxy.getInvocationHandler(external) instanceof AdkSpan adk) return adk.kind;
        } catch (Throwable ignored) { }
        return null;
    }

    /** The id of the agent's own span for an ADK span the application sees as its current span, or "" for any other span. */
    static String idOf(Object external) {
        try {
            if (external != null && Proxy.isProxyClass(external.getClass()) && Proxy.getInvocationHandler(external) instanceof AdkSpan adk && adk.shadow != null) return adk.shadow.getSpanContext().getSpanId();
        } catch (Throwable ignored) { }
        return "";
    }

    /** The agent's own span for an ADK span the application sees as its current span, as a context, or null. */
    static Context contextOf(Object external) {
        try {
            if (external != null && Proxy.isProxyClass(external.getClass()) && Proxy.getInvocationHandler(external) instanceof AdkSpan adk) {
                // A skipped call has no span of its own: the nearest enclosing span the agent records is the context.
                AdkSpan recorded = adk;
                while (recorded != null && recorded.skipped) recorded = recorded.parent;
                if (recorded != null && recorded.shadow != null) { recorded.shadowUsed = true; return Context.root().with(recorded.shadow); }
            }
        } catch (Throwable ignored) { }
        return null;
    }
}
