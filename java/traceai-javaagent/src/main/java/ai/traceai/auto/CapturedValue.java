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
 * <li>Never reflects over fields. A record shows its components (their accessors run), an object whose class declares its
 * own {@code toString()} shows that text with the value of every {@code name=value} or {@code name: value} pair whose name is
 * a credential name replaced, any other object is its type placeholder. A declared {@code toString()} and a record accessor
 * run at most once per capture (a {@link Memo} serves the cuts that repeat the walk), and a failure is a placeholder.</li>
 * <li>A {@code java.util.Collections} wrapper (unmodifiable, synchronized, checked) is a type placeholder: what it wraps is
 * unknown, and it may be lazy or guarded by a lock that capture must not take.</li>
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

    /** What application code returned during one capture, kept so that a repeated walk does not run it again. */
    static final class Memo {
        private final IdentityHashMap<Object, Optional<String>> texts = new IdentityHashMap<>();
        private final IdentityHashMap<Object, Object[]> components = new IdentityHashMap<>();
    }

    private int nodes;
    private long chars;
    private final long maxChars;
    private final Memo memo;
    private final IdentityHashMap<Object, Boolean> path = new IdentityHashMap<>();

    private CapturedValue(long maxChars, Memo memo) { this.maxChars = maxChars; this.memo = memo; }

    /** The view of {@code value}, with at most about {@code maxChars} characters of text in it. */
    static Object of(Object value, long maxChars) { return of(value, maxChars, new Memo()); }
    /** The same, sharing {@code memo} with the other walks of one capture. */
    static Object of(Object value, long maxChars, Memo memo) { return new CapturedValue(Math.max(maxChars, 16), memo).walk(value, 0); }

    private Object walk(Object value, int depth) {
        try {
            if (++nodes > MAX_NODES || chars >= maxChars) return CUT;
            if (value == null || value instanceof Boolean) return value;
            if (value instanceof Number number) {
                if (!number.getClass().getName().startsWith("java.")) return placeholder(number.getClass());
                chars += number.toString().length(); // numbers count against the text limit like any other text
                return number(number);
            }
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
        Object shown = Double.isNaN(d) || Double.isInfinite(d) ? number.toString() : number;
        return shown;
    }

    /**
     * A string, cut to what the character budget still allows while it is read; the rest is never copied. Known secrets are
     * replaced before the cut (in the part that is read, and in as many more characters as the longest secret), so a secret that
     * straddles the cut leaves no part of itself behind.
     */
    private String text(String value) {
        long room = Math.max(maxChars - chars, 0);
        if (value.length() <= room) { chars += value.length(); return value; }
        chars = maxChars;
        int end = (int)room;
        String read;
        try {
            KnownSecrets.Snapshot snapshot = KnownSecrets.PROCESS.snapshot();
            int reach = (int)Math.min((long)value.length(), room + snapshot.longest());
            read = snapshot.scrub(value.substring(0, reach));
        } catch (RuntimeException unavailable) { return CUT; }
        if (read.length() < end) end = read.length();
        if (end > 0 && end < read.length() && Character.isHighSurrogate(read.charAt(end - 1))) end--;
        String kept = read.substring(0, end);
        // A cut that lands inside a placeholder drops the part of it.
        int open = kept.lastIndexOf('<');
        if (open >= 0 && "<redacted>".startsWith(kept.substring(open)) && !kept.substring(open).equals("<redacted>")) kept = kept.substring(0, open);
        return kept + "..." + CUT;
    }

    /** An accessor that threw. */
    private static final class Failed { }

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

    private Object record(Object value, Class<?> type, int depth) {
        if (path.containsKey(value)) return CYCLE;
        path.put(value, Boolean.TRUE);
        try {
            Map<String, Object> out = new LinkedHashMap<>();
            RecordComponent[] parts = type.getRecordComponents();
            Object[] values = memo.components.get(value);
            if (values == null) {
                // Every accessor runs once per capture, here, and the values are kept for a repeated walk.
                values = new Object[parts.length];
                for (int i = 0; i < parts.length; i++) {
                    Method accessor = parts[i].getAccessor();
                    try { accessor.setAccessible(true); } catch (RuntimeException ignored) { }
                    try { values[i] = accessor.invoke(value); } catch (Throwable failed) { values[i] = new Failed(); }
                }
                memo.components.put(value, values);
            }
            for (int i = 0; i < parts.length; i++) {
                if (out.size() >= MAX_ITEMS || nodes > MAX_NODES || chars >= maxChars) { out.put(CUT, null); break; }
                chars += parts[i].getName().length();
                out.put(parts[i].getName(), values[i] instanceof Failed ? "<unavailable>" : walk(values[i], depth + 1));
            }
            return out;
        } catch (RuntimeException inaccessible) {
            return placeholder(type);
        } finally { path.remove(value); }
    }

    /** An application object shows the text its own class declares for it (run once, guarded, credential pairs redacted); one that declares none is its type. */
    private Object declaredToString(Object value, Class<?> type) {
        try {
            if (type.getMethod("toString").getDeclaringClass() == Object.class) return placeholder(type);
        } catch (ReflectiveOperationException | RuntimeException e) { return placeholder(type); }
        Optional<String> known = memo.texts.get(value);
        if (known == null) {
            String declared = null;
            try { declared = value.toString(); } catch (Throwable failed) { declared = null; }
            known = Optional.ofNullable(declared == null ? null : redactCredentialPairs(declared));
            memo.texts.put(value, known);
        }
        return known.isPresent() ? text(known.get()) : placeholder(type);
    }

    private static final java.util.regex.Pattern PAIR = java.util.regex.Pattern.compile("(?<![\\w.\\-])([A-Za-z_][\\w.\\-]*)(\\s*[=:]\\s*)");

    /** The value of every {@code name=value} / {@code name: value} pair whose name is a credential name becomes the placeholder. */
    static String redactCredentialPairs(String text) {
        java.util.regex.Matcher matcher = PAIR.matcher(text);
        StringBuilder out = null; int copied = 0, from = 0;
        while (from <= text.length() && matcher.find(from)) {
            int start = matcher.end();
            if (!WirePrivacy.credentialKey(matcher.group(1))) { from = start; continue; }
            int end = start;
            if (start < text.length() && (text.charAt(start) == '"' || text.charAt(start) == '\'')) {
                int close = text.indexOf(text.charAt(start), start + 1);
                end = close < 0 ? text.length() : close + 1;
            } else {
                while (end < text.length() && ",;)]}\n\r".indexOf(text.charAt(end)) < 0) end++;
            }
            if (out == null) out = new StringBuilder(text.length());
            out.append(text, copied, start).append("<redacted>");
            copied = end; from = Math.max(end, start);
            if (from >= text.length()) break;
        }
        return out == null ? text : out.append(text, copied, text.length()).toString();
    }

    /** JDK collections that hold their elements in memory: iterating one runs no I/O and cannot consume it. */
    private static boolean safe(Class<?> type) {
        String name = type.getName();
        if (SAFE.contains(name)) return true;
        // The Collections wrappers (Unmodifiable, Synchronized, Checked) are not here: what they wrap is unknown.
        return name.startsWith("java.util.ImmutableCollections$") || name.startsWith("java.util.Collections$Empty")
            || name.startsWith("java.util.Collections$Singleton") || name.startsWith("java.util.Collections$CopiesList");
    }
    private static final Set<String> SAFE = Set.of(
        "java.util.ArrayList", "java.util.LinkedList", "java.util.ArrayDeque", "java.util.HashMap", "java.util.LinkedHashMap", "java.util.TreeMap",
        "java.util.HashSet", "java.util.LinkedHashSet", "java.util.TreeSet", "java.util.EnumMap", "java.util.Arrays$ArrayList", "java.util.Vector",
        "java.util.concurrent.ConcurrentHashMap", "java.util.concurrent.CopyOnWriteArrayList", "java.util.concurrent.ConcurrentLinkedQueue",
        "java.util.concurrent.ConcurrentLinkedDeque", "java.util.concurrent.ConcurrentSkipListMap", "java.util.PriorityQueue", "java.util.Hashtable", "java.util.IdentityHashMap");
}
