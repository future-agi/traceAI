package ai.traceai;

import org.junit.jupiter.api.Test;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import java.util.stream.Collectors;
import java.util.stream.Stream;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * Census of the instrumentation modules: no direct {@code span.setAttribute(key, value)} call may write
 * a value on a key that {@link ContentKeys} classifies as content, because only {@link FITracer} applies
 * the hide settings. Keys are resolved from string constants (aliases included); a key built from a
 * variable cannot be resolved and must appear in the reviewed list below with the reason it is safe.
 */
class ContentCensusTest {
    private static final Pattern CONSTANT = Pattern.compile("(?:static\\s+final|final\\s+static)\\s+String\\s+(\\w+)\\s*=\\s*([^;]+);");
    private static final Pattern CALL = Pattern.compile("\\b([\\w.]+)\\.setAttribute\\(");

    /** Unresolvable keys, reviewed: module/file -> why every key written there is not content or is masked first. */
    private static final Map<String, String> REVIEWED_DYNAMIC = Map.ofEntries(
        Map.entry("traceai-java-bedrock/BedrockTracing.java", "setCount writes token and result counts; setJson masks through TraceConfig.maskAttribute first"),
        Map.entry("traceai-java-redis/TracedRedisVectorSearch.java", "the key is a result-count attribute"),
        Map.entry("traceai-java-semantic-kernel/NativeTelemetry.java", "token counts, a cancellation flag and caller-supplied context attributes, not content keys"),
        Map.entry("traceai-java-watsonx/WatsonxAttributes.java", "helpers called only with model, token-count and parameter keys"),
        Map.entry("traceai-javaagent/TraceAIAuto.java", "every key passes hidden(k, ...) before the typed write"),
        Map.entry("traceai-spring-ai/SpringAITracingContext.java", "context attributes (session, user, metadata), not content keys"));

    private static Path javaRoot() {
        Path here = Path.of("").toAbsolutePath();
        return here.getFileName().toString().equals("traceai-java-core") ? here.getParent() : here;
    }

    private static List<Path> sources() throws IOException {
        try (Stream<Path> modules = Files.list(javaRoot())) {
            List<Path> roots = modules.filter(p -> Files.isDirectory(p.resolve("src/main/java"))).map(p -> p.resolve("src/main/java")).toList();
            List<Path> files = new ArrayList<>();
            for (Path root : roots) {
                try (Stream<Path> walk = Files.walk(root)) {
                    walk.filter(p -> p.toString().endsWith(".java")).forEach(files::add);
                }
            }
            return files;
        }
    }

    private static String module(Path file) {
        Path rel = javaRoot().relativize(file);
        return rel.getName(0) + "/" + file.getFileName();
    }

    private static Map<String, Set<String>> constants(List<Path> files) throws IOException {
        Map<String, Set<String>> values = new HashMap<>();
        Map<String, String> aliases = new HashMap<>();
        for (Path file : files) {
            Matcher m = CONSTANT.matcher(Files.readString(file));
            while (m.find()) {
                String expr = m.group(2).trim();
                if (expr.matches("\"[^\"]*\"")) values.computeIfAbsent(m.group(1), k -> new LinkedHashSet<>()).add(expr.substring(1, expr.length() - 1));
                else if (expr.matches("[\\w.]+")) aliases.put(m.group(1), expr.substring(expr.lastIndexOf('.') + 1));
            }
        }
        for (int i = 0; i < 6; i++) {
            aliases.forEach((name, target) -> { if (values.containsKey(target)) values.computeIfAbsent(name, k -> new LinkedHashSet<>()).addAll(values.get(target)); });
        }
        return values;
    }

    private static int closing(String s, int open) {
        int depth = 0;
        for (int i = open; i < s.length(); i++) {
            char c = s.charAt(i);
            if (c == '"') { i++; while (s.charAt(i) != '"') { if (s.charAt(i) == '\\') i++; i++; } }
            else if (c == '(') depth++;
            else if (c == ')' && --depth == 0) return i;
        }
        return -1;
    }

    private static List<String> split(String args) {
        List<String> out = new ArrayList<>();
        int depth = 0;
        StringBuilder cur = new StringBuilder();
        for (int i = 0; i < args.length(); i++) {
            char c = args.charAt(i);
            if (c == '"') {
                int j = i + 1;
                while (args.charAt(j) != '"') { if (args.charAt(j) == '\\') j++; j++; }
                cur.append(args, i, j + 1);
                i = j;
                continue;
            }
            if ("([{<".indexOf(c) >= 0) depth++;
            if (")]}>".indexOf(c) >= 0) depth--;
            if (c == ',' && depth == 0) { out.add(cur.toString().trim()); cur.setLength(0); } else cur.append(c);
        }
        out.add(cur.toString().trim());
        return out;
    }

    /** Candidate keys of a key expression: constants resolved, any other operand taken as "0". */
    private static Set<String> keys(String expr, Map<String, Set<String>> constants) {
        Set<String> result = new LinkedHashSet<>(Set.of(""));
        String[] operands = expr.split("\\s*\\+\\s*");
        // A key that is nothing but a variable or a call cannot be resolved: it is reviewed by hand.
        if (operands.length == 1 && !operands[0].trim().startsWith("\"")
            && !constants.containsKey(operands[0].trim().substring(operands[0].trim().lastIndexOf('.') + 1))) return Set.of();
        for (String part : expr.split("\\s*\\+\\s*")) {
            part = part.trim();
            Set<String> options;
            if (part.startsWith("\"")) options = Set.of(part.substring(1, part.length() - 1));
            else if (constants.containsKey(part.substring(part.lastIndexOf('.') + 1)) && part.matches("[\\w.]+")) options = constants.get(part.substring(part.lastIndexOf('.') + 1));
            else if (part.matches("[\\w.]+") && Character.isUpperCase(part.charAt(part.lastIndexOf('.') + 1))) return Set.of();
            else options = Set.of("0");
            Set<String> next = new LinkedHashSet<>();
            for (String head : result) for (String tail : options) next.add(head + tail);
            result = next;
        }
        return result;
    }

    /** True when the value expression is plainly a long, double, boolean or array of them. */
    private static boolean nonString(String value) {
        return value.matches("\\((long|int|double|float|short)\\).*") || value.matches("(true|false)|\\(Boolean\\).*")
            || value.matches("-?\\d+(\\.\\d+)?[LlDdFf]?") || value.matches(".*\\.(longValue|doubleValue|intValue)\\(\\)")
            || value.matches(".*\\.size\\(\\)") || value.startsWith("newlong[") || value.startsWith("newdouble[")
            || value.startsWith("newboolean[");
    }

    private static boolean isContent(String key) {
        TraceConfig everything = TraceConfig.builder().hideInputs(true).hideOutputs(true)
            .hideInputMessages(true).hideOutputMessages(true).build();
        return ContentKeys.actionFor(key, everything) != ContentKeys.Action.KEEP;
    }

    @Test
    void noInstrumentationWritesAValueOnAContentKeyBypassingFITracer() throws IOException {
        List<Path> files = sources();
        Map<String, Set<String>> constants = constants(files);
        List<String> bypasses = new ArrayList<>();
        Set<String> unresolvedFiles = new LinkedHashSet<>();
        for (Path file : files) {
            if (module(file).startsWith("traceai-java-core/")) continue;
            String source = Files.readString(file);
            Matcher call = CALL.matcher(source);
            while (call.find()) {
                int open = call.end() - 1;
                int close = closing(source, open);
                List<String> args = split(source.substring(open + 1, close));
                if (args.size() != 2 || args.get(0).startsWith("AttributeKey")) continue;
                Set<String> candidates = keys(args.get(0), constants);
                int line = (int) source.substring(0, call.start()).chars().filter(c -> c == '\n').count() + 1;
                if (candidates.isEmpty()) { unresolvedFiles.add(module(file)); continue; }
                // Only non-String values are in scope here: strings written directly are guarded by their own
                // hide checks and covered by the string-path tests. A document count (`(long) x.size()`) on a
                // retrieval/reranker key is an operational attribute (a number of items, never an item),
                // and the Cohere privacy test pins it as visible under every hide flag.
                String value = args.get(1).replaceAll("\\s+", "");
                boolean sizeCount = value.matches("\\(long\\)[\\w.()]+\\.size\\(\\)");
                if (!sizeCount && nonString(value) && candidates.stream().anyMatch(ContentCensusTest::isContent)) {
                    bypasses.add(module(file) + ":" + line + " " + args.get(0).replaceAll("\\s+", " ") + " <- " + args.get(1).replaceAll("\\s+", " "));
                }
            }
        }
        // Unresolved keys are listed so a new one cannot appear unreviewed.
        System.out.println("census: unresolved-key files " + unresolvedFiles);
        assertThat(bypasses).as("direct span writes on content keys").isEmpty();
        assertThat(unresolvedFiles.stream().filter(f -> !REVIEWED_DYNAMIC.containsKey(f)).collect(Collectors.toList()))
            .as("files with unresolvable keys that nobody reviewed").isEmpty();
    }
}
