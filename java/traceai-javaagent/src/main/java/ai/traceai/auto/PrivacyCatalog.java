package ai.traceai.auto;

import java.nio.charset.StandardCharsets;
import java.io.ByteArrayOutputStream;
import java.util.*;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/** S6 recognition, loaded once from the shared catalog. No transport or application objects. */
final class PrivacyCatalog {
    private static final String DIGEST_PARAMETER = "[A-Za-z][A-Za-z0-9_-]{0,31}\\s{0,4}=\\s{0,4}(?:\"[^\"\\r\\n]{0,512}\"|[^\\s,\"]{0,512})";
    private static final List<String> AUTHORIZATION_SHAPES = List.of(
        "\\b(?:Negotiate|NTLM)\\s+[A-Za-z0-9+/_=-]{8,}",
        "\\bDigest\\s+" + DIGEST_PARAMETER + "(?:\\s{0,4},\\s{0,4}" + DIGEST_PARAMETER + "){0,31}");
    // Declared before INSTANCE: the constructor reads them.
    /**
     * The catalog's JWT shape, whose three runs have no upper bound. It is not matched by the regex engine: the classes exclude the dot, so
     * each run is fixed by where it starts, and {@link #replaceJwts} reads them directly, in time linear in the text. The regex
     * {@link #JWT_START} only finds a start (the engine's own word boundary before {@code eyJ}).
     */
    private static final String JWT_SHARED = "\\beyJ[A-Za-z0-9_-]{8,}\\.[A-Za-z0-9_-]{8,}\\.[A-Za-z0-9_-]{8,}";
    private static final Pattern JWT_START = pythonPattern("\\beyJ", 0);
    /** A catalog shape: {@code jwt} when it is the JWT shape, scanned by {@link #replaceJwts} instead of by the engine. */
    private record Shape(Pattern pattern, boolean jwt) {}
    private static boolean runCharacter(char c) { return c >= 'a' && c <= 'z' || c >= 'A' && c <= 'Z' || c >= '0' && c <= '9' || c == '_' || c == '-'; }
    static final PrivacyCatalog INSTANCE = new PrivacyCatalog();
    private final List<String> credentialPatterns;
    private final List<Exclusion> exclusions = new ArrayList<>();
    private final List<Pattern> contextualExclusions = new ArrayList<>();
    private final List<Shape> valueShapes = new ArrayList<>();
    /** The catalog's value shapes as read at a --name's value start (the leading word boundary dropped): a value after an encoded gap is
     *  read as after a plain one. Only the catalog's own shapes, as the shared reference reads them. */
    private final List<Pattern> wordShapes = new ArrayList<>();
    // Exact declared usage/byte counter keys; true when the catalog types them as nonnegative safe integers.
    private final Map<String, Boolean> scalarMetrics = new HashMap<>();
    private final Set<String> signed, mimeNames, mimeExact, mimePayload, audioFields, audioPayload, audioTypes, metadata, base64Fields;
    // Catalog v3 media.image and media.video: type and modality declarations.
    private final Set<String> imageTypes, imageModalities, imagePayload, videoTypes, videoModalities;
    private final List<String> mimePrefixes;
    private final List<List<MagicTest>> magic = new ArrayList<>();
    private final String alphabet, replacement, otherReplacement, dataReplacement, omissionFormat;
    private final int prefixBytes, threshold;
    final int maxDepth, maxNodes, maxText;
    private record Exclusion(Pattern regex, List<String> guards) {}
    private record MagicTest(int offset, byte[] expected, byte[] mask) {}
    /** image is extra image evidence (image type, modality or MIME): it only widens omission. */
    record Hint(boolean media, boolean audio, boolean image) {
        static final Hint NONE = new Hint(false, false, false);
        Hint(boolean media, boolean audio) { this(media, audio, false); }
        Hint merge(Hint other) { return new Hint(media || other.media, audio || other.audio, image || other.image); }
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
        for (Object rule : WireCore.list(credentials.get("value_shapes"))) {
            String flags = Objects.requireNonNullElse(WireCore.str(WireCore.get(rule, "flags")), "");
            if (!flags.replace("i", "").isEmpty()) throw new IllegalStateException("Unsupported value-shape flags: " + flags);
            String regex = WireCore.str(WireCore.get(rule, "regex"));
            int options = flags.contains("i") ? Pattern.CASE_INSENSITIVE | Pattern.UNICODE_CASE : 0;
            valueShapes.add(JWT_SHARED.equals(regex) && options == 0 ? new Shape(null, true) : new Shape(pythonPattern(regex, options), false));
            wordShapes.add(pythonPattern(regex.startsWith("\\b") ? regex.substring(2) : regex, options));
        }
        // Authorization schemes with no name in front of them, besides the catalog's Bearer and Basic: Negotiate and NTLM carry a token, Digest a
        // comma-separated list of name=value parameters.
        for (String shape : AUTHORIZATION_SHAPES) valueShapes.add(new Shape(pythonPattern(shape, Pattern.CASE_INSENSITIVE | Pattern.UNICODE_CASE), false));
        for (Object container : WireCore.list(WireCore.get(catalog.get("scalar_metrics"), "containers"))) {
            boolean typed = "nonnegative_safe_integer".equals(WireCore.get(container, "value_type"));
            for (Object key : WireCore.list(WireCore.get(container, "keys"))) scalarMetrics.merge((String)key, typed, Boolean::logicalOr);
        }
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
        var image = WireCore.map(media.get("image")); var video = WireCore.map(media.get("video"));
        imageTypes = strings(image.get("type_names"), false); imageModalities = strings(image.get("modality_names"), false);
        imagePayload = strings(image.get("payload_fields"), false);
        videoTypes = strings(video.get("type_names"), false); videoModalities = strings(video.get("modality_names"), false);
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
    /** Python's {@code \s} (every {@code str.isspace} character), as the contents of a character class. */
    private static final String PYTHON_SPACE = "\\t\\n\\x0B\\f\\r\\x1C-\\x20\\x85\\xA0\\u1680\\u2000-\\u200A\\u2028\\u2029\\u202F\\u205F\\u3000";
    /**
     * A catalog shape compiled to match as the shared reference's Python {@code re} reads it: {@code \s} is every {@code str.isspace}
     * character (Java's is ASCII only), {@code \S} outside a class its complement, and a case-insensitive shape folds case over
     * Unicode ({@code ı}, {@code İ}, {@code ſ} and the Kelvin sign fold to ASCII letters, as Python's {@code re.I} folds them).
     */
    /** Python's {@code \w}: a Unicode letter or number, or '_' (never a combining mark, a zero-width character or other connector
     *  punctuation); and its {@code \b}, written with it, so the engine's own word rules are never used. */
    private static final String PYTHON_WORD = "[\\p{L}\\p{N}_]";
    private static final String PYTHON_BOUNDARY = "(?:(?<=" + PYTHON_WORD + ")(?!" + PYTHON_WORD + ")|(?<!" + PYTHON_WORD + ")(?=" + PYTHON_WORD + "))";
    /** Python's {@code \b} at {@code at} in {@code regex}: before a word character it is "no word character before", after a class
     *  of word characters "no word character after" (one look instead of two, so the engine can still scan for the literal), and
     *  otherwise both looks. */
    private static String boundary(String regex, int at) {
        String rest = regex.substring(at + 2);
        if (rest.matches("(?s)(?:\\(\\?:)?[A-Za-z0-9_].*")) return "(?<!" + PYTHON_WORD + ")";
        if (regex.substring(0, at).matches("(?s).*\\[(?:[A-Za-z0-9_]|[A-Za-z0-9]-[A-Za-z0-9])+\\](?:\\{[0-9]+(?:,[0-9]*)?\\}|[+*])?")) return "(?!" + PYTHON_WORD + ")";
        return PYTHON_BOUNDARY;
    }
    static Pattern pythonPattern(String regex, int options) {
        StringBuilder out = new StringBuilder(regex.length() + 64);
        boolean inClass = false;
        for (int i = 0; i < regex.length(); i++) {
            char c = regex.charAt(i);
            if (c == '\\' && i + 1 < regex.length()) {
                char next = regex.charAt(++i);
                if (next == 's') out.append(inClass ? PYTHON_SPACE : "[" + PYTHON_SPACE + "]");
                else if (next == 'b' && !inClass) out.append(boundary(regex, i - 1));
                else if (next == 'S' && !inClass) out.append("[^").append(PYTHON_SPACE).append(']');
                else out.append(c).append(next);
                continue;
            }
            if (c == '[' && !inClass) inClass = true;
            else if (c == ']' && inClass) inClass = false;
            out.append(c);
        }
        return Pattern.compile(out.toString(), options);
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
    /** Names that are credentials as a whole or as a segment ({@code dbPass}, {@code db.pwd}, {@code Set-Cookie}), besides the catalog's patterns. Over-redaction (a name like {@code pin}) fails closed. */
    private static final Set<String> VOCABULARY = Set.of("pwd", "pass", "passphrase", "passwd", "passcode", "pin");
    private static boolean vocabulary(String name) {
        StringBuilder word = new StringBuilder();
        for (int i = 0; i <= name.length(); i++) {
            char c = i < name.length() ? name.charAt(i) : '-';
            boolean boundary = !Character.isLetterOrDigit(c) || i > 0 && Character.isUpperCase(c) && Character.isLowerCase(name.charAt(i - 1));
            if (boundary && word.length() > 0) { if (VOCABULARY.contains(word.toString())) return true; word.setLength(0); }
            if (Character.isLetterOrDigit(c)) word.append(Character.toLowerCase(c));
        }
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
            else if (credentialPatterns.stream().anyMatch(folded::contains) || vocabulary(part)) return true;
        }
        if (excluded) return false;
        String folded = normalize(name);
        return !excluded(folded) && (credentialPatterns.stream().anyMatch(folded::contains) || vocabulary(name));
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
        if (!mimeNames.contains(leaf) && !leaf.equals("type") && !leaf.equals("modality")) return Hint.NONE;
        if (text.length() > maxText) throw new IllegalStateException("Privacy text budget");
        String type = CredentialPairs.pyStrip(text.split(";", 2)[0]).toLowerCase(Locale.ROOT);
        if (mimeNames.contains(leaf)) return new Hint(mimeExact.contains(type) || mimePrefixes.stream().anyMatch(type::startsWith), type.startsWith("audio/"), type.startsWith("image/"));
        if (leaf.equals("modality")) {
            boolean image = imageModalities.contains(type);
            return new Hint(image || videoModalities.contains(type), false, image);
        }
        // Image and video types establish the generic media context; audio types establish audio context only.
        boolean image = imageTypes.contains(normalize(type));
        return new Hint(image || videoTypes.contains(normalize(type)), audioTypes.contains(normalize(text)), image);
    }
    /**
     * An encoded value under an image declaration (type, modality or MIME) whose path reaches a catalog
     * image payload field (image, image_url, url and the generic payload names). Audio metadata
     * components end the declaration unless they are image payload fields themselves (url), and a
     * fresh image hint below starts it again. Ordinary HTTP image URLs are not encoded, so they stay text.
     */
    private boolean declaredImagePayload(String path, Map<String, Hint> hints) {
        boolean image = hints.getOrDefault("", Hint.NONE).image, payload = false;
        StringBuilder prefix = new StringBuilder();
        for (String part : path(path, true)) {
            if (!prefix.isEmpty()) prefix.append('.'); prefix.append(part);
            if (!numeric(part)) {
                String folded = normalize(part); boolean field = imagePayload.contains(folded);
                if (metadata.contains(folded) && !field) { image = false; payload = false; }
                if (image && field) payload = true;
            }
            image |= hints.getOrDefault(prefix.toString(), Hint.NONE).image;
        }
        return payload;
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
    /** Credential value shapes apply to every exported string after registered secrets, independently of names. */
    String scrubValueShapes(String text) {
        for (Shape shape : valueShapes) {
            if (shape.jwt()) { text = replaceJwts(text); continue; }
            Matcher matcher = shape.pattern().matcher(text);
            if (matcher.find()) text = matcher.replaceAll(Matcher.quoteReplacement(replacement));
        }
        return text;
    }
    /**
     * The replacements {@code replaceAll} makes for the JWT shape. A start is an {@code eyJ} at a word boundary; its three runs are the maximal
     * runs of {@code [A-Za-z0-9_-]} (the dot is not in the class, so nothing is ever given back), each of at least eight characters after
     * {@code eyJ}, and the match ends at the end of the third run. Every start inside the same first run reads the same later runs, so when a
     * start fails the scan continues after its first run; after a match it continues after the match. Each run is read a bounded number of times.
     */
    private String replaceJwts(String text) {
        Matcher starts = JWT_START.matcher(text);
        StringBuilder out = null;
        int copied = 0, from = 0, length = text.length();
        while (from < length && starts.find(from)) {
            int start = starts.start();
            int first = runEnd(text, start + 3), second = -1, third = -1;
            if (first - (start + 3) >= 8 && first < length && text.charAt(first) == '.') {
                second = runEnd(text, first + 1);
                if (second - (first + 1) >= 8 && second < length && text.charAt(second) == '.') {
                    third = runEnd(text, second + 1);
                    if (third - (second + 1) < 8) third = -1;
                }
            }
            if (third > 0) {
                if (out == null) out = new StringBuilder(length);
                out.append(text, copied, start).append(replacement);
                copied = third;
                from = third;
            } else from = first;
        }
        return out == null ? text : out.append(text, copied, length).toString();
    }
    private static int runEnd(String text, int from) {
        int end = from, length = text.length();
        while (end < length && runCharacter(text.charAt(end))) end++;
        return end;
    }
    /**
     * A declared usage/byte counter (exact complete key) keeps its scalar role, although its spelling
     * contains an audio field. Inherited media/audio context, an explicit hint on the key or any ancestor,
     * arrays and strings keep the media policy; typed counters accept only nonnegative safe integers.
     */
    boolean scalarMetric(String name, Object value, boolean media, boolean audio, Map<String, Hint> hints) {
        Boolean typed = scalarMetrics.get(name);
        if (typed == null || media || audio || !(value instanceof Number || value instanceof Boolean)) return false;
        if (typed && !safeCount(value)) return false;
        Hint root = hints.get("");
        if (root != null && (root.media || root.audio)) return false;
        StringBuilder prefix = new StringBuilder();
        for (String part : path(name, true)) {
            if (!prefix.isEmpty()) prefix.append('.'); prefix.append(part);
            Hint hint = hints.get(prefix.toString());
            if (hint != null && (hint.media || hint.audio)) return false;
        }
        return true;
    }
    private static boolean safeCount(Object value) {
        if (value instanceof Long || value instanceof Integer || value instanceof Short || value instanceof Byte) {
            long count = ((Number)value).longValue();
            return count >= 0 && count <= 9007199254740991L;
        }
        if (value instanceof Double || value instanceof Float) {
            double count = ((Number)value).doubleValue();
            return count >= 0 && count <= 9007199254740991d && count == Math.rint(count);
        }
        return false;
    }
    /** The one media predicate, shared by JSON, attributes, arrays and URL values. */
    String mediaString(String value, String path, boolean media, boolean audio, Map<String, Hint> hints, boolean framed) {
        if (value.equals(replacement) || OMISSION.matcher(value).matches()) return null;
        if (framed) { String data = dataUri(value); if (data != null) return data; }
        boolean encoded = base64(value);
        if (encoded && magic(value) || context(path, media, audio, hints).payload) return encoded ? omitted(value.length()) : otherReplacement;
        if (encoded && declaredImagePayload(path, hints)) return omitted(value.length());
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
    /** The end of the longest catalog value shape that starts at {@code start} (the leading word boundary dropped), or {@code start}. */
    int wordShapeEnd(String text, int start) {
        int end = start;
        for (Pattern shape : wordShapes) {
            Matcher matcher = shape.matcher(text).region(start, text.length()).useTransparentBounds(true).useAnchoringBounds(false);
            if (matcher.lookingAt()) end = Math.max(end, matcher.end());
        }
        return end;
    }

    /** A URL value as the catalog writes it, with the spans it replaced, by position: {start, end, omission (1) or placeholder (0)}. */
    static final class Framed {
        final StringBuilder text = new StringBuilder();
        List<int[]> spans = new ArrayList<>();
        void keep(CharSequence part) { text.append(part); }
        void put(String part, boolean omission) { spans.add(new int[] {text.length(), text.length() + part.length(), omission ? 1 : 0}); text.append(part); }
    }

    /** Writes a part of a URL to {@code out}: each run {start, end} (sorted, disjoint; an empty run inserts the placeholder) becomes the
     *  placeholder, and each data URI in the original text its omission. Where a run and a data URI overlap, the run's placeholder is
     *  written and the data URI's omission covers the rest of it. */
    private void region(Framed out, String text, List<int[]> runs) {
        List<Object[]> data = new ArrayList<>();
        int length = text.length();
        for (int position = 0; position <= length; ) {
            int start = nextDataStart(text, position);
            if (start < 0) break;
            int end = dataUriEnd(text, start, dataMatchEnd(text, start));
            String replacement = dataUri(text.substring(start, end));
            if (replacement != null) data.add(new Object[] {start, end, replacement});
            position = end > start ? end : start + 1;
        }
        int position = 0, r = 0, d = 0; String last = null;
        while (true) {
            while (r < runs.size() && runs.get(r)[1] <= position && !(runs.get(r)[0] == runs.get(r)[1] && runs.get(r)[0] == position)) r++;
            if (r < runs.size() && runs.get(r)[0] == runs.get(r)[1] && runs.get(r)[0] == position) {
                out.put(replacement, false);
                last = "run" + r; r++;
                continue;
            }
            while (d < data.size() && (int)data.get(d)[1] <= position) d++;
            if (position >= length) break;
            if (r < runs.size() && runs.get(r)[0] <= position) {
                if (!("run" + r).equals(last)) { out.put(replacement, false); last = "run" + r; }
                position = runs.get(r)[1];
            } else if (d < data.size() && (int)data.get(d)[0] <= position) {
                int end = r < runs.size() ? Math.min((int)data.get(d)[1], runs.get(r)[0]) : (int)data.get(d)[1];
                if (!("data" + d).equals(last)) { out.put((String)data.get(d)[2], true); last = "data" + d; }
                position = end;
            } else {
                int end = length;
                if (r < runs.size()) end = Math.min(end, runs.get(r)[0]);
                if (d < data.size()) end = Math.min(end, (int)data.get(d)[0]);
                out.keep(text.substring(position, end));
                position = end; last = null;
            }
        }
    }

    /** The URL path pair rule: every '=' and %3D in each path segment; a credential name's value is replaced to the next pair of the
     *  segment or the segment end. The scheme and authority of an absolute URL are no path segments. */
    private void pathPairs(String base, Framed out) {
        int path = 0;
        if (httpPrefix(base, 0)) {
            path = base.indexOf('/', base.indexOf("//") + 2);
            if (path < 0) path = base.length();
        }
        List<int[]> runs = new ArrayList<>();
        int position = path;
        while (position < base.length()) {
            int end = base.indexOf('/', position + 1);
            if (end < 0) end = base.length();
            int first = base.startsWith("/", position) ? position + 1 : position;
            int k = first, name = first; boolean decided = false;
            while (k < end) {
                char c = base.charAt(k);
                if (c == ';' || c == ',' || c == '&') { k++; name = k; continue; }
                int size = c == '=' ? 1 : CredentialPairs.encodedEquals(base, k) ? 3 : 0;
                if (size == 0) { k++; continue; }
                boolean credential = k > name && credentialName(decode(base.substring(name, k), false));
                if (!decided && !credential && k > first) credential = credentialName(decode(base.substring(first, k), false));
                decided = true;
                if (credential) {
                    int valueEnd = pathValueEnd(base, k + size, end);
                    runs.add(new int[] {k + size, valueEnd});
                    k = name = valueEnd;
                } else k = name = k + size;
            }
            position = end;
        }
        region(out, base, runs);
    }

    private static boolean pathSeparator(char c) { return c == ';' || c == ',' || c == '&'; }
    /** The end of a path pair's value in base[start, end): the next ';', ',' or '&' that a non-empty name and '=' or %3D follow, or the end. */
    private static int pathValueEnd(String base, int start, int end) {
        int k = start;
        while (k < end) {
            if (!pathSeparator(base.charAt(k))) { k++; continue; }
            int j = k + 1;
            while (j < end && !pathSeparator(base.charAt(j)) && base.charAt(j) != '=' && !CredentialPairs.encodedEquals(base, j)) j++;
            if (j < end && j > k + 1 && !pathSeparator(base.charAt(j))) return k;
            k = j;
        }
        return end;
    }

    static final int QUESTION_CAP = 8;
    /** Whether a name after a '?' is a credential name: each name text[q + 1, end) for a mark q is read; a name longer than the name cap or
     *  one that holds more than eight '?' is not decided (true, fail closed). */
    private boolean afterQuestion(String text, List<Integer> marks, int end) {
        if (marks.isEmpty()) return false;
        if (end - marks.get(0) - 1 > CredentialPairs.NAME_CAP || marks.size() > QUESTION_CAP) return true;
        for (int q : marks) if (queryName(decode(text.substring(q + 1, end), true))) return true;
        return false;
    }
    /** Whether a raw query name, or the part of it after any '?', is a credential name. */
    private boolean questionName(String rawName) {
        if (queryName(decode(rawName, true))) return true;
        List<Integer> marks = new ArrayList<>();
        int at = rawName.indexOf('?');
        while (at >= 0 && marks.size() <= QUESTION_CAP) { marks.add(at); at = rawName.indexOf('?', at + 1); }
        return afterQuestion(rawName, marks, rawName.length());
    }
    /** Where a value is cut after a credential-named pair that follows a second '?', or -1. One forward pass. */
    private int secondQuestionCut(String rawValue) {
        int i = rawValue.indexOf('?');
        if (i < 0) return -1;
        List<Integer> marks = new ArrayList<>();
        for (int length = rawValue.length(); i < length; i++) {
            char c = rawValue.charAt(i);
            if (c == '?') { if (marks.size() <= QUESTION_CAP) marks.add(i); }
            else if (c == '=') {
                if (!marks.isEmpty() && afterQuestion(rawValue, marks, i)) return i + 1;
                marks = new ArrayList<>();
            }
        }
        return -1;
    }
    /** The part of a URL after its '#': split at '&' like a query; a credential-named pair's value is replaced to the next '&'. */
    private void tailPairs(String segment, Framed out) {
        List<int[]> runs = new ArrayList<>();
        int offset = 0;
        for (String piece : segment.split("&", -1)) {
            int equal = piece.indexOf('=');
            String rawName = equal < 0 ? piece : piece.substring(0, equal), rawValue = equal < 0 ? "" : piece.substring(equal + 1);
            int value = offset + rawName.length() + (equal < 0 ? 0 : 1), end = offset + piece.length();
            if (equal >= 0 && questionName(rawName)) runs.add(new int[] {value, end});
            else if (equal >= 0) {
                int cut = secondQuestionCut(rawValue);
                if (cut >= 0) runs.add(new int[] {value + cut, end});
            }
            offset = end + 1;
        }
        region(out, segment, runs);
    }
    /** The user information of an absolute URL, as {@code ^(https?://)[^\s/@]+:[^\s/@]+@} reads it: the index after its '@', or -1. */
    private static int userinfoEnd(String base) {
        if (!httpPrefix(base, 0)) return -1;
        int p = base.indexOf("://") + 3, q = p;
        while (q < base.length() && !CredentialPairs.pySpace(base.charAt(q)) && base.charAt(q) != '/' && base.charAt(q) != '@') q++;
        if (q >= base.length() || base.charAt(q) != '@') return -1;
        for (int c = p + 1; c < q - 1; c++) if (base.charAt(c) == ':') return q + 1;
        return -1;
    }

    /** One already-framed URL, scrubbed by the catalog: its text and the spans the catalog replaced in it, by position. */
    Framed framedUrl(String original) {
        Framed out = new Framed();
        int hash = original.indexOf('#');
        String target = hash < 0 ? original : original.substring(0, hash), fragment = hash < 0 ? "" : original.substring(hash + 1);
        int question = target.indexOf('?');
        String base = question < 0 ? target : target.substring(0, question), query = question < 0 ? "" : target.substring(question + 1);
        int userinfo = userinfoEnd(base);
        if (userinfo >= 0) {
            out.keep(base.substring(0, base.indexOf("://") + 3));
            out.put(replacement, false);
            out.keep("@");
            base = base.substring(userinfo);
        }
        pathPairs(base, out);
        if (question >= 0) out.keep("?");
        List<QueryPair> parsed = queryPairs(query); Map<String, Hint> hints = new HashMap<>();
        for (QueryPair pair : parsed) if (!pair.equals.isEmpty()) addHint(hints, pair.decodedName, pair.decodedValue);
        for (int index = 0; index < parsed.size(); index++) {
            QueryPair pair = parsed.get(index);
            if (index > 0) out.keep("&");
            out.keep(pair.name + pair.equals);
            if (pair.equals.isEmpty()) continue;
            if (queryName(pair.decodedName) || questionName(pair.name)) { out.put(replacement, false); continue; }
            String media = mediaString(pair.decodedValue, pair.decodedName, false, false, hints, true);
            if (media != null) { out.put(media, true); continue; }
            int cut = secondQuestionCut(pair.value);
            if (cut >= 0) { out.keep(pair.value.substring(0, cut)); out.put(replacement, false); }
            else if (!dataTextFramed(pair.decodedValue).equals(pair.decodedValue)) region(out, pair.decodedValue, List.of());
            else out.keep(pair.value);
        }
        if (hash >= 0) { out.keep("#"); tailPairs(fragment, out); }
        return out;
    }
    private String urlValue(String original) { return framedUrl(original).text.toString(); }
    private String dataTextFramed(String value) { String data = dataUri(value); return data != null ? data : dataText(value); }

    /** Replaces each run {start, end} (sorted, disjoint) of the framed text with the placeholder; a span a run overlaps is dropped, the
     *  others move. */
    void replaceRuns(Framed framed, List<int[]> runs) {
        if (runs.isEmpty()) return;
        String value = framed.text.toString();
        StringBuilder out = new StringBuilder(value.length());
        List<int[]> spans = framed.spans, moved = new ArrayList<>();
        int copied = 0, shift = 0, k = 0;
        for (int[] run : runs) {
            while (k < spans.size() && spans.get(k)[1] <= run[0]) { int[] s = spans.get(k++); moved.add(new int[] {s[0] + shift, s[1] + shift, s[2]}); }
            while (k < spans.size() && spans.get(k)[0] < run[1]) k++;
            out.append(value, copied, run[0]).append(replacement);
            copied = run[1];
            shift += replacement.length() - (run[1] - run[0]);
        }
        for (; k < spans.size(); k++) { int[] s = spans.get(k); moved.add(new int[] {s[0] + shift, s[1] + shift, s[2]}); }
        out.append(value, copied, value.length());
        framed.text.setLength(0); framed.text.append(out);
        framed.spans = moved;
    }
    /** The runs each value shape replaces, shape by shape, applied to the framed text in order (the spans move with them). */
    void replaceValueShapes(Framed framed) {
        for (Shape shape : valueShapes) {
            String text = framed.text.toString();
            List<int[]> runs = new ArrayList<>();
            if (shape.jwt()) jwtRuns(text, runs);
            else { Matcher matcher = shape.pattern().matcher(text); while (matcher.find()) runs.add(new int[] {matcher.start(), matcher.end()}); }
            replaceRuns(framed, runs);
        }
    }
    private static void jwtRuns(String text, List<int[]> runs) {
        Matcher starts = JWT_START.matcher(text);
        int from = 0, length = text.length();
        while (from < length && starts.find(from)) {
            int start = starts.start();
            int first = runEnd(text, start + 3), second, third = -1;
            if (first - (start + 3) >= 8 && first < length && text.charAt(first) == '.') {
                second = runEnd(text, first + 1);
                if (second - (first + 1) >= 8 && second < length && text.charAt(second) == '.') {
                    third = runEnd(text, second + 1);
                    if (third - (second + 1) < 8) third = -1;
                }
            }
            if (third > 0) { runs.add(new int[] {start, third}); from = third; } else from = first;
        }
    }
    private static boolean httpPrefix(String text, int start) { return text.regionMatches(true, start, "http://", 0, 7) || text.regionMatches(true, start, "https://", 0, 8); }
    /** A URI token ends at whitespace as the shared reference's URL pattern reads it (Python's {@code \s}: every {@code str.isspace}
     *  character, the Unicode spaces and separators included), or at a quote, an angle bracket or a backslash. */
    private static boolean uriChar(char c) { return !CredentialPairs.pySpace(c) && c != '"' && c != '<' && c != '>' && c != '\\'; }
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
    /** Whether a framed value is a whole URL, which {@link #text} reads by its own query rules. */
    boolean wholeUrl(String value) { return isUrlValue(value); }
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
    private static boolean dataLineChar(char c) { return c >= 'A' && c <= 'Z' || c >= 'a' && c <= 'z' || c >= '0' && c <= '9' || c == '+' || c == '/' || c == '_' || c == '=' || c == '-'; }
    /** The first data URI start at or after {@code from}, or -1. */
    private static int nextDataStart(String text, int from) {
        for (int i = text.indexOf(':', from); i >= 0; i = text.indexOf(':', i + 1)) if (i - 4 >= from && dataStart(text, i - 4)) return i - 4;
        return -1;
    }
    /** The end of the match of {@code data:[^\s"'<>\\]*(?:[\r\n]+[A-Za-z0-9+/_=\-]+)*} at {@code start} (\s as Python reads it). */
    private static int dataMatchEnd(String text, int start) {
        int i = start + 5, length = text.length();
        while (i < length && !CredentialPairs.pySpace(text.charAt(i)) && "\"'<>\\".indexOf(text.charAt(i)) < 0) i++;
        while (i < length) {
            int j = i;
            while (j < length && (text.charAt(j) == '\r' || text.charAt(j) == '\n')) j++;
            if (j == i) break;
            int k = j;
            while (k < length && dataLineChar(text.charAt(k))) k++;
            if (k == j) break;
            i = k;
        }
        return i;
    }
    private static final String BASE64_INDEX = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    private static int base64Index(char c) { return c == '-' ? 62 : c == '_' ? 63 : BASE64_INDEX.indexOf(c); }
    private static boolean base64Character(char c) { return c != '=' && dataLineChar(c); }
    /** Whether {@code pad} '=' after {@code body} are trailing base64 padding. */
    private static boolean padding(String body, int pad, boolean base64, boolean padded, long count) {
        if (!base64 || padded || pad > 2 || body.isEmpty() || body.startsWith("-") || (count + body.length() + pad) % 4 != 0) return false;
        return base64Index(body.charAt(body.length() - 1)) % (pad == 1 ? 4 : 16) == 0;
    }
    /** The end of the data URI that the data match read at text[start, end): a base64 continuation line belongs to it only if its '='
     *  characters are trailing base64 padding; a line followed by ':' or %3D is a pair name, never base64. */
    private static int dataUriEnd(String text, int start, int end) {
        int head = start + 5;
        while (head < end && text.charAt(head) != '\r' && text.charAt(head) != '\n') head++;
        String header = text.substring(start + 5, head);
        int comma = header.indexOf(',');
        boolean base64 = comma >= 0 && header.substring(0, comma).toLowerCase(Locale.ROOT).endsWith(";base64");
        String decoded = decode(comma < 0 ? "" : header.substring(comma + 1), false);
        long count = 0;
        for (int k = 0; k < decoded.length(); k++) if (base64Character(decoded.charAt(k))) count++;
        boolean padded = decoded.indexOf('=') >= 0;
        int position = head, length = text.length();
        while (position < end) {
            int j = position;
            while (j < end && (text.charAt(j) == '\r' || text.charAt(j) == '\n')) j++;
            if (j == position) break;
            int after = j;
            while (after < end && dataLineChar(text.charAt(after))) after++;
            if (after == j) break;
            int gap = after;
            while (gap < length && (CredentialPairs.dashesSpace(text.charAt(gap)) || CredentialPairs.encodedSpaceAhead(text, gap) != 0)) {
                int width = CredentialPairs.encodedSpaceAhead(text, gap); gap += width != 0 ? width : 1;
            }
            if (gap < length && (text.charAt(gap) == '=' || text.charAt(gap) == ':' || CredentialPairs.encodedEquals(text, gap))) break;
            // The '=' run that ends the line is read back once (linear in the line).
            int bodyEnd = after;
            while (bodyEnd > j && text.charAt(bodyEnd - 1) == '=') bodyEnd--;
            String body = text.substring(j, bodyEnd);
            int pad = after - bodyEnd;
            if (body.startsWith("--") || body.indexOf('=') >= 0 || pad > 0 && !padding(body, pad, base64, padded, count)) break;
            count += body.length(); padded = padded || pad > 0; position = after;
        }
        return position;
    }
    private String dataText(String text) { return scan(text, false); }
    String text(String value, boolean framed) { return framed && isUrlValue(value) ? urlValue(value) : scan(value, true); }
    private String scan(String value, boolean urls) {
        StringBuilder result = null; int copied = 0;
        for (int i = 0; i < value.length();) {
            boolean url = urls && (httpPrefix(value, i) || (i == 0 || CredentialPairs.pySpace(value.charAt(i - 1))) && (value.charAt(i) == '/' || value.charAt(i) == '?'));
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
                int end = dataUriEnd(value, i, dataMatchEnd(value, i)); String original = value.substring(i, end), clean = dataUri(original);
                if (clean != null) { if (result == null) result = new StringBuilder(); result.append(value, copied, i).append(clean); copied = end; }
                i = end; continue;
            }
            i++;
        }
        return result == null ? value : result.append(value, copied, value.length()).toString();
    }
}
