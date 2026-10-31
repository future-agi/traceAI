package ai.traceai.auto;

import java.util.*;

/** S1 bounded, data-only snapshots; all credential/media recognition belongs to the S6 catalog. */
final class WirePrivacy {
    private static final PrivacyCatalog CATALOG = PrivacyCatalog.INSTANCE;
    // Image hiding is an S1 capture control, separate from unconditional S6 raw-media recognition.
    private static final Set<String> IMAGE_FILES = Set.of("file", "filedata", "fileuri", "fileurl", "file.uri", "file.url");
    private static final Set<String> CONTAINERS = Set.of("java.util.ArrayList", "java.util.LinkedList", "java.util.HashMap", "java.util.LinkedHashMap", "java.util.TreeMap",
        "java.util.HashSet", "java.util.LinkedHashSet", "java.util.TreeSet", "java.util.Arrays$ArrayList", "java.util.Collections$CopiesList",
        "java.util.Collections$EmptyList", "java.util.Collections$EmptyMap", "java.util.Collections$EmptySet", "java.util.Collections$SingletonList", "java.util.Collections$SingletonMap", "java.util.Collections$SingletonSet");
    private final KnownSecrets.Snapshot secrets;
    private final boolean hideImages;
    private int nodes, characters;
    boolean failed;
    record Scope(Map<String, PrivacyCatalog.Hint> hints, Set<String> imageParents) {
        static final Scope EMPTY = new Scope(Map.of(), Set.of());
    }

    WirePrivacy(Map<String, String> headers, AutoConfig config) {
        KnownSecrets.observe(headers, null);
        if (config != null) config.credentialValues.forEach(KnownSecrets.PROCESS::register);
        secrets = KnownSecrets.PROCESS.snapshot();
        hideImages = config != null && config.flag("FI_HIDE_INPUT_IMAGES");
    }
    WirePrivacy(KnownSecrets.Snapshot secrets, boolean hideImages) { this.secrets = secrets; this.hideImages = hideImages; }
    String text(String value) { return value == null ? null : (String)clean(value); }
    Object clean(Object value) { return clean("", value); }
    Object clean(String key, Object value) { return clean(key, value, Scope.EMPTY, true); }
    synchronized Object clean(String key, Object value, Scope scope, boolean framed) {
        nodes = 0; characters = 0;
        try { return clean(value, key, 0, scope, false, false, framed); }
        catch (Throwable ignored) { failed = true; return "<redacted>"; }
    }
    static boolean credentialKey(String key) { return CATALOG.credentialName(key); }
    Scope scope(Map<String, Object> values, String parent, Scope inherited) {
        if (values.size() > CATALOG.maxNodes) throw new IllegalStateException("Privacy structure budget");
        // Hint inspection must not execute an arbitrary List implementation's getters.
        Map<String, Object> safe = new LinkedHashMap<>(); Set<String> images = new HashSet<>(inherited.imageParents);
        for (var entry : values.entrySet()) {
            Object value = entry.getValue(); String name = entry.getKey();
            String full = parent.isEmpty() ? name : parent + "." + name;
            if (value instanceof String || value instanceof List<?> && trustedContainer(value)) safe.put(name, value);
            if (hideImages && imageDiscriminator(PrivacyCatalog.leaf(name), value)) images.add(PrivacyCatalog.hintParent(full));
        }
        return new Scope(CATALOG.indexHints(safe, parent, inherited.hints), images);
    }
    private boolean hiddenImage(String key, Scope scope) {
        if (!hideImages) return false;
        if (imageKey(key)) return true;
        String canonical = String.join(".", PrivacyCatalog.path(key, true));
        for (String parent : scope.imageParents) if (parent.isEmpty() || canonical.equals(parent) || canonical.startsWith(parent + ".")) return true;
        return false;
    }
    private Object clean(Object value, String key, int depth, Scope scope, boolean media, boolean audio, boolean framed) {
        if (++nodes > CATALOG.maxNodes || depth > CATALOG.maxDepth) throw new IllegalStateException("Privacy structure budget");
        if (credentialKey(key) || hiddenImage(key, scope)) return "<redacted>";
        boolean jdk = value != null && trustedContainer(value);
        if (jdk && value instanceof Map<?, ?> map) {
            if (map.size() > CATALOG.maxNodes - nodes) throw new IllegalStateException("Privacy structure budget");
            Map<String, Object> values = new LinkedHashMap<>();
            for (var entry : map.entrySet()) {
                String name = entry.getKey() instanceof String s ? s : entry.getKey() == null ? "null" : entry.getKey().getClass().getName();
                characters += name.length(); if (characters > CATALOG.maxText) throw new IllegalStateException("Privacy text budget");
                values.put(name, entry.getValue());
            }
            if (hideImages && imageObject(values)) return "<redacted>";
            Scope local = scope(values, key, scope); Map<String, Object> result = new LinkedHashMap<>();
            for (var entry : values.entrySet()) {
                String name = entry.getKey(), child = key.isEmpty() ? name : key + "." + name;
                result.put(secrets.scrub(CATALOG.text(name, true)), clean(entry.getValue(), child, depth + 1, local, media, audio, true));
            }
            return result;
        }
        var context = CATALOG.context(key, media, audio, scope.hints);
        if (jdk && value instanceof Collection<?> list) {
            if (list.size() > CATALOG.maxNodes - nodes) throw new IllegalStateException("Privacy structure budget");
            if (context.payload() && !list.isEmpty() && list.stream().allMatch(item -> item instanceof Number || item instanceof Boolean)) return CATALOG.otherMedia();
            List<Object> result = new ArrayList<>(); int i = 0;
            for (Object item : list) result.add(clean(item, key + "." + i++, depth + 1, scope, media, audio, true));
            return result;
        }
        if (value != null && value.getClass().isArray()) {
            int length = java.lang.reflect.Array.getLength(value);
            if (length > CATALOG.maxNodes - nodes) throw new IllegalStateException("Privacy structure budget");
            List<Object> array = new ArrayList<>(length);
            for (int i = 0; i < length; i++) array.add(java.lang.reflect.Array.get(value, i));
            return clean(array, key, depth + 1, scope, media, audio, framed);
        }
        if (value == null) return null;
        if (value instanceof Boolean || value instanceof Long || value instanceof Integer || value instanceof Short || value instanceof Byte || value instanceof Double || value instanceof Float) return context.payload() ? CATALOG.otherMedia() : value;
        if (value instanceof String text) return string(text, key, depth, scope, media, audio, framed);
        return string(value.getClass().getName(), "", depth + 1, Scope.EMPTY, false, false, true);
    }
    private static boolean trustedContainer(Object value) {
        Class<?> type = value.getClass();
        if (type == com.google.gson.internal.LinkedTreeMap.class) return true;
        if (type.getClassLoader() != null) return false;
        String name = type.getName();
        return name.startsWith("java.util.ImmutableCollections$") || CONTAINERS.contains(name);
    }
    private String string(String text, String key, int depth, Scope scope, boolean media, boolean audio, boolean framed) {
        characters += text.length();
        if (characters > CATALOG.maxText) throw new IllegalStateException("Privacy text budget");
        String replacement = CATALOG.mediaString(text, key, media, audio, scope.hints, framed);
        if (replacement != null) return replacement;
        if (key.equals("gen_ai.request.parameters") || key.equals("llm.invocation_parameters")) {
            if (!boundedJson(text)) return "<redacted>";
            Object parsed = WireCore.parse(text);
            if (!(parsed instanceof Map)) return "<redacted>";
            text = WireCore.dump(WireCore.invocationParameters(WireCore.map(parsed)));
        }
        String trimmed = text.stripLeading();
        boolean structured = trimmed.startsWith("{") || trimmed.startsWith("[");
        if (structured) {
            if (!boundedJson(text)) return "<redacted>";
            Object json = WireCore.parse(text);
            if (json instanceof Map || json instanceof List) {
                var context = CATALOG.context(key, media, audio, scope.hints);
                Object scrubbed = clean(json, "", depth + 1, Scope.EMPTY, context.media(), context.audio(), true);
                String sanitized = WireCore.dump(scrubbed);
                if (!WireCore.dump(json).equals(sanitized)) text = sanitized;
            } else return "<redacted>";
        } else text = fields(embedded(text, depth));
        return secrets.scrub(CATALOG.text(text, framed));
    }
    private static boolean imageKey(String key) {
        String name = PrivacyCatalog.normalize(key);
        return name.contains("image") || IMAGE_FILES.contains(name)
            || name.endsWith("filedata") || name.endsWith("fileuri") || name.endsWith("fileurl");
    }
    private static boolean imageDiscriminator(String key, Object value) {
        if (value instanceof List<?> list && trustedContainer(value)) {
            if (list.size() > CATALOG.maxNodes) throw new IllegalStateException("Privacy structure budget");
            for (Object item : list) if (item instanceof String && imageDiscriminator(key, item)) return true;
            return false;
        }
        if (!(value instanceof String text)) return false;
        String name = PrivacyCatalog.normalize(key);
        if (name.equals("type")) {
            String type = PrivacyCatalog.normalize(text);
            return type.contains("image") || IMAGE_FILES.contains(type) || type.equals("inputfile");
        }
        return (name.equals("mimetype") || name.equals("mediatype") || name.equals("contenttype")) && text.regionMatches(true, 0, "image/", 0, 6);
    }
    private boolean imageObject(Map<String, Object> map) {
        for (var entry : map.entrySet()) if (imageDiscriminator(entry.getKey(), entry.getValue())) return true;
        return false;
    }
    /** S1 fallback for malformed/truncated diagnostic JSON. Every quoted token is visited once. */
    private String fields(String text) {
        StringBuilder result = null; int copied = 0;
        for (int i = 0; i < text.length();) {
            if (text.charAt(i) != '"') { i++; continue; }
            int start = i, end = quotedEnd(text, i); i = end;
            if (end == text.length() && text.charAt(end - 1) != '"') break;
            int colon = i; while (colon < text.length() && Character.isWhitespace(text.charAt(colon))) colon++;
            if (colon == text.length() || text.charAt(colon) != ':') { i = colon; continue; }
            Object decoded = WireCore.parse(text.substring(start, end)); String name = decoded instanceof String s ? s : "";
            int value = colon + 1; while (value < text.length() && Character.isWhitespace(text.charAt(value))) value++;
            if (value == text.length()) break;
            i = value; char first = text.charAt(value); int last = value;
            if (first == '{' || first == '[') {
                if (credentialKey(name) || hideImages && imageKey(name) || CATALOG.context(name, false, false, Map.of()).payload()) return "<redacted>";
                continue;
            }
            if (first == '"') last = quotedEnd(text, value);
            else while (last < text.length() && ",}]\r\n".indexOf(text.charAt(last)) < 0) last++;
            String replacement = null; Object content = WireCore.parse(text.substring(value, last));
            if (credentialKey(name) || hideImages && imageKey(name)) replacement = "<redacted>";
            else if (hideImages && imageDiscriminator(name, content)) return "<redacted>";
            else if (content instanceof String s) replacement = CATALOG.mediaString(s, name, false, false, Map.of(), true);
            else if (CATALOG.context(name, false, false, Map.of()).payload()) replacement = "<redacted>";
            if (replacement != null) {
                if (result == null) result = new StringBuilder();
                result.append(text, copied, value).append(WireCore.dump(replacement)); copied = last;
            }
            i = last;
        }
        return result == null ? text : result.append(text, copied, text.length()).toString();
    }
    private static int quotedEnd(String text, int start) {
        for (int i = start + 1; i < text.length(); i++) {
            char c = text.charAt(i);
            if (c == '\\') { if (i + 1 < text.length()) i++; }
            else if (c == '"') return i + 1;
        }
        return text.length();
    }
    private String embedded(String text, int depth) {
        StringBuilder result = new StringBuilder(); int copied = 0, start = -1, nesting = 0;
        boolean quoted = false, escape = false;
        for (int i = 0; i < text.length(); i++) {
            char c = text.charAt(i);
            if (start < 0) { if (c != '{' && c != '[') continue; start = i; nesting = 1; quoted = false; escape = false; continue; }
            if (quoted) { if (escape) escape = false; else if (c == '\\') escape = true; else if (c == '"') quoted = false; }
            else if (c == '"') quoted = true;
            else if ((c == '{' || c == '[') && ++nesting > CATALOG.maxDepth) return "<redacted>";
            else if ((c == '}' || c == ']') && --nesting == 0) {
                String fragment = text.substring(start, i + 1); Object json = WireCore.parse(fragment);
                if (json instanceof Map || json instanceof List) {
                    String sanitized = WireCore.dump(clean(json, "", depth + 1, Scope.EMPTY, false, false, true));
                    if (!WireCore.dump(json).equals(sanitized)) { result.append(text, copied, start).append(sanitized); copied = i + 1; }
                }
                start = -1;
            }
        }
        return result.append(text, copied, text.length()).toString();
    }
    private static boolean boundedJson(String text) {
        int depth = 0; boolean quoted = false, escape = false;
        for (int i = 0; i < text.length(); i++) {
            char c = text.charAt(i);
            if (quoted) { if (escape) escape = false; else if (c == '\\') escape = true; else if (c == '"') quoted = false; }
            else if (c == '"') quoted = true;
            else if ((c == '{' || c == '[') && ++depth > CATALOG.maxDepth) return false;
            else if (c == '}' || c == ']') depth--;
        }
        return true;
    }
}
