package ai.traceai.auto;

import java.nio.charset.StandardCharsets;
import java.io.ByteArrayOutputStream;
import java.util.*;
import java.util.regex.Pattern;

/** S6 recognition, loaded once from the shared catalog. No transport or application objects. */
final class PrivacyCatalog {
    static final PrivacyCatalog INSTANCE = new PrivacyCatalog();
    private final List<String> credentialPatterns;
    private final List<Exclusion> exclusions = new ArrayList<>();
    private final List<Pattern> contextualExclusions = new ArrayList<>();
    private final Set<String> signed, mimeNames, mimeExact, mimePayload, audioFields, audioPayload, audioTypes, metadata, base64Fields;
    private final List<String> mimePrefixes;
    private final List<List<MagicTest>> magic = new ArrayList<>();
    private final String alphabet, replacement, otherReplacement, dataReplacement, omissionFormat;
    private final int prefixBytes, threshold;
    final int maxDepth, maxNodes, maxText;
    private record Exclusion(Pattern regex, List<String> guards) {}
    private record MagicTest(int offset, byte[] expected, byte[] mask) {}
    record Hint(boolean media, boolean audio) {
        static final Hint NONE = new Hint(false, false);
        Hint merge(Hint other) { return new Hint(media || other.media, audio || other.audio); }
    }
    record Context(boolean media, boolean audio, boolean payload) {}
    record QueryPair(String name, String equals, String value, String decodedName, String decodedValue) {}
    // Small, anchored patterns only. URI scanning below is iterative, not recursive regex matching.
    private static final Pattern OMISSION = Pattern.compile("<omitted: (?:[0-9]+ base64 chars|media|data URI)>");

    private PrivacyCatalog() {
        Map<String, Object> catalog;
        try (var in = PrivacyCatalog.class.getResourceAsStream("privacy.json")) {
            catalog = WireCore.map(WireCore.parse(new String(Objects.requireNonNull(in).readAllBytes(), StandardCharsets.UTF_8)));
        } catch (Exception error) { throw new IllegalStateException("Cannot load privacy catalog", error); }
        var credentials = WireCore.map(catalog.get("credentials"));
        Map<String, String> patterns = new LinkedHashMap<>();
        for (Object rule : WireCore.list(credentials.get("patterns"))) patterns.put(WireCore.str(WireCore.get(rule, "id")), WireCore.str(WireCore.get(rule, "contains")));
        credentialPatterns = List.copyOf(patterns.values());
        for (Object rule : WireCore.list(credentials.get("exclusions"))) {
            List<String> guards = new ArrayList<>();
            for (Object id : WireCore.list(WireCore.get(rule, "unless_contains_patterns"))) guards.add(Objects.requireNonNull(patterns.get(id)));
            exclusions.add(new Exclusion(Pattern.compile(WireCore.str(WireCore.get(rule, "regex"))), List.copyOf(guards)));
        }
        for (Object rule : WireCore.list(credentials.get("contextual_exclusions"))) contextualExclusions.add(Pattern.compile(WireCore.str(WireCore.get(rule, "path_regex"))));
        replacement = WireCore.str(credentials.get("replacement"));
        signed = strings(WireCore.get(WireCore.get(catalog, "signed_url"), "parameters"), true);
        var media = WireCore.map(catalog.get("media"));
        alphabet = WireCore.str(media.get("base64_alphabet"));
        prefixBytes = ((Number)media.get("base64_prefix_decoded_bytes")).intValue();
        threshold = ((Number)media.get("unrecognized_base64_min_chars_exclusive")).intValue();
        omissionFormat = WireCore.str(media.get("replacement"));
        otherReplacement = WireCore.str(media.get("other_replacement"));
        dataReplacement = WireCore.str(WireCore.get(WireCore.get(media, "data_uri"), "other_replacement"));
        base64Fields = strings(media.get("unrecognized_base64_fields"), false);
        var mime = WireCore.map(media.get("mime_siblings"));
        mimeNames = strings(mime.get("names"), false); mimeExact = strings(mime.get("exact"), false);
        mimePayload = strings(mime.get("payload_fields"), false); mimePrefixes = new ArrayList<>(strings(mime.get("prefixes"), false));
        var audio = WireCore.map(media.get("audio"));
        audioFields = strings(audio.get("fields"), false); audioPayload = strings(audio.get("payload_fields"), false);
        audioTypes = strings(audio.get("type_names"), false); metadata = strings(audio.get("metadata_fields"), false);
        for (Object rule : WireCore.list(media.get("magic"))) {
            List<MagicTest> tests = new ArrayList<>();
            for (Object test : WireCore.list(WireCore.get(rule, "tests"))) {
                byte[] expected = HexFormat.of().parseHex(WireCore.str(WireCore.get(test, "hex")));
                Object mask = WireCore.get(test, "mask");
                tests.add(new MagicTest(((Number)WireCore.get(test, "offset")).intValue(), expected,
                    HexFormat.of().parseHex(mask == null ? "ff".repeat(expected.length) : (String)mask)));
            }
            magic.add(List.copyOf(tests));
        }
        var limits = WireCore.map(catalog.get("limits"));
        maxDepth = ((Number)limits.get("max_depth")).intValue(); maxNodes = ((Number)limits.get("max_nodes")).intValue(); maxText = ((Number)limits.get("max_text_chars")).intValue();
    }
    private static Set<String> strings(Object source, boolean normalize) {
        Set<String> result = new LinkedHashSet<>();
        for (Object item : WireCore.list(source)) result.add(normalize ? normalize((String)item) : (String)item);
        return Set.copyOf(result);
    }
    static String normalize(String name) {
        StringBuilder result = new StringBuilder(name.length());
        for (int i = 0; i < name.length(); i++) {
            char c = name.charAt(i);
            if (c == '-' || c == '_' || c == '.') continue;
            result.append(c >= 'A' && c <= 'Z' ? (char)(c + 32) : c);
        }
        return result.toString();
    }
    static List<String> path(String name, boolean indices) {
        if (name.length() > INSTANCE.maxText) throw new IllegalStateException("Privacy text budget");
        List<String> result = new ArrayList<>(); int components = 0;
        int start = 0;
        for (int i = 0; i <= name.length(); i++) if (i == name.length() || ".[]".indexOf(name.charAt(i)) >= 0) {
            if (i > start) {
                if (++components > INSTANCE.maxDepth) throw new IllegalStateException("Privacy path budget");
                String part = name.substring(start, i); if (indices || !numeric(part)) result.add(part);
            }
            start = i + 1;
        }
        return result;
    }
    private static boolean numeric(String text) {
        if (text.isEmpty()) return false;
        for (int i = 0; i < text.length(); i++) if (!Character.isDigit(text.charAt(i))) return false;
        return true;
    }
    static String leaf(String name) { var parts = path(name, false); return parts.isEmpty() ? "" : normalize(parts.get(parts.size() - 1)); }
    private boolean excluded(String folded) {
        for (Exclusion rule : exclusions) if (rule.regex.matcher(folded).matches() && rule.guards.stream().noneMatch(folded::contains)) return true;
        return false;
    }
    boolean credentialName(String name) { return credentialName(name, false); }
    boolean credentialName(String name, boolean environmentSeed) {
        // S5 initialization deliberately seeds all credential-marked environment names, including
        // names that S6 excludes as telemetry metrics/identifiers. Recognition still uses this
        // same catalog; no second environment vocabulary or request/export whitelist exists.
        if (environmentSeed) { String folded = normalize(name); return credentialPatterns.stream().anyMatch(folded::contains); }
        boolean excluded = false; StringBuilder context = new StringBuilder();
        for (String part : path(name, false)) {
            String folded = normalize(part);
            if (!context.isEmpty()) context.append('.'); context.append(folded);
            String current = context.toString();
            if (excluded(folded) || contextualExclusions.stream().anyMatch(rule -> rule.matcher(current).matches())) excluded = true;
            else if (credentialPatterns.stream().anyMatch(folded::contains)) return true;
        }
        if (excluded) return false;
        String folded = normalize(name);
        return !excluded(folded) && credentialPatterns.stream().anyMatch(folded::contains);
    }
    boolean queryName(String name) { return signed.contains(normalize(name)) || credentialName(name); }
    boolean base64(String value) {
        if (value.isEmpty()) return false;
        for (int i = 0; i < value.length(); i++) if (alphabet.indexOf(value.charAt(i)) < 0) return false;
        return true;
    }
    private boolean magic(String value) {
        StringBuilder prefix = new StringBuilder(); int count = (prefixBytes * 8 + 5) / 6;
        for (int i = 0; i < value.length() && prefix.length() < count; i++) {
            char c = value.charAt(i); if (c != '\r' && c != '\n') prefix.append(c == '-' ? '+' : c == '_' ? '/' : c);
        }
        // Truncation can retain just the first '=' of a padded final quantum (31 bytes
        // at the catalog's 32-byte prefix bound). Restore padding before strict decoding.
        while (prefix.length() % 4 != 0) prefix.append('=');
        byte[] decoded;
        try { decoded = Base64.getDecoder().decode(prefix.toString()); } catch (IllegalArgumentException ignored) { return false; }
        for (List<MagicTest> tests : magic) {
            boolean matched = true;
            for (MagicTest test : tests) {
                if (decoded.length < test.offset + test.expected.length) { matched = false; break; }
                for (int i = 0; i < test.expected.length; i++) if ((decoded[test.offset + i] & test.mask[i]) != (test.expected[i] & test.mask[i])) { matched = false; break; }
            }
            if (matched) return true;
        }
        return false;
    }
    Hint hint(String name, Object value) {
        if (value instanceof List<?> list) {
            if (list.size() > maxNodes) throw new IllegalStateException("Privacy structure budget");
            Hint result = Hint.NONE;
            for (Object item : list) if (item instanceof String) result = result.merge(hint(name, item));
            return result;
        }
        if (!(value instanceof String text)) return Hint.NONE;
        String leaf = leaf(name);
        if (!mimeNames.contains(leaf) && !leaf.equals("type")) return Hint.NONE;
        if (text.length() > maxText) throw new IllegalStateException("Privacy text budget");
        if (mimeNames.contains(leaf)) {
            String mime = text.split(";", 2)[0].trim().toLowerCase(Locale.ROOT);
            return new Hint(mimeExact.contains(mime) || mimePrefixes.stream().anyMatch(mime::startsWith), mime.startsWith("audio/"));
        }
        return new Hint(false, audioTypes.contains(normalize(text)));
    }
    static String hintParent(String name) {
        var parts = path(name, true);
        while (!parts.isEmpty() && numeric(parts.get(parts.size() - 1))) parts.remove(parts.size() - 1);
        return parts.isEmpty() ? "" : String.join(".", parts.subList(0, parts.size() - 1));
    }
    Map<String, Hint> indexHints(Map<String, Object> values, String parent, Map<String, Hint> inherited) {
        Map<String, Hint> result = new HashMap<>(inherited);
        values.forEach((name, value) -> addHint(result, parent.isEmpty() ? name : parent + "." + name, value));
        return result;
    }
    private void addHint(Map<String, Hint> hints, String name, Object value) {
        Hint hint = hint(name, value);
        if (hint.media || hint.audio) hints.merge(hintParent(name), hint, Hint::merge);
    }
    Context context(String name, boolean media, boolean audio, Map<String, Hint> hints) {
        Hint root = hints.getOrDefault("", Hint.NONE); media |= root.media; audio |= root.audio;
        boolean payload = false, direct = false; StringBuilder prefix = new StringBuilder();
        for (String part : path(name, true)) {
            if (!prefix.isEmpty()) prefix.append('.'); prefix.append(part);
            String folded = normalize(part);
            if (!numeric(part)) {
                if (metadata.contains(folded)) { media = false; audio = false; payload = false; }
                direct = audioFields.contains(folded); audio |= direct;
                if (media && mimePayload.contains(folded) || audio && audioPayload.contains(folded)) payload = true;
            }
            Hint hint = hints.getOrDefault(prefix.toString(), Hint.NONE); media |= hint.media; audio |= hint.audio;
        }
        return new Context(media, audio, payload || direct);
    }
    String omitted(int length) { return omissionFormat.replace("{length}", Integer.toString(length)); }
    String otherMedia() { return otherReplacement; }
    /** The one media predicate, shared by JSON, attributes, arrays and URL values. */
    String mediaString(String value, String path, boolean media, boolean audio, Map<String, Hint> hints, boolean framed) {
        if (value.equals(replacement) || OMISSION.matcher(value).matches()) return null;
        if (framed) { String data = dataUri(value); if (data != null) return data; }
        boolean encoded = base64(value);
        if (encoded && magic(value) || context(path, media, audio, hints).payload) return encoded ? omitted(value.length()) : otherReplacement;
        if (encoded && base64Fields.contains(leaf(path)) && value.length() > threshold) return omitted(value.length());
        return null;
    }
    String dataUri(String value) {
        if (!value.regionMatches(true, 0, "data:", 0, 5)) return null;
        int comma = value.indexOf(',', 5); if (comma < 0) return null;
        String payload = decode(value.substring(comma + 1), false);
        return value.substring(5, comma).toLowerCase(Locale.ROOT).endsWith(";base64") && (payload.isEmpty() || base64(payload)) ? omitted(payload.length()) : dataReplacement;
    }
    static String decode(String value, boolean plus) {
        if (value.indexOf('%') < 0) return plus ? value.replace('+', ' ') : value;
        // Decode valid escapes once even beside malformed escapes; UTF-8 errors use U+FFFD.
        // URLDecoder's all-or-nothing exception fallback would miss %74oken%ZZ.
        StringBuilder result = new StringBuilder(value.length()); var bytes = new ByteArrayOutputStream();
        for (int i = 0; i < value.length();) {
            char c = value.charAt(i);
            if (c == '%' && i + 2 < value.length() && hex(value.charAt(i + 1)) >= 0 && hex(value.charAt(i + 2)) >= 0) {
                bytes.reset();
                do { bytes.write(hex(value.charAt(i + 1)) * 16 + hex(value.charAt(i + 2))); i += 3; }
                while (i + 2 < value.length() && value.charAt(i) == '%' && hex(value.charAt(i + 1)) >= 0 && hex(value.charAt(i + 2)) >= 0);
                result.append(bytes.toString(StandardCharsets.UTF_8));
            } else { result.append(plus && c == '+' ? ' ' : c); i++; }
        }
        return result.toString();
    }
    private static int hex(char c) {
        return c >= '0' && c <= '9' ? c - '0' : c >= 'a' && c <= 'f' ? c - 'a' + 10 : c >= 'A' && c <= 'F' ? c - 'A' + 10 : -1;
    }
    static List<QueryPair> queryPairs(String query) {
        if (query.length() > INSTANCE.maxText) throw new IllegalStateException("Privacy text budget");
        List<QueryPair> result = new ArrayList<>();
        for (int start = 0; start <= query.length();) {
            if (result.size() >= INSTANCE.maxNodes) throw new IllegalStateException("Privacy query budget");
            int end = query.indexOf('&', start); if (end < 0) end = query.length();
            String pair = query.substring(start, end);
            int equal = pair.indexOf('='); String name = equal < 0 ? pair : pair.substring(0, equal), value = equal < 0 ? "" : pair.substring(equal + 1);
            result.add(new QueryPair(name, equal < 0 ? "" : "=", value, decode(name, true), decode(value, true)));
            start = end + 1;
        }
        return result;
    }
    private String urlValue(String original) {
        int hash = original.indexOf('#'); String fragment = hash < 0 ? "" : original.substring(hash + 1), target = hash < 0 ? original : original.substring(0, hash);
        int question = target.indexOf('?'); String base = question < 0 ? target : target.substring(0, question), query = question < 0 ? "" : target.substring(question + 1);
        int scheme = httpPrefix(base, 0) ? base.indexOf("://") + 3 : -1;
        if (scheme >= 0) {
            int at = base.indexOf('@', scheme), slash = base.indexOf('/', scheme), colon = base.indexOf(':', scheme);
            if (at >= 0 && colon >= scheme && colon < at && (slash < 0 || at < slash)) base = base.substring(0, scheme) + replacement + base.substring(at);
        }
        List<QueryPair> parsed = queryPairs(query); Map<String, Hint> hints = new HashMap<>();
        for (QueryPair pair : parsed) if (!pair.equals.isEmpty()) addHint(hints, pair.decodedName, pair.decodedValue);
        StringJoiner joined = new StringJoiner("&");
        for (QueryPair pair : parsed) {
            String value = pair.value;
            if (!pair.equals.isEmpty()) {
                String clean = queryName(pair.decodedName) ? replacement : mediaString(pair.decodedValue, pair.decodedName, false, false, hints, true);
                if (clean == null) { String data = dataText(pair.decodedValue); if (!data.equals(pair.decodedValue)) clean = data; }
                if (clean != null) value = clean;
            }
            joined.add(pair.name + pair.equals + value);
        }
        return dataText(base) + (question < 0 ? "" : "?" + joined) + (hash < 0 ? "" : "#" + dataText(fragment));
    }
    private static boolean httpPrefix(String text, int start) { return text.regionMatches(true, start, "http://", 0, 7) || text.regionMatches(true, start, "https://", 0, 8); }
    private static boolean asciiWhitespace(char c) { return c == ' ' || c >= '\t' && c <= '\r'; }
    private static boolean uriChar(char c) { return !asciiWhitespace(c) && c != '"' && c != '<' && c != '>' && c != '\\'; }
    private static int markerEnd(String text, int start) {
        if (text.startsWith("<redacted>", start)) return start + 10;
        if (text.startsWith("<omitted: ", start)) {
            for (int end = start + 10; end < Math.min(text.length(), start + 64); end++) if (text.charAt(end) == '>')
                return OMISSION.matcher(text.substring(start, end + 1)).matches() ? end + 1 : start;
        }
        return start;
    }
    private static int urlEnd(String text, int start) {
        int i = start;
        while (i < text.length()) {
            if (uriChar(text.charAt(i))) { i++; continue; }
            int end = markerEnd(text, i); if (end == i) break; i = end;
        }
        return i;
    }
    private boolean isUrlValue(String value) {
        if (!(httpPrefix(value, 0) || value.startsWith("/") || value.startsWith("?"))) return false;
        if (urlEnd(value, 0) == value.length()) return true;
        int hash = value.indexOf('#'); String target = hash < 0 ? value : value.substring(0, hash), fragment = hash < 0 ? "" : value.substring(hash);
        int question = target.indexOf('?'); if (question < 0) return false;
        StringJoiner joined = new StringJoiner("&");
        for (QueryPair pair : queryPairs(target.substring(question + 1))) joined.add(pair.name + pair.equals + (!pair.equals.isEmpty() && dataUri(pair.decodedValue) != null ? dataReplacement : pair.value));
        String candidate = target.substring(0, question + 1) + joined + fragment;
        return !candidate.equals(value) && urlEnd(candidate, 0) == candidate.length();
    }
    private static boolean dataStart(String text, int start) {
        if (!text.regionMatches(true, start, "data:", 0, 5)) return false;
        if (start == 0) return true;
        char c = text.charAt(start - 1);
        return !(c >= 'A' && c <= 'Z' || c >= 'a' && c <= 'z' || c >= '0' && c <= '9' || "+.-_:".indexOf(c) >= 0);
    }
    private int dataEnd(String text, int start) {
        int i = start + 5;
        while (i < text.length() && !asciiWhitespace(text.charAt(i)) && "\"'<>\\".indexOf(text.charAt(i)) < 0) i++;
        // CR/LF-wrapped payloads are part of the token only when followed by base64.
        while (i < text.length() && (text.charAt(i) == '\r' || text.charAt(i) == '\n')) {
            int end = i; while (end < text.length() && (text.charAt(end) == '\r' || text.charAt(end) == '\n')) end++;
            int payload = end;
            while (end < text.length() && alphabet.indexOf(text.charAt(end)) >= 0 && text.charAt(end) != '\r' && text.charAt(end) != '\n') end++;
            if (end == payload) break; i = end;
        }
        return i;
    }
    private String dataText(String text) { return scan(text, false); }
    String text(String value, boolean framed) { return framed && isUrlValue(value) ? urlValue(value) : scan(value, true); }
    private String scan(String value, boolean urls) {
        StringBuilder result = null; int copied = 0;
        for (int i = 0; i < value.length();) {
            boolean url = urls && (httpPrefix(value, i) || (i == 0 || asciiWhitespace(value.charAt(i - 1))) && (value.charAt(i) == '/' || value.charAt(i) == '?'));
            if (url) {
                int prefix = httpPrefix(value, i) ? value.indexOf("://", i) + 3 : i + 1;
                int end = urlEnd(value, prefix);
                if (end > prefix) {
                    boolean quote = i > 0 && value.charAt(i - 1) == '\'' && value.charAt(end - 1) == '\'';
                    String token = value.substring(i, quote ? end - 1 : end), clean = urlValue(token) + (quote ? "'" : "");
                    if (result == null) result = new StringBuilder();
                    result.append(value, copied, i).append(clean); copied = end; i = end; continue;
                }
            }
            if (dataStart(value, i)) {
                int end = dataEnd(value, i); String original = value.substring(i, end), clean = dataUri(original);
                if (clean != null) { if (result == null) result = new StringBuilder(); result.append(value, copied, i).append(clean); copied = end; }
                i = end; continue;
            }
            i++;
        }
        return result == null ? value : result.append(value, copied, value.length()).toString();
    }
}
