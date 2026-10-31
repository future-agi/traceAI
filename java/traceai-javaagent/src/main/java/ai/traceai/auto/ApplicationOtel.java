package ai.traceai.auto;

import io.opentelemetry.api.common.*;
import io.opentelemetry.api.trace.*;
import io.opentelemetry.sdk.common.*;
import io.opentelemetry.sdk.resources.Resource;
import io.opentelemetry.sdk.trace.ReadableSpan;
import io.opentelemetry.sdk.trace.data.*;
import java.lang.ref.WeakReference;
import java.lang.reflect.*;
import java.util.*;

/** Adds a forwarding processor at the application SDK's processor lookup, including already-built SDKs.
 * No private final fields are mutated, and application processors/providers are never owned or retired.
 */
final class ApplicationOtel {
    private static final String PREFIX = String.join(".", "io", "opentelemetry");
    private static final Map<Object, WeakReference<Object>> PROCESSORS = new WeakHashMap<>();
    static Object processor(Object original) {
        try {
            ClassLoader loader = original.getClass().getClassLoader();
            Class<?> api = Class.forName(PREFIX + ".sdk.trace.SpanProcessor", false, loader);
            synchronized (PROCESSORS) {
                WeakReference<Object> reference = PROCESSORS.get(original); Object cached = reference == null ? null : reference.get();
                if (cached != null) return cached;
                Object forwarding = Proxy.newProxyInstance(loader, new Class<?>[]{api}, (proxy, method, args) -> {
                    if (method.getName().equals("isEndRequired") || method.getName().equals("isStartRequired")) return true;
                    if (method.getName().equals("onStart")) {
                        CoveredSpans.started(args[1], loader);
                        try { if (!(boolean)api.getMethod("isStartRequired").invoke(original)) return null; }
                        catch (InvocationTargetException error) { throw error.getCause(); }
                    }
                    if (method.getName().equals("onEnd")) {
                        // Forward independently: an application processor's failure must not disable our exporter.
                        try { ended(args[0], loader); } catch (Throwable ignored) { }
                        // Enabling our callback must not enable an end callback the application opted out of.
                        try { if (!(boolean)api.getMethod("isEndRequired").invoke(original)) return null; }
                        catch (InvocationTargetException error) { throw error.getCause(); }
                    }
                    try { return method.invoke(original, args); }
                    catch (InvocationTargetException error) { throw error.getCause(); }
                });
                PROCESSORS.put(original, new WeakReference<>(forwarding)); return forwarding;
            }
        } catch (Throwable ignored) { return original; }
    }
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
    private static void ended(Object readable, ClassLoader loader) throws ReflectiveOperationException {
        TraceAIAuto.RuntimeState state = TraceAIAuto.currentRuntime();
        if (state == null || state.processor == null || !state.startFinalizing()) return;
        try {
            SpanData snapshot = snapshot(call(readable, loader, ".sdk.trace.ReadableSpan", "toSpanData"), loader, state.config(), state.resource);
            ReadableSpan forwarded = (ReadableSpan)Proxy.newProxyInstance(ApplicationOtel.class.getClassLoader(), new Class<?>[]{ReadableSpan.class}, (proxy, method, args) -> method.getName().equals("toSpanData") ? snapshot : method.getName().equals("getSpanContext") ? snapshot.getSpanContext() : null);
            state.processor.onEnd(forwarded);
        } finally { state.endFinalizing(); }
    }
    static SpanData snapshot(Object data, ClassLoader loader, AutoConfig config, Resource agentResource) throws ReflectiveOperationException {
            // This is a type/classloader adapter only. Privacy runs once, at export.
            Map<String, Object> values = new HashMap<>(); String api = ".sdk.trace.data.SpanData";
            SpanContext sc = context(call(data, loader, api, "getSpanContext"), loader);
            SpanContext parent = context(call(data, loader, api, "getParentSpanContext"), loader);
            values.put("getSpanContext", sc); values.put("getParentSpanContext", parent);
            values.put("getTraceId", sc.getTraceId()); values.put("getSpanId", sc.getSpanId()); values.put("getParentSpanId", parent.getSpanId());
            values.put("getName", (String)call(data, loader, api, "getName"));
            values.put("getKind", SpanKind.valueOf(String.valueOf(call(data, loader, api, "getKind"))));
            for (String name : List.of("getStartEpochNanos", "getEndEpochNanos", "hasEnded", "getTotalRecordedEvents", "getTotalRecordedLinks", "getTotalAttributeCount")) values.put(name, call(data, loader, api, name));
            Object status = call(data, loader, api, "getStatus");
            values.put("getStatus", StatusData.create(StatusCode.valueOf(String.valueOf(call(status, loader, ".sdk.trace.data.StatusData", "getStatusCode"))), (String)call(status, loader, ".sdk.trace.data.StatusData", "getDescription")));
            values.put("getAttributes", attributes(call(data, loader, api, "getAttributes"), loader));
            Object resource = call(data, loader, api, "getResource");
            values.put("getResource", TraceAIAuto.withRouting(Resource.create(attributes(call(resource, loader, ".sdk.resources.Resource", "getAttributes"), loader),
                (String)call(resource, loader, ".sdk.resources.Resource", "getSchemaUrl")), agentResource));
            Object scope = call(data, loader, api, "getInstrumentationScopeInfo");
            String name = (String)call(scope, loader, ".sdk.common.InstrumentationScopeInfo", "getName");
            String version = (String)call(scope, loader, ".sdk.common.InstrumentationScopeInfo", "getVersion");
            String schema = (String)call(scope, loader, ".sdk.common.InstrumentationScopeInfo", "getSchemaUrl");
            values.put("getInstrumentationScopeInfo", InstrumentationScopeInfo.builder(name).setVersion(version).setSchemaUrl(schema)
                .setAttributes(attributes(call(scope, loader, ".sdk.common.InstrumentationScopeInfo", "getAttributes"), loader)).build());
            values.put("getInstrumentationLibraryInfo", InstrumentationLibraryInfo.create(name, version, schema));
            List<EventData> events = new ArrayList<>();
            for (Object event : (List<?>)call(data, loader, api, "getEvents")) {
                Attributes attrs = attributes(call(event, loader, ".sdk.trace.data.EventData", "getAttributes"), loader);
                events.add(EventData.create((long)call(event, loader, ".sdk.trace.data.EventData", "getEpochNanos"),
                    (String)call(event, loader, ".sdk.trace.data.EventData", "getName"), attrs,
                    (int)call(event, loader, ".sdk.trace.data.EventData", "getTotalAttributeCount")));
            }
            List<LinkData> links = new ArrayList<>();
            for (Object link : (List<?>)call(data, loader, api, "getLinks")) links.add(LinkData.create(context(call(link, loader, ".sdk.trace.data.LinkData", "getSpanContext"), loader),
                attributes(call(link, loader, ".sdk.trace.data.LinkData", "getAttributes"), loader),
                (int)call(link, loader, ".sdk.trace.data.LinkData", "getTotalAttributeCount")));
            values.put("getEvents", List.copyOf(events)); values.put("getLinks", List.copyOf(links));
            return (SpanData)Proxy.newProxyInstance(ApplicationOtel.class.getClassLoader(), new Class<?>[]{SpanData.class}, (proxy, method, args) -> values.get(method.getName()));
    }
    private ApplicationOtel() {}
}
