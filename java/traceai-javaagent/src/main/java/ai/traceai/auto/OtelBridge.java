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
    /** external: the application's current span (trace id/span id) when it is readable, else null. */
    record Snapshot(Context parent, Map<String, String> attributes, boolean suppressed, long startEpochNanos, long startNanos, Integer attributeLimit, io.opentelemetry.sdk.resources.Resource resource, String external) {
        Snapshot(Context parent, Map<String, String> attributes, boolean suppressed, long startEpochNanos, long startNanos) {
            this(parent, attributes, suppressed, startEpochNanos, startNanos, null, null);
        }
        Snapshot(Context parent, Map<String, String> attributes, boolean suppressed, long startEpochNanos, long startNanos, Integer attributeLimit, io.opentelemetry.sdk.resources.Resource resource) {
            this(parent, attributes, suppressed, startEpochNanos, startNanos, attributeLimit, resource, null);
        }
    }
    private static final Set<String> LIBRARY = Set.of("LLM", "EMBEDDING", "RERANKER", "GUARDRAIL");
    /** A tool span is not created again under an active TOOL span, such as one from a traceai-java wrapper. */
    private static final Set<String> TOOL = Set.of("TOOL");
    static Snapshot snapshot() { return snapshot(LIBRARY); }
    static Snapshot toolSnapshot() { return snapshot(TOOL); }
    private static Snapshot snapshot(Set<String> kinds) {
        java.time.Instant instant = java.time.Instant.now(); long startNanos = System.nanoTime();
        Context parent = Context.current();
        Span current = Span.current();
        boolean suppressed = Boolean.TRUE.equals(parent.get(TraceAIAuto.SUPPRESS));
        if (current instanceof ReadableSpan readable) suppressed |= kind(kinds, readable.getAttribute(AttributeKey.stringKey("gen_ai.span.kind"))) || kind(kinds, readable.getAttribute(AttributeKey.stringKey("fi.span.kind")));
        Integer attributeLimit = null;
        io.opentelemetry.sdk.resources.Resource resource = null;
        String external = null;
        boolean applicationKind = false;
        Map<String, String> attrs = new LinkedHashMap<>(ContextAttributes.getAttributesFromContext());
        try {
            ClassLoader loader = Thread.currentThread().getContextClassLoader();
            // Build the prefix dynamically so Maven Shade does not rewrite the application API name.
            String prefix = String.join(".", "io", "opentelemetry");
            Class<?> spanApi = Class.forName(prefix + ".api.trace.Span", false, loader);
            Object applicationSpan = spanApi.getMethod("current").invoke(null);
            attributeLimit = ApplicationOtel.spanLimit(applicationSpan, loader);
            resource = ApplicationOtel.resource(applicationSpan, loader);
            try {
                Class<?> readable = Class.forName(prefix + ".sdk.trace.ReadableSpan", false, loader);
                if (readable.isInstance(applicationSpan)) {
                    Class<?> keyApi = Class.forName(prefix + ".api.common.AttributeKey", false, loader);
                    for (String name : List.of("gen_ai.span.kind", "fi.span.kind")) {
                        Object key = keyApi.getMethod("stringKey", String.class).invoke(null, name);
                        Object value = readable.getMethod("getAttribute", keyApi).invoke(applicationSpan, key);
                        applicationKind |= kind(kinds, value == null ? null : String.valueOf(value));
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
            Object sc = spanApi.getMethod("getSpanContext").invoke(applicationSpan);
            Class<?> scApi = Class.forName(prefix + ".api.trace.SpanContext", false, loader);
            if ((boolean)scApi.getMethod("isValid").invoke(sc)) {
                parent = Context.root().with(Span.wrap(ApplicationOtel.context(sc, loader)));
                external = scApi.getMethod("getTraceId").invoke(sc) + "/" + scApi.getMethod("getSpanId").invoke(sc);
            }
            try {
                Class<?> contextAttrs = Class.forName(String.join(".", "ai", "traceai", "ContextAttributes"), false, loader);
                Object data = contextAttrs.getMethod("getAttributesFromContext").invoke(null);
                if (data instanceof Map<?, ?> map) map.forEach((k, v) -> attrs.put(String.valueOf(k), String.valueOf(v)));
            } catch (ReflectiveOperationException ignored) { }
        } catch (Throwable ignored) { }
        // A Spring AI tool_call span that the agent itself marked TOOL is the observed tool invocation, not
        // a wrapper's tool span: tools that invocation calls directly still get their own spans.
        try {
            if (applicationKind && !(kinds == TOOL && ToolSpans.markedByAgent(external))) suppressed = true;
        } catch (Throwable ignored) { suppressed |= applicationKind; }
        // Work a tool does on this thread, while the application's current span is still the one the
        // tool started under, belongs to the agent's tool span.
        try {
            ToolSpans.Frame tool = ToolSpans.current();
            if (tool != null && Objects.equals(tool.external, external)) parent = Context.root().with(tool.span);
        } catch (Throwable ignored) { }
        return new Snapshot(parent, attrs, suppressed || TraceAIAuto.IN_FLIGHT.get(), instant.getEpochSecond() * 1_000_000_000L + instant.getNano(), startNanos, attributeLimit, resource, external);
    }
    static boolean library(String kind) { return kind(LIBRARY, kind); }
    private static boolean kind(Set<String> kinds, String kind) { return kind != null && kinds.contains(kind.toUpperCase(Locale.ROOT)); }
    private OtelBridge() {}
}
