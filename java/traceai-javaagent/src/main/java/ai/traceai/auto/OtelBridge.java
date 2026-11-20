package ai.traceai.auto;

import ai.traceai.ContextAttributes;
import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.api.trace.*;
import io.opentelemetry.context.Context;
import io.opentelemetry.sdk.trace.ReadableSpan;
import java.lang.reflect.Method;
import java.util.*;

/** Reads the application's active OTel context across the shaded agent boundary, read-only.
 * A call is not traced again when the active span is a library span (LLM, EMBEDDING, RERANKER or
 * GUARDRAIL); only the active span is read, never its ancestors. */
final class OtelBridge {
    record Snapshot(Context parent, Map<String, String> attributes, boolean suppressed, long startEpochNanos, long startNanos, Integer attributeLimit, io.opentelemetry.sdk.resources.Resource resource) {
        Snapshot(Context parent, Map<String, String> attributes, boolean suppressed, long startEpochNanos, long startNanos) {
            this(parent, attributes, suppressed, startEpochNanos, startNanos, null, null);
        }
    }
    private static final Set<String> LIBRARY = Set.of("LLM", "EMBEDDING", "RERANKER", "GUARDRAIL");
    static Snapshot snapshot() { return snapshot(true); }
    /**
     * @param wire true for a request about to be captured by the wire layer: under an ADK span, a request that belongs to an ADK model
     *             call in flight under that span's agent is not captured twice. A request from a thread with no span at all is always
     *             captured: the in-flight registry has no URL or model to tell whose request it is, so it never drops one.
     */
    static Snapshot snapshot(boolean wire) {
        boolean validSpan = false;
        Object adkSpan = null;
        java.time.Instant instant = java.time.Instant.now(); long startNanos = System.nanoTime();
        Context parent = Context.current();
        Span current = Span.current();
        validSpan = current.getSpanContext().isValid();
        // Inside an annotated application method its span is the parent, and is not a library span to suppress under.
        boolean annotated = parent.get(AnnotationTracing.ACTIVE) != null;
        boolean suppressed = Boolean.TRUE.equals(parent.get(TraceAIAuto.SUPPRESS));
        if (!annotated && current instanceof ReadableSpan readable) suppressed |= library(readable.getAttribute(AttributeKey.stringKey("gen_ai.span.kind"))) || library(readable.getAttribute(AttributeKey.stringKey("fi.span.kind")));
        Integer attributeLimit = null;
        io.opentelemetry.sdk.resources.Resource resource = null;
        Map<String, String> attrs = new LinkedHashMap<>(ContextAttributes.getAttributesFromContext());
        try {
            ClassLoader loader = Thread.currentThread().getContextClassLoader();
            // Build the prefix dynamically so Maven Shade does not rewrite the application API name.
            String prefix = String.join(".", "io", "opentelemetry");
            Class<?> spanApi = Class.forName(prefix + ".api.trace.Span", false, loader);
            Object external = spanApi.getMethod("current").invoke(null);
            attributeLimit = ApplicationOtel.spanLimit(external, loader);
            resource = ApplicationOtel.resource(external, loader);
            try {
                Class<?> readable = Class.forName(prefix + ".sdk.trace.ReadableSpan", false, loader);
                if (readable.isInstance(external)) {
                    Class<?> keyApi = Class.forName(prefix + ".api.common.AttributeKey", false, loader);
                    for (String name : List.of("gen_ai.span.kind", "fi.span.kind")) {
                        Object key = keyApi.getMethod("stringKey", String.class).invoke(null, name);
                        Object value = readable.getMethod("getAttribute", keyApi).invoke(external, key);
                        suppressed |= library(value == null ? null : String.valueOf(value));
                    }
                }
            } catch (ReflectiveOperationException ignored) { }
            Class<?> contextApi = Class.forName(prefix + ".context.Context", false, loader);
            Object externalContext = contextApi.getMethod("current").invoke(null);
            Class<?> keyApi = Class.forName(prefix + ".context.ContextKey", false, loader);
            for (String holder : List.of(".instrumentation.api.internal.InstrumentationContextKeys", ".instrumentation.api.internal.ContextKeys")) {
                try {
                    Class<?> keys = Class.forName(prefix + holder, false, loader);
                    Object key = keys.getField("SUPPRESS_INSTRUMENTATION_KEY").get(null);
                    suppressed |= Boolean.TRUE.equals(contextApi.getMethod("get", keyApi).invoke(externalContext, key));
                } catch (ReflectiveOperationException ignored) { }
            }
            // A span Google ADK started: its model call is a library span, and the agent's own span for it is the parent.
            String adkKind = GoogleAdk.kindOf(external);
            if (adkKind != null) {
                // The model call's own span only counts until the model has answered: a tool that runs under it is not that call.
                suppressed |= library(adkKind) && GoogleAdk.awaitingAnswer(external);
                validSpan = true;
                adkSpan = external;
            }
            Object sc = spanApi.getMethod("getSpanContext").invoke(external);
            Class<?> scApi = Class.forName(prefix + ".api.trace.SpanContext", false, loader);
            boolean validContext = (boolean)scApi.getMethod("isValid").invoke(sc);
            validSpan |= validContext;
            // The annotated span stays the parent inside an annotated method; the application's span context does not replace it.
            if (validContext && !annotated) parent = Context.root().with(Span.wrap(ApplicationOtel.context(sc, loader)));
            // A Google ADK span is current: it is the parent even inside an annotated method (the agent span nests under the
            // annotated span, and the model and tool spans nest under the agent span).
            Context adkParent = GoogleAdk.contextOf(external);
            if (adkParent != null) parent = adkParent;
            try {
                Class<?> contextAttrs = Class.forName(String.join(".", "ai", "traceai", "ContextAttributes"), false, loader);
                Object data = contextAttrs.getMethod("getAttributesFromContext").invoke(null);
                if (data instanceof Map<?, ?> map) map.forEach((k, v) -> attrs.put(String.valueOf(k), String.valueOf(v)));
            } catch (ReflectiveOperationException ignored) { }
        } catch (Throwable ignored) { }
        if (wire && !suppressed) {
            // An ADK span is current: only a model call that is its child can own this request. No span at all: fail open, traced.
            if (adkSpan != null) suppressed = GoogleAdk.modelCallInFlightNear(adkSpan);
        }
        return new Snapshot(parent, attrs, suppressed || TraceAIAuto.IN_FLIGHT.get(), instant.getEpochSecond() * 1_000_000_000L + instant.getNano(), startNanos, attributeLimit, resource);
    }
    /**
     * True when the application already records this tool call: its active span is a TOOL span for this tool (what a
     * traceai-java-langchain4j or traceai-java-spring-ai wrapper opens around the tool), or Spring AI's own tool_call
     * observation is open (the native observation path). The agent adds no second span under either.
     *
     * <p>The native path is recognised by the open Micrometer observation: while a tool runs, Spring AI's
     * {@code ToolCallingObservationContext} is the current observation's context. The tracing handler starts the span unnamed and
     * applies its name ({@code framework <tool name>}), the {@code spring.ai.kind=tool_call} and {@code gen_ai.operation.name=framework}
     * tags and the TOOL kind only when the observation stops, so the live span cannot tell; the span name and tag are still checked
     * as an extra signal.
     */
    static boolean toolSpanActive(String tool) {
        return spanRecordsTool(tool) || nativeToolObservationOpen(tool);
    }
    private static final String TOOL_CONTEXT = "org.springframework.ai.tool.observation.ToolCallingObservationContext";
    private static final int OBSERVATION_DEPTH = 16;
    /**
     * The current Micrometer observation, or one of its parents, carries a Spring AI tool-calling context for this tool. Read
     * reflectively, by class name, from the application's class loader: nothing here needs Micrometer or Spring AI, and any
     * absence or failure means "not seen".
     */
    private static boolean nativeToolObservationOpen(String tool) {
        try {
            ClassLoader loader = Thread.currentThread().getContextClassLoader();
            Class<?> accessor = Class.forName("io.micrometer.observation.contextpropagation.ObservationThreadLocalAccessor", false, loader);
            Object value = accessor.getMethod("getValue").invoke(accessor.getMethod("getInstance").invoke(null));
            Class<?> observationType = Class.forName("io.micrometer.observation.Observation", false, loader);
            Object observation = observationType.isInstance(value) ? value : null;
            for (int depth = 0; observation != null && depth < OBSERVATION_DEPTH; depth++) {
                Object context = observationType.getMethod("getContextView").invoke(observation);
                if (context != null && isToolContext(context)) {
                    Object definition = ToolSpans.invoke(context, "getToolDefinition");
                    Object name = definition == null ? null : ToolSpans.invoke(definition, "name");
                    // A different tool's observation does not record this call; an unreadable name is treated as this one's.
                    if ((name == null || tool.equals(String.valueOf(name))) && hasTracingSpan(context)) return true;
                }
                Object parent = context == null ? null : ToolSpans.invoke(context, "getParentObservation");
                observation = observationType.isInstance(parent) ? parent : null;
            }
            return false;
        } catch (Throwable ignored) { return false; }
    }
    private static final String TRACING_CONTEXT = "io.micrometer.tracing.handler.TracingObservationHandler$TracingContext";
    /**
     * A tracing handler (Micrometer Tracing's) attached a span to this observation: its {@code TracingContext} is in the observation
     * context with a non-null span. A registry with only meter handlers (the common Spring Boot actuator setup) opens the observation
     * but never makes a span, so the call is not traced and the agent must record it. Any absence or failure means "no span".
     */
    private static boolean hasTracingSpan(Object context) {
        try {
            Class<?> key = Class.forName(TRACING_CONTEXT, false, context.getClass().getClassLoader());
            Class<?> view = Class.forName("io.micrometer.observation.Observation$ContextView", false, context.getClass().getClassLoader());
            Object tracing = view.getMethod("get", Object.class).invoke(context, key);
            return key.isInstance(tracing) && ToolSpans.invoke(tracing, "getSpan") != null;
        } catch (Throwable ignored) { return false; }
    }
    private static boolean isToolContext(Object context) {
        Deque<Class<?>> pending = new ArrayDeque<>(List.of(context.getClass()));
        Set<Class<?>> seen = new HashSet<>();
        while (!pending.isEmpty()) {
            Class<?> type = pending.pop();
            if (type == null || !seen.add(type)) continue;
            if (TOOL_CONTEXT.equals(type.getName())) return true;
            pending.add(type.getSuperclass()); pending.addAll(Arrays.asList(type.getInterfaces()));
        }
        return false;
    }
    private static boolean spanRecordsTool(String tool) {
        try {
            Span current = Span.current();
            if (current instanceof ReadableSpan readable) return active(tool, readable.getName(), new String[] {
                readable.getAttribute(AttributeKey.stringKey("fi.span.kind")), readable.getAttribute(AttributeKey.stringKey("gen_ai.operation.name")),
                readable.getAttribute(AttributeKey.stringKey("gen_ai.tool.name")), readable.getAttribute(AttributeKey.stringKey("spring.ai.kind"))});
            ClassLoader loader = Thread.currentThread().getContextClassLoader();
            // Build the prefix dynamically so Maven Shade does not rewrite the application API name.
            String prefix = String.join(".", "io", "opentelemetry");
            Class<?> spanApi = Class.forName(prefix + ".api.trace.Span", false, loader);
            Object external = spanApi.getMethod("current").invoke(null);
            Class<?> readable = Class.forName(prefix + ".sdk.trace.ReadableSpan", false, loader);
            if (!readable.isInstance(external)) return false;
            Class<?> keyApi = Class.forName(prefix + ".api.common.AttributeKey", false, loader);
            String[] names = {"fi.span.kind", "gen_ai.operation.name", "gen_ai.tool.name", "spring.ai.kind"}, values = new String[names.length];
            for (int i = 0; i < names.length; i++) {
                Object value = readable.getMethod("getAttribute", keyApi).invoke(external, keyApi.getMethod("stringKey", String.class).invoke(null, names[i]));
                values[i] = value == null ? null : String.valueOf(value);
            }
            return active(tool, String.valueOf(readable.getMethod("getName").invoke(external)), values);
        } catch (Throwable ignored) { return false; }
    }
    /** values: fi.span.kind, gen_ai.operation.name, gen_ai.tool.name, spring.ai.kind. */
    private static boolean active(String tool, String spanName, String[] values) {
        if (sameTool(values[0], values[1], values[2], tool)) return true;
        return "tool_call".equals(values[3]) || spanName != null && (spanName.equals("framework " + tool) || spanName.equals("tool_call " + tool) || spanName.equals("spring.ai.tool"));
    }
    private static boolean sameTool(String kind, String operation, String name, String tool) {
        return "TOOL".equalsIgnoreCase(kind) && "execute_tool".equals(operation) && tool.equals(name);
    }
    static boolean library(String kind) { return kind != null && LIBRARY.contains(kind.toUpperCase(Locale.ROOT)); }
    private OtelBridge() {}
}
