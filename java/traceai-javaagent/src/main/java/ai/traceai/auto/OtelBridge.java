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
    static Snapshot snapshot() {
        java.time.Instant instant = java.time.Instant.now(); long startNanos = System.nanoTime();
        Context parent = Context.current();
        Span current = Span.current();
        boolean suppressed = Boolean.TRUE.equals(parent.get(TraceAIAuto.SUPPRESS));
        if (current instanceof ReadableSpan readable) suppressed |= library(readable.getAttribute(AttributeKey.stringKey("gen_ai.span.kind"))) || library(readable.getAttribute(AttributeKey.stringKey("fi.span.kind")));
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
            Object sc = spanApi.getMethod("getSpanContext").invoke(external);
            Class<?> scApi = Class.forName(prefix + ".api.trace.SpanContext", false, loader);
            if ((boolean)scApi.getMethod("isValid").invoke(sc)) parent = Context.root().with(Span.wrap(ApplicationOtel.context(sc, loader)));
            try {
                Class<?> contextAttrs = Class.forName(String.join(".", "ai", "traceai", "ContextAttributes"), false, loader);
                Object data = contextAttrs.getMethod("getAttributesFromContext").invoke(null);
                if (data instanceof Map<?, ?> map) map.forEach((k, v) -> attrs.put(String.valueOf(k), String.valueOf(v)));
            } catch (ReflectiveOperationException ignored) { }
        } catch (Throwable ignored) { }
        return new Snapshot(parent, attrs, suppressed || TraceAIAuto.IN_FLIGHT.get(), instant.getEpochSecond() * 1_000_000_000L + instant.getNano(), startNanos, attributeLimit, resource);
    }
    /**
     * True when the application's active span is a TOOL span for this tool (what a traceai-java-langchain4j or
     * traceai-java-spring-ai wrapper opens around the tool). The agent adds no second span under it. Only the
     * active span is read.
     */
    static boolean toolSpanActive(String tool) {
        try {
            Span current = Span.current();
            if (current instanceof ReadableSpan readable && sameTool(readable.getAttribute(AttributeKey.stringKey("fi.span.kind")),
                readable.getAttribute(AttributeKey.stringKey("gen_ai.operation.name")), readable.getAttribute(AttributeKey.stringKey("gen_ai.tool.name")), tool)) return true;
            ClassLoader loader = Thread.currentThread().getContextClassLoader();
            // Build the prefix dynamically so Maven Shade does not rewrite the application API name.
            String prefix = String.join(".", "io", "opentelemetry");
            Class<?> spanApi = Class.forName(prefix + ".api.trace.Span", false, loader);
            Object external = spanApi.getMethod("current").invoke(null);
            Class<?> readable = Class.forName(prefix + ".sdk.trace.ReadableSpan", false, loader);
            if (!readable.isInstance(external)) return false;
            Class<?> keyApi = Class.forName(prefix + ".api.common.AttributeKey", false, loader);
            String[] values = new String[3];
            String[] names = {"fi.span.kind", "gen_ai.operation.name", "gen_ai.tool.name"};
            for (int i = 0; i < 3; i++) {
                Object value = readable.getMethod("getAttribute", keyApi).invoke(external, keyApi.getMethod("stringKey", String.class).invoke(null, names[i]));
                values[i] = value == null ? null : String.valueOf(value);
            }
            return sameTool(values[0], values[1], values[2], tool);
        } catch (Throwable ignored) { return false; }
    }
    private static boolean sameTool(String kind, String operation, String name, String tool) {
        return "TOOL".equalsIgnoreCase(kind) && "execute_tool".equals(operation) && tool.equals(name);
    }
    static boolean library(String kind) { return kind != null && LIBRARY.contains(kind.toUpperCase(Locale.ROOT)); }
    private OtelBridge() {}
}
