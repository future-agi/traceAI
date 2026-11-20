package ai.traceai.auto;

import com.sun.net.httpserver.HttpServer;
import org.junit.jupiter.api.*;
import java.io.File;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.nio.file.*;
import java.util.*;
import java.util.concurrent.*;
import java.util.stream.Collectors;
import static org.junit.jupiter.api.Assertions.*;

/**
 * The packaged agent, in a child JVM, adds an execute_tool span for LangChain4j tool executors and Spring AI
 * tool callbacks with no application change (ToolApp), under the library's own name and hide switches.
 */
class ToolSpansE2ETest {
    HttpServer server;
    ExecutorService executor;
    final List<Map<String, Object>> spans = new CopyOnWriteArrayList<>();

    @BeforeEach void startServer() throws Exception {
        server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        server.createContext("/tracer/v1/traces", exchange -> {
            Object root = WireCore.parse(new String(exchange.getRequestBody().readAllBytes(), StandardCharsets.UTF_8));
            for (Object resource : WireCore.list(WireCore.get(root, "resourceSpans")))
                for (Object scope : WireCore.list(WireCore.get(resource, "scopeSpans")))
                    for (Object span : WireCore.list(WireCore.get(scope, "spans"))) spans.add(WireCore.map(span));
            exchange.getResponseHeaders().add("Content-Type", "application/json"); exchange.sendResponseHeaders(200, 2);
            try (var out = exchange.getResponseBody()) { out.write("{}".getBytes(StandardCharsets.UTF_8)); }
        });
        executor = Executors.newFixedThreadPool(2); server.setExecutor(executor); server.start();
    }
    @AfterEach void stopServer() { server.stop(0); executor.shutdownNow(); }

    String run(String scenario, Map<String, String> options) throws Exception {
        Path agent = AgentE2ETest.agentJar(); assertTrue(Files.isRegularFile(agent));
        String cp = System.getProperty("surefire.test.class.path", System.getProperty("java.class.path"));
        String classes = Path.of("target", "classes").toAbsolutePath().normalize().toString();
        // The agent's classes come only from the packaged jar.
        cp = Arrays.stream(cp.split(java.util.regex.Pattern.quote(File.pathSeparator)))
            .filter(p -> !Path.of(p).toAbsolutePath().normalize().toString().equals(classes)).collect(Collectors.joining(File.pathSeparator));
        Path output = Files.createTempFile(Path.of("target"), "tools-e2e-", ".log");
        List<String> command = List.of(Path.of(System.getProperty("java.home"), "bin", "java").toString(), "-javaagent:" + agent, "-cp", cp, ToolApp.class.getName(), scenario);
        ProcessBuilder pb = new ProcessBuilder(command).redirectErrorStream(true).redirectOutput(output.toFile());
        Map<String, String> env = pb.environment(); env.keySet().removeIf(k -> k.startsWith("FI_") || k.startsWith("OTEL_"));
        env.put("FI_CONFIG_FILE", "target/no-e2e-config.toml"); env.put("FI_BASE_URL", "http://127.0.0.1:" + server.getAddress().getPort());
        env.put("FI_API_KEY", "test-api-key"); env.put("FI_SECRET_KEY", "test-secret-key");
        env.putAll(options);
        Process process = pb.start();
        if (!process.waitFor(40, TimeUnit.SECONDS)) { process.destroyForcibly(); fail("Child timed out: " + Files.readString(output)); }
        String log = Files.readString(output); assertEquals(0, process.exitValue(), log);
        assertTrue(log.contains("DONE"), log);
        Files.deleteIfExists(output);
        return log;
    }
    static String line(String log, String prefix) {
        return log.lines().filter(l -> l.startsWith(prefix)).findFirst().orElseThrow(() -> new AssertionError(prefix + " missing: " + log)).substring(prefix.length());
    }
    static Map<String, Object> attributes(Map<String, Object> span) {
        Map<String, Object> attrs = new LinkedHashMap<>();
        for (Object raw : WireCore.list(span.get("attributes"))) {
            Map<String, Object> value = WireCore.map(WireCore.get(raw, "value"));
            attrs.put(WireCore.str(WireCore.get(raw, "key")), WireCore.or(value.get("stringValue"), value.get("intValue"), value.get("boolValue"), value.get("doubleValue")));
        }
        return attrs;
    }
    List<Map<String, Object>> tools() {
        List<Map<String, Object>> result = new ArrayList<>(spans);
        result.sort(Comparator.comparing(span -> Long.parseLong(String.valueOf(span.get("startTimeUnixNano")))));
        return result;
    }
    static boolean everyToolSpanHas(List<Map<String, Object>> spans, String needle) { return WireCore.dump(spans).contains(needle); }

    @Test void everyToolExecutionIsOneInternalSpanUnderTheApplicationSpan() throws Exception {
        String log = run("basic", Map.of());
        assertEquals("PRIVATE_TOOL_RESULT", line(log, "LC4J=")); assertEquals("PRIVATE_TOOL_RESULT", line(log, "SPRING_ONE=")); assertEquals("PRIVATE_TOOL_RESULT", line(log, "SPRING_TWO="));
        // One LangChain4j execution, and two Spring AI calls (the one-argument call delegating to the two-argument one is still one span).
        assertEquals(3, spans.size(), spans.toString());
        for (Map<String, Object> span : spans) {
            Map<String, Object> attrs = attributes(span);
            assertEquals("execute_tool lookup", span.get("name")); assertEquals(1, ((Number)span.get("kind")).intValue());
            assertEquals(line(log, "PARENT="), span.get("parentSpanId"));
            assertEquals("TOOL", attrs.get("fi.span.kind")); assertEquals("execute_tool", attrs.get("gen_ai.operation.name")); assertEquals("lookup", attrs.get("gen_ai.tool.name"));
            assertEquals(ToolApp.ARGS, attrs.get("input.value")); assertEquals("PRIVATE_TOOL_RESULT", attrs.get("output.value"));
            assertEquals(1, ((Number)WireCore.get(span.get("status"), "code")).intValue());
        }
        List<Map<String, Object>> ordered = tools();
        assertEquals("call-9", attributes(ordered.get(0)).get("gen_ai.tool.call.id"));
        assertNull(attributes(ordered.get(0)).get("gen_ai.tool.description"), "a LangChain4j executor does not know the description");
        assertEquals("PRIVATE_TOOL_DESCRIPTION", attributes(ordered.get(1)).get("gen_ai.tool.description"));
        assertNull(attributes(ordered.get(1)).get("gen_ai.tool.call.id"));
    }
    @Test void withoutAnApplicationSpanTheToolSpanIsARoot() throws Exception {
        run("no_parent", Map.of());
        assertEquals(3, spans.size(), spans.toString());
        for (Map<String, Object> span : spans) assertNull(span.get("parentSpanId"));
    }
    @Test void aFailingToolRecordsTheExceptionAndTheApplicationSeesTheSameInstance() throws Exception {
        String log = run("failure", Map.of());
        assertEquals(List.of("SAME=true", "SAME=true"), log.lines().filter(l -> l.startsWith("SAME=") || l.startsWith("NO_FAILURE")).toList());
        assertEquals(2, spans.size(), spans.toString());
        for (Map<String, Object> span : spans) {
            assertEquals("execute_tool failing", span.get("name"));
            assertEquals(2, ((Number)WireCore.get(span.get("status"), "code")).intValue());
            assertTrue(WireCore.dump(span.get("events")).contains("exception"), span.toString());
        }
    }
    @Test void aLibraryToolSpanThatIsAlreadyCurrentIsNotTracedAgain() throws Exception {
        run("library_span", Map.of());
        assertTrue(spans.isEmpty(), spans.toString());
    }
    @Test void hideInputsKeepsArgumentsAndDescriptionOut() throws Exception {
        run("basic", Map.of("FI_HIDE_INPUTS", "true"));
        assertEquals(3, spans.size());
        assertFalse(everyToolSpanHas(spans, "PRIVATE_TOOL_ARGUMENTS")); assertFalse(everyToolSpanHas(spans, "PRIVATE_TOOL_DESCRIPTION"));
        assertTrue(everyToolSpanHas(spans, "PRIVATE_TOOL_RESULT"));
    }
    @Test void hideOutputsKeepsTheResultOut() throws Exception {
        run("basic", Map.of("FI_HIDE_OUTPUTS", "true"));
        assertEquals(3, spans.size());
        assertFalse(everyToolSpanHas(spans, "PRIVATE_TOOL_RESULT")); assertTrue(everyToolSpanHas(spans, "PRIVATE_TOOL_ARGUMENTS"));
    }
    @Test void hideInputMessagesKeepsArgumentsOut() throws Exception {
        run("basic", Map.of("FI_HIDE_INPUT_MESSAGES", "true"));
        assertEquals(3, spans.size()); assertFalse(everyToolSpanHas(spans, "PRIVATE_TOOL_ARGUMENTS")); assertTrue(everyToolSpanHas(spans, "PRIVATE_TOOL_RESULT"));
    }
    @Test void hideOutputMessagesKeepsTheResultOut() throws Exception {
        run("basic", Map.of("FI_HIDE_OUTPUT_MESSAGES", "true"));
        assertEquals(3, spans.size()); assertFalse(everyToolSpanHas(spans, "PRIVATE_TOOL_RESULT")); assertTrue(everyToolSpanHas(spans, "PRIVATE_TOOL_ARGUMENTS"));
    }
    @Test void hideErrorDetailsKeepsTheFailureTextOut() throws Exception {
        run("failure", Map.of("FI_HIDE_ERROR_DETAILS", "true"));
        assertEquals(2, spans.size()); assertFalse(everyToolSpanHas(spans, "PRIVATE_TOOL_FAILURE"));
        for (Map<String, Object> span : spans) assertEquals(2, ((Number)WireCore.get(span.get("status"), "code")).intValue());
    }
    @Test void disablingLangchain4jTurnsOffOnlyItsToolSpans() throws Exception {
        run("basic", Map.of("FI_DISABLED_INSTRUMENTATIONS", "langchain4j"));
        assertEquals(2, spans.size(), spans.toString());
        for (Map<String, Object> span : spans) assertEquals("PRIVATE_TOOL_DESCRIPTION", attributes(span).get("gen_ai.tool.description"));
    }
    @Test void disablingSpringAiTurnsOffOnlyItsToolSpans() throws Exception {
        run("basic", Map.of("FI_DISABLED_INSTRUMENTATIONS", "spring-ai"));
        assertEquals(1, spans.size(), spans.toString()); assertEquals("call-9", attributes(spans.get(0)).get("gen_ai.tool.call.id"));
    }
    @Test void anAllowListWithoutTheLibrariesTracesNoTools() throws Exception {
        run("basic", Map.of("FI_INSTRUMENTATIONS", "okhttp"));
        assertTrue(spans.isEmpty(), spans.toString());
    }

    long appSpans(String log) { return log.lines().filter(l -> l.equals("APP_SPAN=execute_tool lookup")).count(); }

    @Test void theLibrarysOwnWrappersAreNotTracedAgain() throws Exception {
        // The wrapper opens its TOOL span after the agent's enter advice ran on it: one execute_tool span in all, the wrapper's.
        String log = run("wrapped", Map.of());
        assertEquals(2, appSpans(log), log);
        assertTrue(spans.isEmpty(), "the agent added a sibling span to a wrapper's: " + spans);
    }
    @Test void aNativeSpringAiToolObservationSpanNamedForTheToolIsNotTracedAgain() throws Exception {
        run("native_name", Map.of());
        assertTrue(spans.isEmpty(), spans.toString());
    }
    @Test void aNativeSpringAiToolObservationSpanTaggedToolCallIsNotTracedAgain() throws Exception {
        run("native_attribute", Map.of());
        assertTrue(spans.isEmpty(), spans.toString());
    }
    @Test void aNativeSpringAiSpanNamedSpringAiToolIsNotTracedAgain() throws Exception {
        run("native_spring_ai_tool", Map.of());
        assertTrue(spans.isEmpty(), spans.toString());
    }
    @Test void aNativeSpringAiSpanNamedToolCallAndTheToolIsNotTracedAgain() throws Exception {
        run("native_tool_call_name", Map.of());
        assertTrue(spans.isEmpty(), spans.toString());
    }
    @Test void anApplicationSubclassOfALibraryWrapperIsNotTracedAgain() throws Exception {
        String log = run("wrapper_subclass", Map.of());
        assertEquals(2, appSpans(log), log);
        assertTrue(spans.isEmpty(), "the agent added a sibling span to a wrapper subclass's: " + spans);
    }
    @Test void aRecursionThroughTheTwoArgumentOverloadIsTracedAtEveryLevel() throws Exception {
        run("recursive_two_arg", Map.of());
        assertEquals(2, spans.size(), spans.toString());
    }
    @Test void aCalleeWhoseEqualsThrowsStillGetsItsSpanAndItsResult() throws Exception {
        String log = run("hostile_equals", Map.of());
        assertEquals("PRIVATE_TOOL_RESULT", line(log, "SPRING_ONE="));
        assertEquals(1, spans.size(), spans.toString());
    }
    @Test void aRecursiveToolCallIsTracedAtEveryLevel() throws Exception {
        run("recursive", Map.of());
        // One LangChain4j recursion (2 calls) and one Spring AI recursion (2 calls, each through the delegating overload).
        assertEquals(4, spans.size(), spans.toString());
    }
    @Test void theToolIntegrationStatusSaysChatCallsGoThroughWireOnlyWhenWireIsOn() throws Exception {
        String on = run("doctor", Map.of());
        assertTrue(line(on, "DOCTOR=").contains("chat calls through wire"), on);
        String off = run("doctor", Map.of("FI_WIRE_CAPTURE", "false"));
        assertFalse(line(off, "DOCTOR=").contains("chat calls through wire"), off);
        assertTrue(line(off, "DOCTOR=").contains("execute_tool spans"), off);
        assertTrue(line(off, "DOCTOR=").contains("manual (use ai.traceai.langchain4j.TracedChatLanguageModel"), "manual-wrapper hint kept: " + off);
    }
}
