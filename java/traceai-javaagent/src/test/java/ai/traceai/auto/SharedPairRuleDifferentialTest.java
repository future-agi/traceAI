package ai.traceai.auto;

import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.*;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.Test;
import static org.junit.jupiter.api.Assertions.*;

/**
 * The pair rule changes nothing but credential-named pairs: every shared wire fixture and every shared privacy fixture is exported
 * with and without it, and the two outputs are byte-equal except where the rule's output holds the placeholder.
 */
class SharedPairRuleDifferentialTest {
    @AfterEach void restore() { CredentialPairs.setEnabledForTests(true); }

    /**
     * A placeholder written inside the quote spelling of a quoted credential name: a double quote, an apostrophe, an escaped quote
     * (backslash + double quote) or the entity {@code &quot;}. In the JSON-dumped strings that are compared, a double quote appears as
     * backslash + quote and an escaped quote with more backslashes in front, so any run of backslashes before a double quote is one spelling.
     * Both quotes around the placeholder use the same spelling.
     */
    private static final java.util.regex.Pattern QUOTED_PLACEHOLDER = java.util.regex.Pattern.compile("(\\\\*\"|'|&quot;)<redacted>\\1");

    /**
     * {@code with} equals {@code without} once each run of text that {@code with} replaced by the placeholder is allowed to stand for any text;
     * a quoted placeholder counts as one replaced run, its quotes included.
     */
    static boolean equalExceptReplacedPairs(String without, String with) {
        if (without.equals(with)) return true;
        with = QUOTED_PLACEHOLDER.matcher(with).replaceAll("<redacted>");
        if (without.equals(with)) return true;
        String[] pieces = with.split(java.util.regex.Pattern.quote("<redacted>"), -1);
        if (pieces.length == 1 || !without.startsWith(pieces[0]) || !without.endsWith(pieces[pieces.length - 1])) return false;
        int at = pieces[0].length(), end = without.length() - pieces[pieces.length - 1].length();
        if (at > end) return false;
        for (int i = 1; i < pieces.length - 1; i++) {
            int found = without.indexOf(pieces[i], at);
            if (found < 0 || found + pieces[i].length() > end) return false;
            at = found + pieces[i].length();
        }
        return true;
    }

    @Test void theComparisonAllowsOnlyReplacedRuns() {
        assertTrue(equalExceptReplacedPairs("a password=hunter2, b=1", "a password=<redacted>, b=1"));
        assertTrue(equalExceptReplacedPairs("same", "same"));
        assertFalse(equalExceptReplacedPairs("a password=hunter2, b=1", "a password=<redacted>, b=2"));
        assertFalse(equalExceptReplacedPairs("keep this", "drop this"));
    }

    @Test void aQuotedPlaceholderIsOneReplacedRunInEverySpelling() {
        String bs = "\\";
        for (String quote : List.of("\"", "'", bs + "\"", bs + bs + bs + "\"", "&quot;")) {
            String without = "x password=" + quote + "TAILV1" + quote + " y", with = "x password=" + quote + "<redacted>" + quote + " y";
            assertTrue(equalExceptReplacedPairs(without, with), quote);
            assertTrue(equalExceptReplacedPairs("{" + quote + "password" + quote + ":" + quote + "TAILV1" + quote + ",k:1}",
                "{" + quote + "password" + quote + ":" + quote + "<redacted>" + quote + ",k:1}"), quote);
            // text outside the quoted placeholder still has to match
            assertFalse(equalExceptReplacedPairs(without, with.replace(" y", " z")), quote);
            assertFalse(equalExceptReplacedPairs(without, with.replace("x ", "w ")), quote);
            // two different spellings around a placeholder are not a quoted placeholder: the stray quote is outside any replaced run
            String other = quote.equals("'") ? "\"" : "'";
            assertFalse(equalExceptReplacedPairs(without, "x password=" + quote + "<redacted>" + other + " y"), quote);
            // the swap is refused
            assertFalse(equalExceptReplacedPairs(with, without), quote);
        }
    }

    /** Every attribute a wire fixture builds, each build and export made from scratch with the rule off and again with it on. */
    static Map<String, String> exportedWireAttributes(Path path, boolean rule) throws Exception {
        CredentialPairs.setEnabledForTests(rule);
        Map<String, Object> fixture = WireCore.map(WireCore.parse(Files.readString(path)));
        WireCore.Request request = WireFixturesTest.request(WireCore.map(fixture.get("request")));
        WireCore.Match match = WireCore.match(request.method, request.url);
        WireCore.Result result = match == null ? null : WireCore.build(match, request, WireFixturesTest.response(WireCore.map(fixture.get("response"))));
        Map<String, String> out = new LinkedHashMap<>();
        if (result == null) return out;
        for (var attribute : result.attributes().entrySet()) {
            Object value = attribute.getValue();
            if (value == null) continue;
            String text = value instanceof String string ? string : WireCore.dump(value);
            out.put(attribute.getKey(), SharedPairRuleRegressionTest.exported(SharedPairRuleTest.export(Map.of(attribute.getKey(), text)), attribute.getKey()));
        }
        return out;
    }

    /** One attribute through the export path the wire attributes use, with the rule off or on. */
    private static String exportedAttribute(String key, String text, boolean rule) {
        CredentialPairs.setEnabledForTests(rule);
        return SharedPairRuleRegressionTest.exported(SharedPairRuleTest.export(Map.of(key, text)), key);
    }

    private static void assertPositiveControl(String key, String text) {
        String without = exportedAttribute(key, text, false), with = exportedAttribute(key, text, true);
        assertNotEquals(without, with, "the rule has to change the control: " + with);
        assertTrue(without.contains("TAILV"), without);
        assertFalse(with.contains("TAILV"), with);
        assertTrue(equalExceptReplacedPairs(without, with), "without: " + without + "\n  with: " + with);
        // swapped outputs, and an output altered outside the placeholder, are refused
        assertFalse(equalExceptReplacedPairs(with, without), "swapped");
        assertFalse(equalExceptReplacedPairs(without, "X" + with), "altered before");
        assertFalse(equalExceptReplacedPairs(without, with + "X"), "altered after");
        assertFalse(equalExceptReplacedPairs(without, with.replaceFirst("[a-z]", "#")), "altered inside the kept text");
    }

    @Test void thePositiveControlDiffersWithTheRuleAndIsAcceptedOnlyAsAReplacedRun() {
        // the same export path as the wire attributes: a plain string, and a JSON-dumped map the way WireCore.dump writes a structured value
        assertPositiveControl("input.value", "please log in with password=TAILV1 and then retry");
        assertPositiveControl("output.value", "log in as \"password\": \"TAILV2\" and go on"); // a quoted name: the placeholder is quoted too
        assertPositiveControl("output.value", WireCore.dump(Map.of("note", "go on", "text", "token: TAILV3 then stop")));
    }

    @Test void everySharedWireFixtureIsExportedUnchangedExceptInCredentialPairs() throws Exception {
        assertPositiveControl("input.value", "log in with password=TAILV4 now");
        List<Path> fixtures;
        try (var paths = Files.walk(WireFixturesTest.SPEC.resolve("fixtures"))) { fixtures = paths.filter(p -> p.toString().endsWith(".json")).sorted().toList(); }
        int compared = 0, replaced = 0;
        List<String> failures = new ArrayList<>();
        for (Path path : fixtures) {
            if (String.valueOf(WireCore.map(WireCore.parse(Files.readString(path))).get("name")).startsWith("milestone-a10-bedrock-")) continue;
            Map<String, String> without = exportedWireAttributes(path, false), with = exportedWireAttributes(path, true);
            assertEquals(without.keySet(), with.keySet(), path.toString());
            for (String key : without.keySet()) {
                compared++;
                if (!without.get(key).equals(with.get(key))) replaced++;
                if (!equalExceptReplacedPairs(without.get(key), with.get(key))) failures.add(path.getFileName() + " " + key + "\n  without: " + without.get(key) + "\n  with:    " + with.get(key));
            }
        }
        System.out.println("pair rule differential, wire fixtures: " + fixtures.size() + " files, " + compared + " attributes compared, " + replaced + " differ");
        assertTrue(compared > 200, "compared " + compared);
        assertTrue(failures.isEmpty(), failures.size() + " differences outside a replaced pair\n" + String.join("\n", failures.subList(0, Math.min(10, failures.size()))));
    }

    private static void leaves(Object node, Set<String> into) {
        if (node instanceof String text) { if (!text.isBlank() && text.length() < 4000) into.add(text); }
        else if (node instanceof Map<?, ?> map) map.values().forEach(v -> leaves(v, into));
        else if (node instanceof List<?> list) list.forEach(v -> leaves(v, into));
    }

    /**
     * The attributes a library or agent integration really emits for one corpus text, string-valued ones only (the export path under test is the string path).
     * Where each key list comes from:
     * <ul>
     * <li>LangChain4j chat model: java/traceai-langchain4j TracedChatLanguageModel.generate. It runs the real producer, FITracer (java/traceai-java-core), the way
     *     generate does: FITracer.message(role, text) into setInputMessages / setOutputMessages (keys gen_ai.input.messages and gen_ai.output.messages, a JSON
     *     array of {role, content}), setOutputValue (output.value), and the tool-call attributes it writes through tracer.setAttribute with the key
     *     MESSAGE_TOOL_CALLS + ".0." + TOOL_CALL_ID / TOOL_CALL_FUNCTION_NAME / TOOL_CALL_FUNCTION_ARGUMENTS (message.tool_calls.0.tool_call.id, ...function.name,
     *     ...function.arguments), whose arguments value is the tool request's JSON text.</li>
     * <li>LangChain4j and Spring AI tool spans: javaagent ToolSpans.exit (span.setAttribute calls): gen_ai.tool.name, gen_ai.tool.call.id (LangChain4j request id),
     *     gen_ai.tool.description (Spring AI definition), input.value (the JSON arguments text), output.value (the tool result text).</li>
     * <li>Spring AI chat model: java/traceai-spring-ai TracedChatModel.call (about lines 160 to 225). It runs the same FITracer calls: setInputMessages with one
     *     message per prompt message (system, user), setInputValue with the message texts joined by newlines (input.value), setOutputValue and setOutputMessages
     *     with the assistant text.</li>
     * <li>Google ADK: java/traceai-java-google-adk AdkAttributes.llm(request, response) and AdkAttributes.tool(...), the producers used by TracedLlm / TracedTool and
     *     the agent's GoogleAdk.java, fed with the JSON shapes ADK serializes (the REQUEST and RESPONSE shapes of AdkAttributesTest). Real keys:
     *     gen_ai.input.messages.N.message.content / .tool_calls.K.tool_call.function.arguments (JSON text of the call args), gen_ai.output.messages.N.message.content,
     *     gen_ai.tool.definitions.N.tool.json_schema, gen_ai.request.parameters, input.value, output.value, gen_ai.tool.call.arguments, gen_ai.tool.call.result.</li>
     * </ul>
     */
    static final List<String> INTEGRATIONS = List.of("langchain4j", "spring-ai", "google-adk");

    private static Map<String, String> strings(Map<String, ?> attributes) {
        Map<String, String> out = new TreeMap<>();
        attributes.forEach((key, value) -> { if (value instanceof String string) out.put(key, string); });
        return out;
    }

    /** Runs {@code fill} against a real FITracer and returns the string attributes of the span it wrote. */
    private static Map<String, String> fromFITracer(String kind, java.util.function.BiConsumer<ai.traceai.FITracer, io.opentelemetry.api.trace.Span> fill) {
        var memory = io.opentelemetry.sdk.testing.exporter.InMemorySpanExporter.create();
        try (var provider = io.opentelemetry.sdk.trace.SdkTracerProvider.builder()
                .addSpanProcessor(io.opentelemetry.sdk.trace.export.SimpleSpanProcessor.create(memory)).build()) {
            var tracer = new ai.traceai.FITracer(provider.get("integration"));
            var span = tracer.startSpan(kind, ai.traceai.FISpanKind.valueOf(kind));
            fill.accept(tracer, span);
            span.end();
            Map<String, String> out = new TreeMap<>();
            memory.getFinishedSpanItems().get(0).getAttributes().forEach((key, value) -> { if (value instanceof String string) out.put(key.getKey(), string); });
            return out;
        }
    }

    /** The attribute maps one integration emits, the corpus text placed in every value position that integration fills. */
    static List<Map<String, String>> integrationAttributes(String integration, String text) {
        String arguments = WireCore.dump(new TreeMap<>(Map.of("query", text, "limit", 3)));
        String result = WireCore.dump(new TreeMap<>(Map.of("answer", text)));
        List<Map<String, String>> maps = new ArrayList<>();
        switch (integration) {
            case "langchain4j" -> {
                maps.add(fromFITracer("LLM", (tracer, span) -> {
                    tracer.setInputMessages(span, List.of(ai.traceai.FITracer.message("system", "Be brief"), ai.traceai.FITracer.message("user", text)));
                    tracer.setOutputValue(span, text);
                    tracer.setOutputMessages(span, List.of(ai.traceai.FITracer.message("assistant", text)));
                    String base = ai.traceai.SemanticConventions.MESSAGE_TOOL_CALLS + ".0.";
                    tracer.setAttribute(span, base + ai.traceai.SemanticConventions.TOOL_CALL_ID, "call-1");
                    tracer.setAttribute(span, base + ai.traceai.SemanticConventions.TOOL_CALL_FUNCTION_NAME, "lookup");
                    tracer.setAttribute(span, base + ai.traceai.SemanticConventions.TOOL_CALL_FUNCTION_ARGUMENTS, arguments);
                }));
                maps.add(toolSpanAttributes("call-1", null, arguments, text));
                maps.add(toolSpanAttributes("call-1", null, text, result));
            }
            case "spring-ai" -> {
                maps.add(fromFITracer("LLM", (tracer, span) -> {
                    tracer.setInputMessages(span, List.of(ai.traceai.FITracer.message("system", "Be brief"), ai.traceai.FITracer.message("user", text)));
                    tracer.setInputValue(span, "Be brief\n" + text);
                    tracer.setOutputValue(span, text);
                    tracer.setOutputMessages(span, List.of(ai.traceai.FITracer.message("assistant", text)));
                }));
                maps.add(toolSpanAttributes(null, text, arguments, text));
                maps.add(toolSpanAttributes(null, "lookup", text, result));
            }
            case "google-adk" -> {
                Map<String, Object> request = Map.of("model", "gemini-2.0-flash",
                    "config", Map.of("systemInstruction", Map.of("parts", List.of(Map.of("text", text)), "role", "user"),
                        "tools", List.of(Map.of("functionDeclarations", List.of(Map.of("name", "lookup", "description", text))))),
                    "contents", List.of(
                        Map.of("role", "user", "parts", List.of(Map.of("text", text))),
                        Map.of("role", "model", "parts", List.of(Map.of("functionCall", Map.of("id", "call-1", "name", "lookup", "args", Map.of("query", text))))),
                        Map.of("role", "user", "parts", List.of(Map.of("functionResponse", Map.of("id", "call-1", "name", "lookup", "response", Map.of("answer", text)))))));
                Map<String, Object> response = Map.of("content", Map.of("role", "model", "parts", List.of(Map.of("text", text))), "finishReason", "STOP");
                maps.add(strings(ai.traceai.googleadk.AdkAttributes.llm(request, response)));
                Map<String, Object> callResponse = Map.of("content", Map.of("role", "model", "parts", List.of(Map.of("functionCall", Map.of("id", "call-2", "name", "lookup", "args", Map.of("query", text))))));
                maps.add(strings(ai.traceai.googleadk.AdkAttributes.llm(request, callResponse)));
                maps.add(strings(ai.traceai.googleadk.AdkAttributes.tool("lookup", text, "FunctionTool", Map.of("query", text, "limit", 3), Map.of("answer", text), "call-1")));
                maps.add(strings(ai.traceai.googleadk.AdkAttributes.agent("helper", text)));
            }
            default -> throw new IllegalArgumentException(integration);
        }
        return maps;
    }

    /** The content attributes of a tool span as javaagent ToolSpans.exit writes them. */
    private static Map<String, String> toolSpanAttributes(String callId, String description, String input, String output) {
        Map<String, String> attributes = new TreeMap<>();
        attributes.put("gen_ai.tool.name", "lookup");
        if (callId != null) attributes.put("gen_ai.tool.call.id", callId);
        if (description != null) attributes.put("gen_ai.tool.description", description);
        attributes.put("input.value", input);
        attributes.put("output.value", output);
        return attributes;
    }

    /** Integration attribute shapes (LangChain4j, Spring AI, Google ADK) and annotation captures, over every text the wire fixtures hold. */
    @Test void integrationAndAnnotationTextsAreExportedUnchangedExceptInCredentialPairs() throws Exception {
        Set<String> texts = new TreeSet<>();
        try (var paths = Files.walk(WireFixturesTest.SPEC.resolve("fixtures"))) {
            for (Path path : paths.filter(p -> p.toString().endsWith(".json")).sorted().toList()) leaves(WireCore.parse(Files.readString(path)), texts);
        }
        texts.addAll(List.of("connect with password=hunter2-value, then retry", "token: abc123def456 for the account", "run --password hunter2-value now",
            "Cookie: sid=one111; theme=two222", "user[password]=nested-value ok", "log in with password=TAILV5 now",
            "{\"password\":\"TAILV6\",\"user\":\"alice\"}", "api_key='TAILV7' and more"));
        Map<String, int[]> perIntegration = new LinkedHashMap<>();
        INTEGRATIONS.forEach(name -> perIntegration.put(name, new int[2]));
        int compared = 0, replaced = 0;
        List<String> failures = new ArrayList<>();
        for (String text : texts) {
            for (String integration : INTEGRATIONS) {
                CredentialPairs.setEnabledForTests(false);
                List<Map<String, String>> off = integrationAttributes(integration, text);
                CredentialPairs.setEnabledForTests(true);
                List<Map<String, String>> on = integrationAttributes(integration, text);
                assertEquals(off.size(), on.size());
                for (int i = 0; i < off.size(); i++) {
                    for (var attribute : off.get(i).entrySet()) {
                        String key = attribute.getKey();
                        String without = exportedAttribute(key, attribute.getValue(), false), with = exportedAttribute(key, on.get(i).get(key), true);
                        compared++; perIntegration.get(integration)[0]++;
                        if (!without.equals(with)) { replaced++; perIntegration.get(integration)[1]++; }
                        if (!equalExceptReplacedPairs(without, with)) failures.add(integration + " " + key + " " + text + "\n  without: " + without + "\n  with:    " + with);
                    }
                }
            }
            CredentialPairs.setEnabledForTests(false);
            String capturedWithout = String.valueOf(CapturedValue.of(text, 4096));
            CredentialPairs.setEnabledForTests(true);
            String capturedWith = String.valueOf(CapturedValue.of(text, 4096));
            compared++;
            if (!capturedWithout.equals(capturedWith)) replaced++;
            if (!equalExceptReplacedPairs(capturedWithout, capturedWith)) failures.add("annotation " + text + "\n  without: " + capturedWithout + "\n  with:    " + capturedWith);
        }
        StringBuilder counts = new StringBuilder();
        perIntegration.forEach((name, n) -> counts.append(", ").append(name).append(" ").append(n[0]).append(" compared ").append(n[1]).append(" differ"));
        System.out.println("pair rule differential, integration and annotation texts: " + texts.size() + " texts, " + compared + " exports compared, " + replaced + " differ" + counts);
        assertTrue(replaced >= 5, "the rule has to change something in the corpus: " + replaced);
        perIntegration.forEach((name, n) -> {
            assertTrue(n[0] > 0, name + " produced attributes");
            assertTrue(n[1] >= 5, name + " has to be changed by the rule somewhere in the corpus: " + n[1]);
        });
        assertTrue(failures.isEmpty(), failures.size() + " differences outside a replaced pair\n" + String.join("\n", failures.subList(0, Math.min(10, failures.size()))));
    }

    @Test void nothingInTheAgentSwitchesThePairRuleOff() throws Exception {
        Path main = Path.of("src", "main", "java");
        int visited = 0;
        boolean sawCredentialPairs = false;
        var write = java.util.regex.Pattern.compile("\\benabled\\s*(?:<<|>>>?|[-+*/%&|^])?=(?!=)");
        try (var files = Files.walk(main)) {
            for (Path file : files.filter(p -> p.toString().endsWith(".java")).sorted().toList()) {
                visited++;
                String source = Files.readString(file);
                if (file.getFileName().toString().equals("CredentialPairs.java")) {
                    sawCredentialPairs = true;
                    assertEquals(1, source.split("setEnabledForTests", -1).length - 1, "defined once, never called");
                    int open = source.indexOf('{', source.indexOf("static void setEnabledForTests("));
                    assertTrue(open > 0, "setEnabledForTests is defined");
                    int depth = 0, close = -1;
                    for (int i = open; i < source.length() && close < 0; i++) {
                        if (source.charAt(i) == '{') depth++;
                        else if (source.charAt(i) == '}' && --depth == 0) close = i;
                    }
                    assertTrue(close > open, "setEnabledForTests has a body");
                    var assignments = write.matcher(source);
                    int declarations = 0, insideSetter = 0;
                    while (assignments.find()) {
                        int lineStart = source.lastIndexOf('\n', assignments.start()) + 1;
                        String line = source.substring(lineStart, source.indexOf('\n', assignments.start()));
                        if (assignments.start() > open && assignments.start() < close) insideSetter++;
                        else if (line.matches("\\s*(?:private\\s+)?static\\s+volatile\\s+boolean\\s+enabled\\s*=\\s*true;.*")) declarations++;
                        else fail("enabled is written outside setEnabledForTests: " + line.trim());
                    }
                    assertEquals(1, declarations, "the field declaration");
                    assertEquals(1, insideSetter, "the one write is inside setEnabledForTests");
                    continue;
                }
                assertFalse(source.contains("setEnabledForTests") || source.contains("CredentialPairs.enabled"), file.toString());
            }
        }
        assertTrue(visited > 0, "the scan visited src/main/java files from " + main.toAbsolutePath());
        assertTrue(sawCredentialPairs, "CredentialPairs.java was among the " + visited + " files scanned");
    }

    /** Every string leaf that holds a JSON object or array, written back as the export writes a changed one (compact). */
    @SuppressWarnings("unchecked")
    static Object compact(Object value) {
        if (value instanceof String text) {
            String trimmed = text.stripLeading();
            if (!trimmed.startsWith("{") && !trimmed.startsWith("[")) return text;
            try {
                Object parsed = WireCore.parse(text);
                return parsed instanceof Map || parsed instanceof List ? WireCore.dump(parsed) : text;
            } catch (RuntimeException notJson) { return text; }
        }
        if (value instanceof Map<?, ?> map) {
            Map<String, Object> out = new LinkedHashMap<>();
            ((Map<String, Object>)map).forEach((key, item) -> out.put(key, compact(item)));
            return out;
        }
        if (value instanceof List<?> list) { List<Object> out = new ArrayList<>(); for (Object item : list) out.add(compact(item)); return out; }
        return value;
    }

    @Test void everySharedPrivacyFixtureIsCleanedUnchangedExceptInCredentialPairs() throws Exception {
        List<Object> cases;
        try (var in = WirePrivacy.class.getResourceAsStream("privacy-fixtures.json")) {
            cases = WireCore.list(WireCore.get(WireCore.parse(new String(Objects.requireNonNull(in).readAllBytes(), StandardCharsets.UTF_8)), "cases"));
        }
        int replaced = 0;
        List<String> failures = new ArrayList<>();
        for (Object fixture : cases) {
            var input = WireCore.map(WireCore.get(fixture, "input"));
            var registry = new KnownSecrets();
            for (Object secret : WireCore.list(input.get("known_secrets"))) registry.register((String)secret);
            boolean framed = !"free_text_urls".equals(input.get("site"));
            String[] outputs = new String[2];
            Object[] cleaned = new Object[2];
            for (int i = 0; i < 2; i++) {
                CredentialPairs.setEnabledForTests(i == 1);
                var privacy = new WirePrivacy(registry.snapshot(), false);
                var scope = privacy.scope(WireCore.map(input.get("siblings")), "", WirePrivacy.Scope.EMPTY);
                cleaned[i] = privacy.clean((String)input.get("path"), input.get("value"), scope, framed);
                outputs[i] = WireCore.dump(cleaned[i]);
            }
            // A string that holds JSON is written back compactly once any of its leaves changes; with the rule off nothing may change,
            // and the string keeps its own spacing. The text without the rule is compared in the form the export writes it.
            if (!outputs[0].equals(outputs[1])) outputs[0] = WireCore.dump(compact(cleaned[0]));
            if (!outputs[0].equals(outputs[1])) replaced++;
            if (!equalExceptReplacedPairs(outputs[0], outputs[1])) failures.add(WireCore.get(fixture, "name") + "\n  without: " + outputs[0] + "\n  with:    " + outputs[1]);
        }
        System.out.println("pair rule differential, privacy fixtures: " + cases.size() + " cases, " + replaced + " differ");
        assertTrue(cases.size() >= 6238);
        assertTrue(failures.isEmpty(), failures.size() + " differences outside a replaced pair\n" + String.join("\n", failures.subList(0, Math.min(10, failures.size()))));
    }
}
