package ai.traceai.auto;

import io.opentelemetry.api.common.*;
import io.opentelemetry.api.trace.*;
import io.opentelemetry.sdk.resources.Resource;
import java.lang.reflect.*;
import java.util.*;

/** Read-only views of the application's OpenTelemetry objects, across the shaded agent boundary.
 * Wire spans take their parent, span limit and resource from the application's active span or
 * registered SDK. Nothing here wraps, replaces or registers an application provider, processor or span.
 */
final class ApplicationOtel {
    private static final String PREFIX = String.join(".", "io", "opentelemetry");
    /** Read only an already-registered SDK: GlobalOpenTelemetry.get() would install a no-op
     * global before the application has configured its own provider. Never do that here. */
    static Object sdkProvider(ClassLoader loader) throws ReflectiveOperationException {
        Class<?> global = Class.forName(PREFIX + ".api.GlobalOpenTelemetry", false, loader);
        Field installed = global.getDeclaredField("globalOpenTelemetry"); installed.setAccessible(true);
        Object wrapper = installed.get(null); if (wrapper == null) return null;
        Field delegate = wrapper.getClass().getDeclaredField("delegate"); delegate.setAccessible(true);
        Object sdk = delegate.get(wrapper); Class<?> api = Class.forName(PREFIX + ".sdk.OpenTelemetrySdk", false, loader);
        return api.isInstance(sdk) ? api.getMethod("getSdkTracerProvider").invoke(sdk) : null;
    }
    static Resource resource(Object span, ClassLoader loader) {
        try {
            Object raw;
            Class<?> readable = Class.forName(PREFIX + ".sdk.trace.ReadableSpan", false, loader);
            if (span != null && readable.isInstance(span)) {
                Object data = readable.getMethod("toSpanData").invoke(span);
                raw = call(data, loader, ".sdk.trace.data.SpanData", "getResource");
            } else {
                Object provider = sdkProvider(loader); if (provider == null) return null;
                Field field = Class.forName(PREFIX + ".sdk.trace.SdkTracerProvider", false, loader).getDeclaredField("sharedState"); field.setAccessible(true);
                Object state = field.get(provider);
                Method resource = state.getClass().getDeclaredMethod("getResource"); resource.setAccessible(true); raw = resource.invoke(state);
            }
            return Resource.create(attributes(call(raw, loader, ".sdk.resources.Resource", "getAttributes"), loader), (String)call(raw, loader, ".sdk.resources.Resource", "getSchemaUrl"));
        } catch (Throwable ignored) { return null; }
    }
    static Integer spanLimit(Object span, ClassLoader loader) {
        try {
            Object limits = null;
            Class<?> sdkSpan = Class.forName(PREFIX + ".sdk.trace.SdkSpan", false, loader);
            if (sdkSpan.isInstance(span)) {
                Field field = sdkSpan.getDeclaredField("spanLimits"); field.setAccessible(true); limits = field.get(span);
            }
            if (limits == null) {
                Object provider = sdkProvider(loader);
                if (provider != null) limits = call(provider, loader, ".sdk.trace.SdkTracerProvider", "getSpanLimits");
            }
            return limits == null ? null : (Integer)call(limits, loader, ".sdk.trace.SpanLimits", "getMaxNumberOfAttributes");
        } catch (Throwable ignored) { return null; }
    }
    static Object call(Object value, ClassLoader loader, String api, String name) throws ReflectiveOperationException {
        return Class.forName(PREFIX + api, false, loader).getMethod(name).invoke(value);
    }
    static SpanContext context(Object value, ClassLoader loader) throws ReflectiveOperationException {
        String api = ".api.trace.SpanContext";
        TraceStateBuilder state = TraceState.builder();
        Map<?, ?> entries = (Map<?, ?>)call(call(value, loader, api, "getTraceState"), loader, ".api.trace.TraceState", "asMap");
        List<?> keys = new ArrayList<>(entries.keySet());
        for (int i = keys.size() - 1; i >= 0; i--) state.put(String.valueOf(keys.get(i)), String.valueOf(entries.get(keys.get(i))));
        String trace = (String)call(value, loader, api, "getTraceId"), span = (String)call(value, loader, api, "getSpanId");
        TraceFlags flags = TraceFlags.fromByte((byte)call(call(value, loader, api, "getTraceFlags"), loader, ".api.trace.TraceFlags", "asByte"));
        return (boolean)call(value, loader, api, "isRemote") ? SpanContext.createFromRemoteParent(trace, span, flags, state.build()) : SpanContext.create(trace, span, flags, state.build());
    }
    @SuppressWarnings({"unchecked", "rawtypes"})
    private static Attributes attributes(Object value, ClassLoader loader) throws ReflectiveOperationException {
        Map<?, ?> data = (Map<?, ?>)call(value, loader, ".api.common.Attributes", "asMap");
        AttributesBuilder builder = Attributes.builder();
        for (var entry : data.entrySet()) {
            String key = (String)call(entry.getKey(), loader, ".api.common.AttributeKey", "getKey");
            Object clean = entry.getValue();
            String type = String.valueOf(call(entry.getKey(), loader, ".api.common.AttributeKey", "getType"));
            AttributeKey typed = clean instanceof String ? AttributeKey.stringKey(key) : switch (type) {
                case "BOOLEAN" -> AttributeKey.booleanKey(key); case "LONG" -> AttributeKey.longKey(key); case "DOUBLE" -> AttributeKey.doubleKey(key);
                case "BOOLEAN_ARRAY" -> AttributeKey.booleanArrayKey(key); case "LONG_ARRAY" -> AttributeKey.longArrayKey(key); case "DOUBLE_ARRAY" -> AttributeKey.doubleArrayKey(key);
                case "STRING_ARRAY" -> AttributeKey.stringArrayKey(key); default -> AttributeKey.stringKey(key);
            };
            builder.put(typed, clean);
        }
        return builder.build();
    }
    private ApplicationOtel() {}
}
