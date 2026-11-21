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
 * end of the pair (see {@link #redactCredentialPairs}).</li>
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
            // Replacements shrank the window: the cut is at (or near) its edge, where a shape that began before the cut may have been cut
            // short and no longer match. Fail closed: drop the trailing token that could start one, and the scheme word before it.
            if (reach < value.length() && read.length() - end < SHAPE_GUARD) kept = dropShapePrefix(kept);
            kept = kept.substring(0, kept.length() - snapshot.trailingPartial(kept));
            // A cut that lands inside a placeholder drops the part of it.
            int open = kept.lastIndexOf('<');
            if (open >= 0 && "<redacted>".startsWith(kept.substring(open)) && !kept.substring(open).equals("<redacted>")) kept = kept.substring(0, open);
            return kept + "..." + CUT;
        } catch (RuntimeException unavailable) { return CUT; }
    }

    /** What is left of the window beyond the cut, below which the cut counts as being at the window's edge. */
    private static final int SHAPE_GUARD = 512, READ_MARGIN = 1024;

    /** The first {@code reach} characters with known secrets, credential shapes and credential-named pairs replaced, in that order. */
    private static String replaced(KnownSecrets.Snapshot snapshot, String value, int reach) {
        String head = reach == value.length() ? value : value.substring(0, reach);
        return redactCredentialPairs(PrivacyCatalog.INSTANCE.scrubValueShapes(snapshot.scrub(head)));
    }

    private static boolean tokenChar(char c) { return c >= 'a' && c <= 'z' || c >= 'A' && c <= 'Z' || c >= '0' && c <= '9' || "+/_=.-".indexOf(c) >= 0; }

    /** Cuts back to before the last token, and before a scheme word (Bearer, Basic) in front of it. */
    private static String dropShapePrefix(String kept) {
        int end = kept.length();
        while (end > 0 && tokenChar(kept.charAt(end - 1))) end--;
        int word = end;
        while (word > 0 && kept.charAt(word - 1) == ' ') word--;
        for (String scheme : new String[] {"Bearer", "Basic"}) {
            if (word >= scheme.length() && kept.regionMatches(true, word - scheme.length(), scheme, 0, scheme.length())
                && (word == scheme.length() || !tokenChar(kept.charAt(word - scheme.length() - 1)))) { end = word - scheme.length(); break; }
        }
        return kept.substring(0, end);
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
                    out.put(shown, credentialName(shown) ? "<redacted>" : walk(entry.getValue(), depth + 1));
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
                out.put(parts[i].getName(), values[i] instanceof Failed ? "<unavailable>" : credentialName(parts[i].getName()) ? "<redacted>" : walk(values[i], depth + 1));
            }
            return out;
        } catch (RuntimeException inaccessible) {
            return placeholder(type);
        } finally { path.remove(value); }
    }

    /**
     * Whether a name, as a parameter, a record component, a Map key or a name in text, is a credential name. It is the export catalog's
     * rule, which also knows {@code pwd}, {@code pass}, {@code passphrase}, {@code passcode}, {@code pin} and cookie names as a whole name or
     * a segment ({@code db.password}, {@code dbPass}), here with the command-line and bracket spellings undone first: {@code --password},
     * {@code -Ddb.password} and {@code user[password]}. Over-redaction fails closed.
     */
    static boolean credentialName(String name) {
        if (name == null || name.isEmpty()) return false;
        String stripped = name.replaceFirst("^-+", "");
        if (stripped.startsWith("D") && stripped.length() > 1 && name.startsWith("-D")) stripped = stripped.substring(1);
        stripped = stripped.replace('[', '.').replace(']', '.');
        return !stripped.isEmpty() && (WirePrivacy.credentialKey(stripped) || WirePrivacy.credentialKey(name));
    }

    /** Headers whose value is itself a list of pairs: everything to the end of the line is the value. */
    private static boolean listHeader(String name) {
        String folded = PrivacyCatalog.normalize(name);
        return folded.equals("cookie") || folded.equals("setcookie") || folded.equals("authorization") || folded.equals("proxyauthorization");
    }
    /** The index of the opening quote of the JSON name that ends just before the colon at {@code colon}, or -1 when there is none. */
    private static int jsonName(String text, int colon) {
        int end = colon;
        while (end > 0 && Character.isWhitespace(text.charAt(end - 1)) && colon - end < 8) end--;
        if (end < 3 || text.charAt(end - 1) != '"' && text.charAt(end - 1) != '\'') return -1;
        int open = text.lastIndexOf(text.charAt(end - 1), end - 2);
        return open >= 0 && end - 1 - open <= 130 ? open : -1;
    }

    /** The index after the JSON value that starts at {@code start}: a string, a balanced object or array (quote-aware), or a scalar. */
    private static int jsonValueEnd(String text, int start) {
        int length = text.length();
        if (start >= length) return start;
        char first = text.charAt(start);
        if (first == '"' || first == '\'') {
            for (int i = start + 1; i < length; i++) {
                char c = text.charAt(i);
                if (c == '\\') i++;
                else if (c == first) return i + 1;
            }
            return length;
        }
        if (first == '{' || first == '[') {
            int depth = 0;
            for (int i = start; i < length; i++) {
                char c = text.charAt(i);
                if (c == '"' || c == '\'') { i = jsonValueEnd(text, i) - 1; continue; }
                if (c == '{' || c == '[') depth++;
                else if ((c == '}' || c == ']') && --depth == 0) return i + 1;
            }
            return length;
        }
        int i = start;
        while (i < length && ",}] \t\r\n".indexOf(text.charAt(i)) < 0) i++;
        return i;
    }

    private static boolean nameChar(char c) { return c >= 'a' && c <= 'z' || c >= 'A' && c <= 'Z' || c >= '0' && c <= '9' || c == '_' || c == '.' || c == '-' || c == '[' || c == ']'; }

    /**
     * The start of another pair after a separator: a name, then a colon, or an equals sign that is not base64 padding (an equals sign
     * followed by nothing, a space, another equals sign or a closing or separating character is padding or an empty value). A bounded
     * run of spaces may come first; every part is anchored at one position, so scanning is linear.
     */
    private static final java.util.regex.Pattern NEXT_PAIR = java.util.regex.Pattern.compile("\\G[ \\t]{0,8}-{0,2}[A-Za-z_][\\w.\\-\\[\\]]{0,127}[ ]{0,8}(?::|=(?![=\\s,;)\\]}\"']|$))");
    private static final java.util.regex.Pattern NEXT_PAIR_TIGHT = java.util.regex.Pattern.compile("\\G-{0,2}[A-Za-z_][\\w.\\-\\[\\]]{0,127}(?::|=(?![=\\s,;)\\]}\"']|$))");

    /**
     * The value of every {@code name=value} / {@code name: value} pair whose name is a credential name becomes the placeholder, in one
     * left-to-right pass whose work is linear in the text. A value ends only where the next pair starts (after a comma, semicolon, line
     * break or space) or at the end of the text; no bracket, parenthesis, brace or quote inside it ends it early, so a credential that
     * contains one is replaced whole (and what closes a group around the pair goes with it: over-redaction fails closed). A value that
     * starts with a private key header runs to the end line of the key. For a header whose value is a list of pairs (cookie, set-cookie,
     * authorization, proxy-authorization) the value is the rest of the line. A command-line flag with a credential name takes the next
     * word ({@code --password value}), and the user information of any {@code scheme://user:password@host} is replaced.
     * The limit: a credential value that itself contains {@code , name=} (or {@code ; name:}) ends there, where it looks like the next pair.
     */
    static String redactCredentialPairs(String text) {
        int length = text.length();
        StringBuilder out = null; int copied = 0;
        java.util.HashMap<String, Boolean> known = new java.util.HashMap<>();
        for (int i = 0; i < length; ) {
            char c = text.charAt(i);
            int from = -1, to = -1; String replacement = "<redacted>";
            int quoted = c == ':' && i > 0 && i < length ? jsonName(text, i) : -1;
            if (quoted >= 0) {
                // "name": value in JSON text: the value is read exactly (a string, an object or array, or a scalar) and replaced by a JSON string.
                char quote = text.charAt(quoted);
                String name = text.substring(quoted + 1, text.lastIndexOf(quote, i - 1));
                if (known.computeIfAbsent(name, CapturedValue::credentialName)) {
                    int valueStart = i + 1;
                    while (valueStart < length && Character.isWhitespace(text.charAt(valueStart))) valueStart++;
                    int end = jsonValueEnd(text, valueStart);
                    if (end > valueStart) { from = valueStart; to = end; replacement = quote + "<redacted>" + quote; }
                }
                if (from < 0) { i++; continue; }
            } else if (c == ':' && text.startsWith("//", i + 1)) {
                // scheme://user:password@host: the user information goes, for any scheme.
                int limit = Math.min(length, i + 3 + 512), end = i + 3;
                while (end < limit && "/?# \t\r\n".indexOf(text.charAt(end)) < 0) end++;
                int at = text.lastIndexOf('@', end - 1);
                if (at < i + 3) {
                    // A password may hold a slash, question mark or hash: when a user:password form precedes an at sign later in the same
                    // word, the user information runs to that at sign.
                    int word = i + 3;
                    while (word < length && word - i < 1024 && !Character.isWhitespace(text.charAt(word))) word++;
                    int later = text.lastIndexOf('@', word - 1);
                    int colon = later > i + 3 ? text.indexOf(':', i + 3) : -1;
                    if (later > i + 3 && colon > i + 3 && colon < later) at = later;
                }
                if (at >= i + 3) { from = i + 3; to = at; }
                else { i += 3; continue; }
            } else if (c == '=' || c == ':') {
                int nameEnd = i;
                while (nameEnd > copied && i - nameEnd < 8 && text.charAt(nameEnd - 1) == ' ') nameEnd--;
                int nameStart = nameEnd;
                while (nameStart > copied && nameEnd - nameStart < 128 && nameChar(text.charAt(nameStart - 1))) nameStart--;
                if (nameEnd - nameStart >= 2) {
                    String name = text.substring(nameStart, nameEnd);
                    if (known.computeIfAbsent(name, CapturedValue::credentialName)) {
                        int valueStart = i + 1;
                        while (valueStart < length && valueStart - i <= 8 && text.charAt(valueStart) == ' ') valueStart++;
                        from = valueStart;
                        if (listHeader(name)) {
                            to = from;
                            while (to < length && text.charAt(to) != '\n' && text.charAt(to) != '\r') to++;
                        } else to = valueEnd(text, valueStart);
                    }
                }
            } else if (c == '-' && text.startsWith("--", i) && (i == 0 || Character.isWhitespace(text.charAt(i - 1)))) {
                int end = i + 2;
                while (end < length && nameChar(text.charAt(end)) && end - i < 130) end++;
                if (end < length && (text.charAt(end) == ' ' || text.charAt(end) == '\t') && end - i > 2 && known.computeIfAbsent(text.substring(i, end), CapturedValue::credentialName)) {
                    int valueStart = end;
                    while (valueStart < length && (text.charAt(valueStart) == ' ' || text.charAt(valueStart) == '\t')) valueStart++;
                    int valueEnd = valueStart;
                    char q = valueStart < length ? text.charAt(valueStart) : 0;
                    if (q == '"' || q == '\'') {
                        // A quoted word may hold spaces: it runs to its closing quote.
                        valueEnd = valueStart + 1;
                        while (valueEnd < length && text.charAt(valueEnd) != q) valueEnd += text.charAt(valueEnd) == '\\' ? 2 : 1;
                        valueEnd = Math.min(length, valueEnd + 1);
                    } else while (valueEnd < length && !Character.isWhitespace(text.charAt(valueEnd))) valueEnd++;
                    if (valueEnd > valueStart && !(text.charAt(valueStart) == '-' && text.startsWith("--", valueStart))) { from = valueStart; to = valueEnd; }
                }
                if (from < 0) { i = end; continue; }
            }
            if (from >= copied && to >= from) {
                if (out == null) out = new StringBuilder(length);
                out.append(text, copied, from).append(replacement);
                copied = to; i = Math.max(to, i + 1);
                continue;
            }
            i++;
        }
        return out == null ? text : out.append(text, copied, length).toString();
    }

    private static int valueEnd(String text, int start) {
        int length = text.length();
        if (start >= length) return length;
        if (text.startsWith("-----BEGIN", start)) {
            int end = text.indexOf("-----END", start);
            if (end < 0) return length;
            int close = text.indexOf("-----", end + 8);
            return close < 0 ? length : close + 5;
        }
        int i = start;
        char first = text.charAt(start);
        if (first == '"' || first == '\'') {
            // A quoted value is read to its closing quote, then on to the next pair like any other: a quote inside it does not end it.
            i = start + 1;
            while (i < length && text.charAt(i) != first) i += text.charAt(i) == '\\' ? 2 : 1;
            if (i >= length) return length;
        }
        // Brackets that open inside the value close inside it; a closer with nothing open (the group around the pair, or part of the
        // value) does not end it. A value ends only at a pair boundary with nothing open, or at the end of the text.
        int depth = 0;
        for (i = Math.max(i, start) + (first == '"' || first == '\'' ? 1 : 0); i < length; i++) {
            char c = text.charAt(i);
            if (c == '(' || c == '[' || c == '{') { depth++; continue; }
            if (c == ')' || c == ']' || c == '}') { if (depth > 0) depth--; continue; }
            if (depth > 0) continue;
            boolean separator = c == ',' || c == ';' || c == '\n' || c == '\r';
            if (separator && NEXT_PAIR.matcher(text).region(i + 1, length).lookingAt()) return i;
            if (c == ' ' && NEXT_PAIR_TIGHT.matcher(text).region(i + 1, length).lookingAt()) return i;
        }
        return length;
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
