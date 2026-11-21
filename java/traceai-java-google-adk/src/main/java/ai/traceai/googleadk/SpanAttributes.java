package ai.traceai.googleadk;

import ai.traceai.FITracer;
import io.opentelemetry.api.trace.Span;

import java.util.List;
import java.util.Map;

/** Writes mapped attributes through the tracer, so its privacy settings apply to every value. */
final class SpanAttributes {
    private SpanAttributes() {}

    @SuppressWarnings("unchecked")
    static void write(FITracer tracer, Span span, Map<String, Object> attributes) {
        for (Map.Entry<String, Object> entry : attributes.entrySet()) {
            String key = entry.getKey();
            Object value = entry.getValue();
            if (value instanceof String s) tracer.setAttribute(span, key, s);
            else if (value instanceof Boolean b) tracer.setAttribute(span, key, b.booleanValue());
            else if (value instanceof Double || value instanceof Float) tracer.setAttribute(span, key, ((Number) value).doubleValue());
            else if (value instanceof Number n) tracer.setAttribute(span, key, n.longValue());
            else if (value instanceof List<?> list) tracer.setAttribute(span, key, (List<String>) list);
        }
    }
}
