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
 * <li>Never reflects over fields and never exports free-form text an application object declares: a record shows its
 * components by name (their accessors run once per capture, a {@link Memo} serves the cuts that repeat the walk; a component
 * with a credential name is redacted), a known JDK value type shows its value, and every other object is its type placeholder.
 * Its {@code toString()} is never run. Parsing arbitrary text for credential values cannot be made complete, so text an
 * application object chose to print is not trusted. A plain {@code String} (an argument or a return value) keeps its text, with
 * known secrets and credential shapes replaced, and the value of every credential-named {@code name=value} pair replaced to the
 * end of the pair (see {@link CredentialPairs#redactCredentialPairs}).</li>
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
            if (value instanceof java.net.URI uri) return text(uriText(uri));
            // A Date or Path that an application defines may declare its own text: epoch milliseconds for the first, the type for the second.
            if (value instanceof Date date && !jdk(value.getClass())) return text(Long.toString(date.getTime()));
            if (value instanceof java.nio.file.Path && !jdk(value.getClass())) return placeholder(value.getClass());
            if (value instanceof Character || value instanceof Enum<?> || value instanceof UUID || value instanceof Class<?>
                || value instanceof java.nio.file.Path || value instanceof Date || value instanceof Locale
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
            return placeholder(type);
        } catch (Throwable ignored) {
            try { return placeholder(value.getClass()); } catch (Throwable alsoIgnored) { return "<unknown>"; }
        }
    }

    /** A URI without its user information, its fragment and its query values: query keys stay, each with the placeholder for a value. */
    private static String uriText(java.net.URI uri) {
        if (uri.isOpaque()) return (uri.getScheme() == null ? "" : uri.getScheme() + ":") + "<opaque>";
        StringBuilder out = new StringBuilder();
        if (uri.getScheme() != null) out.append(uri.getScheme()).append(':');
        String authority = uri.getRawAuthority();
        if (authority != null) out.append("//").append(authority.substring(authority.lastIndexOf('@') + 1));
        if (uri.getRawPath() != null) out.append(uri.getRawPath());
        String query = uri.getRawQuery();
        if (query != null) {
            StringBuilder keys = new StringBuilder();
            for (String pair : query.split("&")) {
                if (keys.length() > 0) keys.append('&');
                int equals = pair.indexOf('=');
                keys.append(equals < 0 ? "<redacted>" : pair.substring(0, equals + 1) + "<redacted>"); // a bare segment can be a token too
            }
            out.append('?').append(keys);
        }
        return out.toString();
    }

    private static Object number(Number number) {
        double d = number.doubleValue();
        Object shown = Double.isNaN(d) || Double.isInfinite(d) ? number.toString() : number;
        return shown;
    }

    /** How far past the cut a credential that starts before it is still read, so that it is replaced whole. */
    private static final int SHAPE_REACH = 8192;

    /**
     * A string, cut to what the character budget still allows while it is read; the rest is never copied. Known secrets and
     * the catalog's credential shapes (keys, JWTs, Bearer and Basic values, private keys) are replaced in the window that is
     * read, which reaches past the cut so that a credential that straddles it is replaced whole; the cut is then made on the
     * replaced text, and what could still be the start of a known secret is dropped before the marker (as the core registry
     * does), so no part of a credential is left behind.
     */
    private String text(String value) {
        long room = Math.max(maxChars - chars, 0);
        try {
            // The order on every string path: known secrets, then the catalog's credential shapes, then the pair rule. A shape (a Basic or
            // Bearer value, a key, a JWT) must be matched before the pair rule removes the word that anchors it.
            KnownSecrets.Snapshot snapshot = KnownSecrets.PROCESS.snapshot();
            if (value.length() <= room) { String clean = replaced(snapshot, value, value.length()); chars += clean.length(); return clean; }
            chars = maxChars;
            // One bounded read: the cut plus a fixed reach for a credential that straddles it. The work does not depend on the value's length.
            int reach = (int)Math.min((long)value.length(), room + Math.max(snapshot.longest(), SHAPE_REACH) + READ_MARGIN);
            String read = replaced(snapshot, value, reach);
            if (reach == value.length() && read.length() <= room) return read; // all read, and it fits once replaced
            int end = (int)Math.min((long)read.length(), room);
            if (end > 0 && end < read.length() && Character.isHighSurrogate(read.charAt(end - 1))) end--;
            String kept = read.substring(0, end);
            // When replacements bring the cut within the shape reach of the end of the read, a shape that began before the cut may continue
            // past the end of the read and so not match. Fail closed: drop the trailing token that could start one, and the scheme word
            // before it; the text is marked truncated.
            if (reach < value.length() && read.length() - end < SHAPE_REACH) kept = dropShapePrefix(kept);
            // A run of token characters that holds a JWT start and goes on to the end of the read is not matched when the token is longer than
            // the read (a header that meets no dot, a payload that meets no second dot): the part before the cut is dropped with the whole run.
            if (reach < value.length()) kept = dropOpenRun(read, end, kept);
            kept = kept.substring(0, kept.length() - snapshot.trailingPartial(kept));
            // A cut that lands inside a placeholder drops the part of it.
            int open = kept.lastIndexOf('<');
            if (open >= 0 && "<redacted>".startsWith(kept.substring(open)) && !kept.substring(open).equals("<redacted>")) kept = kept.substring(0, open);
            return kept + "..." + CUT;
        } catch (RuntimeException unavailable) { return CUT; }
    }

    /** The margin the read window keeps past the cut and the reach. */
    private static final int READ_MARGIN = 1024;

    /** The first {@code reach} characters with known secrets, credential shapes and credential-named pairs replaced, in that order. */
    private static String replaced(KnownSecrets.Snapshot snapshot, String value, int reach) {
        String head = reach == value.length() ? value : value.substring(0, reach);
        return CredentialPairs.scrub(snapshot, head);
    }

    private static boolean tokenChar(char c) { return c >= 'a' && c <= 'z' || c >= 'A' && c <= 'Z' || c >= '0' && c <= '9' || "+/_=.-".indexOf(c) >= 0; }

    /** Cuts back to before the last token, and before a scheme word (Bearer, Basic, Negotiate, Digest, NTLM) in front of it. */
    private static String dropShapePrefix(String kept) {
        int end = kept.length();
        while (end > 0 && tokenChar(kept.charAt(end - 1))) end--;
        int word = end;
        while (word > 0 && kept.charAt(word - 1) == ' ') word--;
        for (String scheme : new String[] {"Bearer", "Basic", "Negotiate", "Digest", "NTLM"}) {
            if (word >= scheme.length() && kept.regionMatches(true, word - scheme.length(), scheme, 0, scheme.length())
                && (word == scheme.length() || !tokenChar(kept.charAt(word - scheme.length() - 1)))) { end = word - scheme.length(); break; }
        }
        return kept.substring(0, end);
    }

    private static boolean runChar(char c) { return c >= 'a' && c <= 'z' || c >= 'A' && c <= 'Z' || c >= '0' && c <= '9' || c == '_' || c == '.' || c == '-'; }

    /**
     * When the read stopped before the end of the value and the run of {@code [A-Za-z0-9_.-]} at the cut goes on to the end of the read,
     * that run may be the start of a token the read could not complete. If it holds {@code eyJ} at a word boundary the whole run is cut
     * from {@code kept}, which is then shorter than the budget; more than the token may go, and not less.
     */
    private static String dropOpenRun(String read, int end, String kept) {
        int stop = end;
        while (stop < read.length() && runChar(read.charAt(stop))) stop++;
        if (stop != read.length()) return kept;
        int start = end;
        while (start > 0 && runChar(read.charAt(start - 1))) start--;
        for (int at = read.indexOf("eyJ", start); at >= 0; at = read.indexOf("eyJ", at + 1)) {
            char before = at == 0 ? ' ' : read.charAt(at - 1);
            boolean word = before >= 'a' && before <= 'z' || before >= 'A' && before <= 'Z' || before >= '0' && before <= '9' || before == '_';
            if (!word) return kept.substring(0, Math.min(start, kept.length()));
        }
        return kept;
    }

    /** An accessor that threw. */
    private static final class Failed { }

    private static boolean jdk(Class<?> type) { String name = type.getName(); return name.startsWith("java.") || name.startsWith("jdk.") || name.startsWith("sun."); }
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
                    String shown = key(entry.getKey());
                    out.put(shown, CredentialPairs.credentialName(shown) ? "<redacted>" : walk(entry.getValue(), depth + 1));
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

    /**
     * A map key. Only the boxed primitives, {@code String}, {@code UUID} and enums are shown, and none of them runs application code: a
     * String key goes through the same pipeline as any text (known secrets, credential shapes, the pair rule), an enum shows its name.
     * Every other key type, application types and other {@code java.*} types alike, is its type placeholder: no {@code toString},
     * {@code hashCode} or {@code equals} of the key runs.
     */
    private String key(Object key) {
        if (key == null) { chars += 4; return "null"; }
        if (key instanceof String string) return text(string);
        Class<?> type = key.getClass();
        String shown;
        if (type == Integer.class || type == Long.class || type == Short.class || type == Byte.class || type == Double.class || type == Float.class
            || type == Boolean.class || type == Character.class || type == UUID.class) shown = key.toString();
        else if (key instanceof Enum<?> constant) shown = constant.name();
        else shown = placeholder(type);
        if (shown.length() > 256) shown = shown.substring(0, 256);
        chars += shown.length();
        return shown;
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
                out.put(parts[i].getName(), values[i] instanceof Failed ? "<unavailable>" : CredentialPairs.credentialName(parts[i].getName()) ? "<redacted>" : walk(values[i], depth + 1));
            }
            return out;
        } catch (RuntimeException inaccessible) {
            return placeholder(type);
        } finally { path.remove(value); }
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
