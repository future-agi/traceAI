package ai.traceai.auto;

import ai.traceai.TraceConfig;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.*;

/** Immutable resolved configuration. No startup network requests or credential logging. */
public final class AutoConfig {
    final Map<String, String> values;
    final TraceConfig masking;
    final double sampleRate;
    final int maxBodyBytes;
    // Even ignored/incomplete FI credentials remain private; never expose this list in diagnostics.
    final List<String> credentialValues;

    public static AutoConfig resolve(Map<String, ?> overrides) {
        return resolve(overrides, System.getenv());
    }
    static AutoConfig resolve(Map<String, ?> overrides, Map<String, String> environment) {
        return resolve(overrides, environment, Path.of(System.getProperty("user.dir", ".")));
    }
    static AutoConfig resolve(Map<String, ?> overrides, Map<String, String> environment, Path workingDirectory) {
        Map<String, String> result = new LinkedHashMap<>();
        String path = String.valueOf(overrides.containsKey("config_file") ? overrides.get("config_file") : overrides.containsKey("FI_CONFIG_FILE") ? overrides.get("FI_CONFIG_FILE") : environment.getOrDefault("FI_CONFIG_FILE", "futureagi.toml"));
        try {
            for (String line : Files.readAllLines(Path.of(path))) {
                line = stripComment(line).trim();
                if (line.isEmpty() || line.startsWith("[")) continue;
                String[] pair = line.split("=", 2); if (pair.length != 2) continue;
                String key = envKey(pair[0].trim()), value = pair[1].trim();
                if (value.startsWith("[")) {
                    Object array = WireCore.parse(value); if (!(array instanceof List)) continue;
                    value = String.join(",", WireCore.list(array).stream().map(String::valueOf).toList());
                } else if (value.startsWith("\"")) { Object parsed = WireCore.parse(value); if (!(parsed instanceof String)) continue; value = (String)parsed; }
                result.put(key, value);
            }
        } catch (Exception ignored) { }
        Map<String, String> file = new LinkedHashMap<>(result), explicit = new LinkedHashMap<>();
        environment.forEach((k, v) -> { if (k.startsWith("FI_") || k.equals("OTEL_SDK_DISABLED") || k.equals("OTEL_SERVICE_NAME") || k.equals("OTEL_RESOURCE_ATTRIBUTES") || k.equals("OTEL_SPAN_ATTRIBUTE_COUNT_LIMIT") || k.equals("OTEL_ATTRIBUTE_COUNT_LIMIT")) result.put(k, v); });
        overrides.forEach((k, v) -> {
            if (v == null) return;
            String key = envKey(k);
            boolean names = key.equals("FI_INSTRUMENTATIONS") || key.equals("FI_DISABLED_INSTRUMENTATIONS");
            explicit.put(key, names && v instanceof Collection ? String.join(",", ((Collection<?>)v).stream().map(String::valueOf).toList()) : v instanceof Map || v instanceof Collection ? WireCore.dump(v) : String.valueOf(v));
        });
        result.putAll(explicit);
        String profile = result.getOrDefault("FI_PROFILE", "default");
        List<Map<String, String>> profiles = fiProfiles(environment, profile, workingDirectory);
        Map<String, String> resolved = new LinkedHashMap<>();
        for (var source : profiles) resolved.putAll(source);
        resolved.putAll(result); result.clear(); result.putAll(resolved);
        result.remove("FI_API_KEY"); result.remove("FI_SECRET_KEY");
        List<String> credentialValues = new ArrayList<>();
        // Low-to-high source order: a partial source cannot overwrite either half of a complete pair.
        for (int i = 0; i < profiles.size(); i++) resolvePair(result, profiles.get(i), i == 0 ? "fi user profile" : "fi project profile", credentialValues);
        resolvePair(result, file, "config file", credentialValues);
        resolvePair(result, environment, "environment", credentialValues);
        resolvePair(result, explicit, "explicit arguments", credentialValues);
        result.putIfAbsent("FI_BASE_URL", "https://api.futureagi.com");
        String directory = "DEFAULT_PROJECT_NAME";
        try { Path name = Path.of(System.getProperty("user.dir", "")).toAbsolutePath().getFileName(); if (name != null && !name.toString().isBlank()) directory = name.toString(); } catch (RuntimeException ignored) { }
        String service = result.get("OTEL_SERVICE_NAME");
        if (!WireCore.truth(result.get("FI_PROJECT_NAME"))) result.put("FI_PROJECT_NAME", service != null && !service.isBlank() ? service : directory);
        result.putIfAbsent("FI_PROJECT_VERSION_NAME", "DEFAULT_PROJECT_VERSION_NAME");
        result.putIfAbsent("FI_EVAL_TAGS", "[]"); result.putIfAbsent("FI_METADATA", "{}");
        result.putIfAbsent("FI_PROJECT_TYPE", "observe"); result.putIfAbsent("FI_WIRE_CAPTURE", "true");
        result.putIfAbsent("FI_WIRE_MAX_BODY_BYTES", "4194304"); result.putIfAbsent("FI_SAMPLE_RATE", "1.0"); result.putIfAbsent("FI_AUTO_DEBUG", "false");
        result.put("FI_CONFIG_FILE", path);
        return new AutoConfig(result, credentialValues);
    }
    /** fi's named-profile files, kept as separate atomic sources even across project/user layers. */
    private static List<Map<String, String>> fiProfiles(Map<String, String> environment, String profile, Path directory) {
        Map<String, String> user = Map.of(), project = Map.of();
        try {
            String xdg = environment.get("XDG_CONFIG_HOME"), home = environment.getOrDefault("HOME", environment.get("USERPROFILE"));
            if (xdg != null && !xdg.isBlank()) user = readProfile(Path.of(xdg, "fi", "config.toml"), profile);
            else if (home != null && !home.isBlank()) user = readProfile(Path.of(home, ".config", "fi", "config.toml"), profile);
        } catch (RuntimeException ignored) { }
        try {
            for (Path parent = directory.toAbsolutePath().normalize(); parent != null; parent = parent.getParent()) {
                Path file = parent.resolve(".fi/config.toml");
                if (Files.isRegularFile(file)) { project = readProfile(file, profile); break; }
            }
        } catch (RuntimeException ignored) { }
        return List.of(user, project);
    }
    private static Map<String, String> readProfile(Path file, String profile) {
        Map<String, String> result = new LinkedHashMap<>();
        try (var in = Files.newInputStream(file)) {
            byte[] bytes = in.readNBytes(262145); if (bytes.length > 262144) return Map.of();
            boolean selected = false;
            for (String raw : new String(bytes, java.nio.charset.StandardCharsets.UTF_8).split("\\R")) {
                String line = stripComment(raw).trim();
                if (line.startsWith("[")) {
                    selected = false;
                    if (line.startsWith("[profiles.") && line.endsWith("]")) {
                        String name = line.substring(10, line.length() - 1).trim();
                        if (name.startsWith("\"")) { Object decoded = WireCore.parse(name); if (!(decoded instanceof String)) continue; name = (String)decoded; }
                        selected = name.equals(profile);
                    }
                    continue;
                }
                if (!selected || line.isEmpty()) continue;
                String[] pair = line.split("=", 2); if (pair.length != 2) continue;
                String key = pair[0].trim();
                if (!Set.of("api_key", "secret_key", "base_url", "project").contains(key)) continue;
                String value = pair[1].trim(); Object parsed = value.startsWith("\"") ? WireCore.parse(value) : value.startsWith("'") && value.endsWith("'") && value.length() >= 2 ? value.substring(1, value.length() - 1) : null;
                if (parsed instanceof String text) result.put(key.equals("project") ? "FI_PROJECT_NAME" : envKey(key), text);
            }
        } catch (Exception ignored) { return Map.of(); }
        return result;
    }
    private static void resolvePair(Map<String, String> result, Map<String, String> source, String name, List<String> credentials) {
        String key = source.get("FI_API_KEY"), secret = source.get("FI_SECRET_KEY");
        if (key != null) credentials.add(key); if (secret != null) credentials.add(secret);
        boolean hasKey = key != null && !key.isBlank(), hasSecret = secret != null && !secret.isBlank();
        if (hasKey && hasSecret) { result.put("FI_API_KEY", key); result.put("FI_SECRET_KEY", secret); }
        else if (hasKey || hasSecret) System.err.println("[traceai-auto] Ignoring incomplete credentials from " + name + "; missing " + (hasKey ? "FI_SECRET_KEY" : "FI_API_KEY"));
    }
    private AutoConfig(Map<String, String> resolved, List<String> credentials) {
        credentialValues = List.copyOf(credentials);
        Map<String, String> normalized = new LinkedHashMap<>(resolved);
        values = Collections.unmodifiableMap(normalized);
        double rate = 1; int limit = 4 * 1024 * 1024;
        try { rate = Double.parseDouble(get("FI_SAMPLE_RATE")); if (!Double.isFinite(rate) || rate < 0 || rate > 1) rate = 1; } catch (Exception ignored) { }
        try { limit = Math.max(0, Integer.parseInt(get("FI_WIRE_MAX_BODY_BYTES"))); } catch (Exception ignored) { }
        sampleRate = rate; maxBodyBytes = limit;
        normalized.put("FI_SAMPLE_RATE", Double.toString(rate)); normalized.put("FI_WIRE_MAX_BODY_BYTES", Integer.toString(limit));
        if (!Set.of("observe", "experiment").contains(get("FI_PROJECT_TYPE"))) normalized.put("FI_PROJECT_TYPE", "observe");
        masking = TraceConfig.builder().hideInputs(flag("FI_HIDE_INPUTS")).hideOutputs(flag("FI_HIDE_OUTPUTS"))
            .hideInputMessages(flag("FI_HIDE_INPUT_MESSAGES")).hideOutputMessages(flag("FI_HIDE_OUTPUT_MESSAGES")).build();
    }
    static String envKey(String key) { return key.startsWith("FI_") || key.startsWith("OTEL_") ? key : key.equals("debug") ? "FI_AUTO_DEBUG" : "FI_" + key.toUpperCase(Locale.ROOT); }
    static String stripComment(String line) {
        char quote = 0; boolean escaped = false;
        for (int i = 0; i < line.length(); i++) {
            char c = line.charAt(i);
            if (quote == '"') {
                if (escaped) escaped = false;
                else if (c == '\\') escaped = true;
                else if (c == '"') quote = 0;
            } else if (quote == '\'') {
                // TOML literal strings do not interpret backslashes or double quotes.
                if (c == '\'') quote = 0;
            } else if (c == '"' || c == '\'') quote = c;
            else if (c == '#') return line.substring(0, i);
        }
        return line;
    }
    String get(String key) { return values.get(key); }
    boolean flag(String key) { return "true".equalsIgnoreCase(get(key)); }
    boolean credentials() { return WireCore.truth(get("FI_API_KEY")) && WireCore.truth(get("FI_SECRET_KEY")); }
    boolean enabled(String name) {
        Set<String> allow = names(get("FI_INSTRUMENTATIONS")), deny = names(get("FI_DISABLED_INSTRUMENTATIONS"));
        return !flag("OTEL_SDK_DISABLED") && !deny.contains(name) && (allow.isEmpty() || allow.contains("all") || allow.contains(name));
    }
    static Set<String> names(String text) { return text == null || text.isBlank() ? Set.of() : new HashSet<>(Arrays.asList(text.toLowerCase(Locale.ROOT).replace(" ", "").split(","))); }
    Map<String, String> masked() {
        Map<String, String> copy = new LinkedHashMap<>(values);
        WirePrivacy privacy = new WirePrivacy(Map.of(), this);
        // Structural URL cleanup does not remove credentials echoed in the path.
        copy.replaceAll((k, v) -> PrivacyCatalog.INSTANCE.credentialName(k) ? "<masked>" : privacy.text(k.equals("FI_BASE_URL") ? safeUrl(v) : v));
        return copy;
    }
    private static String safeUrl(String value) { try { java.net.URI u = java.net.URI.create(value); return new java.net.URI(u.getScheme(), null, u.getHost(), u.getPort(), u.getPath(), null, null).toString(); } catch (Exception ignored) { return "<invalid URL>"; } }
}
