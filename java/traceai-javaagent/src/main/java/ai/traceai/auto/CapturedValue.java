package ai.traceai.auto;

import java.lang.reflect.Method;
import java.lang.reflect.RecordComponent;
import java.util.*;

/**
 * A bounded, JSON-safe view of an application value (an argument or a return value of an annotated method), built
 * without running application code that can change the application's behaviour.
 *
 * <ul>
 * <li>Never iterates anything that is not a fully materialised JDK collection: an {@code Iterable}, {@code Map},
 * {@code Iterator}, {@code Enumeration} or {@code Stream} of any other class (a {@code DirectoryStream}, a stream-backed
 * iterable, a lazy map, a database cursor) becomes the placeholder {@code "<fully.qualified.Type>"}.</li>
 * <li>Never reflects over fields. A record shows its components, an object whose class declares its own
 * {@code toString()} shows that text, any other object is its type placeholder.</li>
 * <li>{@code char[]} and {@code byte[]} are the placeholders {@code "<char[n]>"} and {@code "<byte[n]>"}: they are the
 * usual carriers of passwords and tokens, and are never rendered.</li>
 * <li>Bounded while it walks: depth, items per container, total nodes and total characters are enforced as the tree is
 * built, so a huge value is never built whole. A cycle, found by identity on the path from the root, is {@code "<cycle>"}.</li>
 * <li>Never throws: anything that fails becomes a placeholder.</li>
 * </ul>
 */
final class CapturedValue {
    static final int MAX_DEPTH = 6, MAX_ITEMS = 100, MAX_NODES = 2_000;
    static final String CYCLE = "<cycle>", DEPTH = "<depth limit>", CUT = "<truncated>";

    private int nodes;
    private long chars;
    private final long maxChars;
    private final IdentityHashMap<Object, Boolean> path = new IdentityHashMap<>();

    private CapturedValue(long maxChars) { this.maxChars = maxChars; }

    /** The view of {@code value}, with at most about {@code maxChars} characters of text in it. */
    static Object of(Object value, long maxChars) { return new CapturedValue(Math.max(maxChars, 16)).walk(value, 0); }

    private Object walk(Object value, int depth) {
        try {
            if (++nodes > MAX_NODES || chars >= maxChars) return CUT;
            if (value == null || value instanceof Boolean) return value;
            if (value instanceof Number number) return number.getClass().getName().startsWith("java.") ? number(number) : placeholder(number.getClass());
            if (value instanceof String string) return text(string);
            if (value instanceof Character || value instanceof Enum<?> || value instanceof UUID || value instanceof Class<?>
                || value instanceof java.net.URI || value instanceof java.nio.file.Path || value instanceof Date || value instanceof Locale
                || value.getClass().getName().startsWith("java.time.")) {
                return text(value instanceof Enum<?> e ? e.name() : value instanceof Class<?> c ? c.getName() : value.toString());
            }
            if (value instanceof StringBuilder || value instanceof StringBuffer) return text(value.toString());
            Class<?> type = value.getClass();
            if (type.isArray()) return array(value, type, depth);
            if (depth >= MAX_DEPTH) return DEPTH;
            if (value instanceof Optional<?> optional) return optional.isPresent() ? walk(optional.get(), depth + 1) : null;
            if (value instanceof Map<?, ?> map) return safe(type) ? container(map, depth) : placeholder(type);
            if (value instanceof Iterable<?> items) return safe(type) ? container(items, depth) : placeholder(type);
            if (value instanceof Iterator<?> || value instanceof Enumeration<?> || value instanceof java.util.stream.BaseStream<?, ?>
                || value instanceof AutoCloseable || value instanceof java.util.concurrent.Future<?> || value instanceof Throwable) return placeholder(type);
            if (type.isRecord()) return record(value, type, depth);
            if (type.getName().startsWith("java.") || type.getName().startsWith("jdk.") || type.getName().startsWith("sun.")) return placeholder(type);
            return declaredToString(value, type);
        } catch (Throwable ignored) {
            try { return placeholder(value.getClass()); } catch (Throwable alsoIgnored) { return "<unknown>"; }
        }
    }

    private static Object number(Number number) {
        double d = number.doubleValue();
        return Double.isNaN(d) || Double.isInfinite(d) ? number.toString() : number;
    }

    /** A string, cut to what the character budget still allows while it is read; the rest is never copied. */
    private String text(String value) {
        long room = Math.max(maxChars - chars, 0);
        if (value.length() <= room) { chars += value.length(); return value; }
        chars = maxChars;
        int end = (int)room;
        if (end > 0 && end < value.length() && Character.isHighSurrogate(value.charAt(end - 1))) end--;
        return value.substring(0, end) + "..." + CUT;
    }

    private static String placeholder(Class<?> type) { return "<" + type.getName() + ">"; }

    private Object array(Object array, Class<?> type, int depth) {
        if (type == char[].class || type == byte[].class) return "<" + type.getComponentType().getName() + "[" + java.lang.reflect.Array.getLength(array) + "]>";
        if (depth >= MAX_DEPTH) return DEPTH;
        if (!path.isEmpty() && path.containsKey(array)) return CYCLE;
        path.put(array, Boolean.TRUE);
        try {
            int length = java.lang.reflect.Array.getLength(array);
            List<Object> out = new ArrayList<>();
            for (int i = 0; i < length; i++) {
                if (i >= MAX_ITEMS || nodes > MAX_NODES || chars >= maxChars) { out.add(CUT); break; }
                out.add(walk(java.lang.reflect.Array.get(array, i), depth + 1));
            }
            return out;
        } finally { path.remove(array); }
    }

    private Object container(Object value, int depth) {
        if (path.containsKey(value)) return CYCLE;
        path.put(value, Boolean.TRUE);
        try {
            if (value instanceof Map<?, ?> map) {
                Map<String, Object> out = new LinkedHashMap<>(); int n = 0;
                for (Map.Entry<?, ?> entry : map.entrySet()) {
                    if (n++ >= MAX_ITEMS || nodes > MAX_NODES || chars >= maxChars) { out.put(CUT, null); break; }
                    out.put(key(entry.getKey()), walk(entry.getValue(), depth + 1));
                }
                return out;
            }
            List<Object> out = new ArrayList<>(); int n = 0;
            for (Object item : (Iterable<?>)value) {
                if (n++ >= MAX_ITEMS || nodes > MAX_NODES || chars >= maxChars) { out.add(CUT); break; }
                out.add(walk(item, depth + 1));
            }
            return out;
        } catch (ConcurrentModificationException changing) {
            return placeholder(value.getClass());
        } finally { path.remove(value); }
    }

    /** A map key: only scalars are shown as themselves, anything else is its type placeholder (no application toString). */
    private String key(Object key) {
        Object shown = key == null || key instanceof Number || key instanceof Boolean || key instanceof String || key instanceof Character || key instanceof Enum<?> || key instanceof UUID
            ? (key instanceof Enum<?> e ? e.name() : String.valueOf(key)) : placeholder(key.getClass());
        String text = String.valueOf(shown);
        if (text.length() > 256) text = text.substring(0, 256);
        chars += text.length();
        return text;
    }

    private Object record(Object value, Class<?> type, int depth) throws ReflectiveOperationException {
        if (path.containsKey(value)) return CYCLE;
        path.put(value, Boolean.TRUE);
        try {
            Map<String, Object> out = new LinkedHashMap<>();
            for (RecordComponent component : type.getRecordComponents()) {
                if (out.size() >= MAX_ITEMS || nodes > MAX_NODES || chars >= maxChars) { out.put(CUT, null); break; }
                Method accessor = component.getAccessor();
                try { accessor.setAccessible(true); } catch (RuntimeException ignored) { }
                chars += component.getName().length();
                out.put(component.getName(), walk(accessor.invoke(value), depth + 1));
            }
            return out;
        } catch (IllegalAccessException | java.lang.reflect.InvocationTargetException | RuntimeException inaccessible) {
            return placeholder(type);
        } finally { path.remove(value); }
    }

    /** An application object shows the text its own class declares for it; one that declares none is its type. */
    private Object declaredToString(Object value, Class<?> type) {
        try {
            if (type.getMethod("toString").getDeclaringClass() == Object.class) return placeholder(type);
        } catch (ReflectiveOperationException | RuntimeException e) { return placeholder(type); }
        return text(String.valueOf(value));
    }

    /** JDK collections that hold their elements in memory: iterating one runs no I/O and cannot consume it. */
    private static boolean safe(Class<?> type) {
        String name = type.getName();
        if (SAFE.contains(name)) return true;
        return name.startsWith("java.util.ImmutableCollections$") || name.startsWith("java.util.Collections$Unmodifiable")
            || name.startsWith("java.util.Collections$Empty") || name.startsWith("java.util.Collections$Singleton") || name.startsWith("java.util.Collections$CopiesList")
            || name.startsWith("java.util.Collections$Synchronized") || name.startsWith("java.util.Collections$Checked");
    }
    private static final Set<String> SAFE = Set.of(
        "java.util.ArrayList", "java.util.LinkedList", "java.util.ArrayDeque", "java.util.HashMap", "java.util.LinkedHashMap", "java.util.TreeMap",
        "java.util.HashSet", "java.util.LinkedHashSet", "java.util.TreeSet", "java.util.EnumMap", "java.util.Arrays$ArrayList", "java.util.Vector",
        "java.util.concurrent.ConcurrentHashMap", "java.util.concurrent.CopyOnWriteArrayList", "java.util.concurrent.ConcurrentLinkedQueue",
        "java.util.concurrent.ConcurrentLinkedDeque", "java.util.concurrent.ConcurrentSkipListMap", "java.util.PriorityQueue", "java.util.Hashtable", "java.util.IdentityHashMap");
}
