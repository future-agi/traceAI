package ai.traceai.auto;

import java.lang.ref.*;
import java.util.*;
import okhttp3.ResponseBody;
import okio.Buffer;

/** Observe original sources in place: no response/body/source replacement and no eager source() calls. */
final class OkHttpSources {
    static final int TRANSPORT = 1, NETWORK = 2, DECODED = 4, OUTPUT = 8;
    private record Binding(WireCapture capture, int roles) { }
    private static final ReferenceQueue<Object> QUEUE = new ReferenceQueue<>();
    private static final Map<Key, Binding> BINDINGS = new HashMap<>();
    private static final class Key extends WeakReference<Object> {
        final int hash;
        Key(Object value) { super(value, QUEUE); hash = System.identityHashCode(value); }
        @Override public int hashCode() { return hash; }
        @Override public boolean equals(Object other) { return this == other || other instanceof Key key && get() != null && get() == key.get(); }
    }
    private static synchronized Binding get(Object value) {
        Reference<?> expired; while ((expired = QUEUE.poll()) != null) BINDINGS.remove(expired);
        return value == null ? null : BINDINGS.get(new Key(value));
    }
    private static synchronized void bind(Object value, Binding binding) {
        if (value == null) return;
        Binding existing = get(value);
        if (existing != null && existing.capture == binding.capture) binding = new Binding(binding.capture, binding.roles | existing.roles);
        BINDINGS.put(new Key(value), binding);
    }
    static void body(ResponseBody body, WireCapture capture, int roles) { if (body != null && capture != null) bind(body, new Binding(capture, roles)); }
    static void source(Object body, Object source) {
        Binding binding = get(body); if (binding == null || source == null) return;
        // Native BufferedSource helpers read this field directly, bypassing read(Buffer,long).
        // Reading a known library field neither calls application code nor consumes a byte.
        if (source.getClass().getName().equals("okio.RealBufferedSource")) try {
            var field = source.getClass().getDeclaredField("source"); field.setAccessible(true); bind(field.get(source), binding);
        } catch (ReflectiveOperationException ignored) { }
        else bind(source, binding);
    }
    private record Reading(Object source, Binding binding) { }
    private static final ThreadLocal<Set<Object>> READING = ThreadLocal.withInitial(() -> Collections.newSetFromMap(new IdentityHashMap<>()));
    static Object reading(Object source) {
        Binding binding = get(source);
        return binding != null && READING.get().add(source) ? new Reading(source, binding) : null;
    }
    static void read(Object token, Object rawSink, long count, Throwable error, long observed) {
        if (!(token instanceof Reading reading)) return;
        READING.get().remove(reading.source);
        Binding binding = reading.binding;
        WireCapture capture = binding.capture; int roles = binding.roles;
        if (count > 0 && (roles & TRANSPORT) != 0) capture.transportBytes(count, observed);
        if (error != null) {
            capture.errorObserved(observed);
            if ((roles & TRANSPORT) != 0) capture.transportFailure(error);
            if ((roles & NETWORK) != 0) capture.networkFailure(error);
            if ((roles & DECODED) != 0) capture.decodingFailure(error);
            if ((roles & OUTPUT) != 0) capture.readFailure(error);
        } else if ((roles & OUTPUT) != 0 || (roles & TRANSPORT) != 0 && !capture.transportReturned) {
            if (count > 0 && rawSink instanceof Buffer sink) OkHttpTransport.copy(sink, sink.size() - count, count, capture, false, capture.transportByteNanos == 0 ? observed : capture.transportByteNanos);
            else if (count < 0 && capture.transportReturned) capture.finish(null);
        }
    }
    static void closed(Object token, Throwable error) {
        if (!(token instanceof Reading reading)) return;
        read(token, null, 0, error, System.nanoTime());
        if ((reading.binding.roles & OUTPUT) != 0) reading.binding.capture.close();
    }
    private OkHttpSources() {}
}
