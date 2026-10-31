package ai.traceai.auto;

import java.lang.ref.WeakReference;
import java.util.*;

/** Tracks ancestry rather than only the current span; kinds may be set after onStart. */
final class CoveredSpans {
    private record Entry(WeakReference<Object> span, ClassLoader loader, String parent, boolean covered) { }
    private static final Map<String, Entry> SPANS = new LinkedHashMap<>();
    static synchronized void started(Object span, ClassLoader loader) {
        try {
            String id = ApplicationOtel.context(ApplicationOtel.call(span, loader, ".sdk.trace.ReadableSpan", "getSpanContext"), loader).getSpanId();
            String parent = ApplicationOtel.context(ApplicationOtel.call(span, loader, ".sdk.trace.ReadableSpan", "getParentSpanContext"), loader).getSpanId();
            if (SPANS.size() >= 16384) SPANS.remove(SPANS.keySet().iterator().next());
            SPANS.put(id, new Entry(new WeakReference<>(span), loader, parent, covered(parent)));
        } catch (Throwable ignored) { }
    }
    static synchronized boolean covered(String id) {
        Set<String> seen = new HashSet<>();
        for (int i = 0; i < 256 && seen.add(id); i++) {
            Entry entry = SPANS.get(id); if (entry == null) return false;
            if (entry.covered) return true;
            Object span = entry.span.get();
            if (span != null) try {
                String prefix = String.join(".", "io", "opentelemetry");
                Class<?> keyApi = Class.forName(prefix + ".api.common.AttributeKey", false, entry.loader);
                Class<?> readable = Class.forName(prefix + ".sdk.trace.ReadableSpan", false, entry.loader);
                for (String key : List.of("gen_ai.span.kind", "fi.span.kind")) {
                    Object value = readable.getMethod("getAttribute", keyApi).invoke(span, keyApi.getMethod("stringKey", String.class).invoke(null, key));
                    if (value instanceof String kind && OtelBridge.library(kind)) return true;
                }
            } catch (Throwable ignored) { }
            id = entry.parent;
        }
        return false;
    }
    private CoveredSpans() {}
}
