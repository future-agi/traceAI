package ai.traceai.auto;

import java.util.*;

/** Process-wide registry. Writers touch LRU order; exporters read one immutable snapshot. */
final class KnownSecrets {
    static final KnownSecrets PROCESS = new KnownSecrets();
    private static final int MAX_VALUES = 1024, MAX_CHARACTERS = 4 * 1024 * 1024;
    private final LinkedHashMap<String, Boolean> values = new LinkedHashMap<>(16, .75f, true);
    private int characters;
    private boolean unavailable;
    private volatile Snapshot cached = new Snapshot(List.of(), false);

    synchronized void register(String value) {
        if (value == null || value.length() < 8) return;
        if (value.length() > MAX_CHARACTERS) { unavailable = true; cached = null; return; }
        if (values.put(value, Boolean.TRUE) != null) return; // Refresh observation recency, not export recency.
        characters += value.length();
        while (values.size() > MAX_VALUES || characters > MAX_CHARACTERS) {
            var iterator = values.keySet().iterator(); String oldest = iterator.next();
            characters -= oldest.length(); iterator.remove();
        }
        cached = null;
    }
    synchronized void failClosed() { unavailable = true; cached = null; }
    Snapshot snapshot() {
        Snapshot snapshot = cached;
        return snapshot == null ? compile() : snapshot;
    }
    private synchronized Snapshot compile() {
        if (cached == null) cached = new Snapshot(values.keySet().stream().sorted(Comparator.comparingInt(String::length).reversed()).map(Secret::new).toList(), unavailable);
        return cached;
    }
    void seed(AutoConfig config, Map<String, String> environment) {
        environment.forEach((name, value) -> { if (environmentCredential(name)) register(value); });
        if (config != null) config.credentialValues.forEach(this::register);
    }
    static boolean environmentCredential(String name) { return PrivacyCatalog.INSTANCE.credentialName(name, true); }
    static void observe(Map<String, String> headers, String url) {
        PROCESS.observeRequest(headers, url);
    }
    void observeRequest(Map<String, String> headers, String url) {
        try { observeChecked(headers, url); } catch (Throwable ignored) { failClosed(); }
    }
    private void observeChecked(Map<String, String> headers, String url) {
        headers.forEach((name, value) -> {
            if (!PrivacyCatalog.INSTANCE.credentialName(name)) return;
            if (value == null) return;
            int start = 0;
            for (int i = 0; i <= value.length(); i++) if (i == value.length() || value.charAt(i) == '\n') {
                String entry = value.substring(start, i).trim(); start = i + 1;
                register(entry);
                if (PrivacyCatalog.leaf(name).endsWith("authorization")) {
                    int scheme = 0; while (scheme < entry.length() && !Character.isWhitespace(entry.charAt(scheme))) scheme++;
                    if (scheme < entry.length()) register(entry.substring(scheme).stripLeading());
                }
            }
        });
        if (url == null) return;
        int hash = url.indexOf('#'); String target = hash < 0 ? url : url.substring(0, hash);
        int question = target.indexOf('?'); if (question < 0) return;
        for (var pair : PrivacyCatalog.queryPairs(target.substring(question + 1)))
            if (!pair.equals().isEmpty() && PrivacyCatalog.INSTANCE.queryName(pair.decodedName())) register(pair.decodedValue());
    }
    void observeBody(Object value) {
        try { observeBody(value, "", 0, new int[]{8192}); } catch (Throwable ignored) { failClosed(); }
    }
    private void observeBody(Object value, String path, int depth, int[] remaining) {
        // An unseen suffix may contain a credential echoed by any later span. Budget
        // exhaustion is a privacy failure, not a successful partial observation.
        if (depth > 32 || --remaining[0] < 0) throw new IllegalStateException("Credential observation budget exceeded");
        // Called only on the agent's parsed JSON/multipart data, never SDK/application objects.
        if (value instanceof Map<?, ?> map) for (var entry : map.entrySet()) {
            if (entry.getKey() instanceof String name) observeBody(entry.getValue(), path.isEmpty() ? name : path + "." + name, depth + 1, remaining);
        } else if (value instanceof List<?> list) for (Object item : list) {
            observeBody(item, path, depth + 1, remaining);
        } else if (value instanceof String text && PrivacyCatalog.INSTANCE.credentialName(path)) {
            register(text);
            if (PrivacyCatalog.leaf(path).endsWith("authorization")) {
                int scheme = 0; while (scheme < text.length() && !Character.isWhitespace(text.charAt(scheme))) scheme++;
                if (scheme < text.length()) register(text.substring(scheme).stripLeading());
            }
        }
    }
    record Snapshot(List<Secret> secrets, boolean unavailable) {
        String scrub(String text) {
            if (unavailable) throw new IllegalStateException("Privacy registry unavailable");
            if ((long)text.length() * (secrets.size() + 1) > 16 * 1024 * 1024) return "<redacted>";
            for (Secret secret : secrets) text = secret.replace(text);
            return text;
        }
    }
    /** KMP keeps replacement linear even for long, repeated-prefix credentials. */
    private static final class Secret {
        final String value;
        final int[] prefix;
        Secret(String value) {
            this.value = value; prefix = new int[value.length()];
            for (int i = 1, j = 0; i < value.length(); i++) {
                while (j > 0 && value.charAt(i) != value.charAt(j)) j = prefix[j - 1];
                if (value.charAt(i) == value.charAt(j)) j++;
                prefix[i] = j;
            }
        }
        String replace(String text) {
            StringBuilder result = null; int copied = 0;
            for (int i = 0, j = 0; i < text.length(); i++) {
                // Redaction markers are boundaries, not new credential-bearing text.
                if (text.startsWith("<redacted>", i)) { j = 0; i += "<redacted>".length() - 1; continue; }
                while (j > 0 && text.charAt(i) != value.charAt(j)) j = prefix[j - 1];
                if (text.charAt(i) == value.charAt(j)) j++;
                if (j != value.length()) continue;
                if (result == null) result = new StringBuilder();
                result.append(text, copied, i + 1 - j).append("<redacted>"); copied = i + 1; j = 0;
            }
            return result == null ? text : result.append(text, copied, text.length()).toString();
        }
    }
}
