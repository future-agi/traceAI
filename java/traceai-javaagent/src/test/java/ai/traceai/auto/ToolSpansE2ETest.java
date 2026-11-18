package ai.traceai.auto;

import com.sun.net.httpserver.HttpServer;
import org.junit.jupiter.api.*;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;
import java.io.File;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.nio.file.*;
import java.util.*;
import java.util.concurrent.*;
import static org.junit.jupiter.api.Assertions.*;

/** Zero-code execute_tool spans for LangChain4j AiServices tools and Spring AI ToolCallbacks, in a child JVM with the packaged agent. */
class ToolSpansE2ETest {
    HttpServer server;
    ExecutorService serverExecutor;
    final List<Map<String, Object>> spans = new CopyOnWriteArrayList<>();
    final List<String> payloads = new CopyOnWriteArrayList<>();

    @BeforeEach void startServer() throws Exception {
        server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        server.createContext("/v1/", exchange -> {
            exchange.getRequestBody().readAllBytes();
            byte[] body = AgentE2ETest.BASIC.getBytes(StandardCharsets.UTF_8);
            exchange.getResponseHeaders().add("Content-Type", "application/json"); exchange.sendResponseHeaders(200, body.length);
            try (var out = exchange.getResponseBody()) { out.write(body); }
        });
        server.createContext("/tracer/v1/traces", exchange -> {
            String body = new String(exchange.getRequestBody().readAllBytes(), StandardCharsets.UTF_8);
            payloads.add(body);
            Object root = WireCore.parse(body);
            for (Object resource : WireCore.list(WireCore.get(root, "resourceSpans")))
                for (Object scope : WireCore.list(WireCore.get(resource, "scopeSpans")))
                    for (Object span : WireCore.list(WireCore.get(scope, "spans"))) spans.add(WireCore.map(span));
            exchange.getResponseHeaders().add("Content-Type", "application/json"); exchange.sendResponseHeaders(200, 2);
            exchange.getResponseBody().write("{}".getBytes(StandardCharsets.UTF_8)); exchange.close();
        });
        serverExecutor = Executors.newFixedThreadPool(3); server.setExecutor(serverExecutor); server.start();
    }
    @AfterEach void stopServer() { server.stop(0); serverExecutor.shutdownNow(); }

    String run(String scenario, Map<String, String> options) throws Exception {
        spans.clear(); payloads.clear();
        String cp = System.getProperty("surefire.test.class.path", System.getProperty("java.class.path"));
        String classes = Path.of("target", "classes").toAbsolutePath().normalize().toString();
        cp = String.join(File.pathSeparator, Arrays.stream(cp.split(java.util.regex.Pattern.quote(File.pathSeparator)))
            .filter(p -> !Path.of(p).toAbsolutePath().normalize().toString().equals(classes)).toList());
        List<String> command = new ArrayList<>();
        command.add(Path.of(System.getProperty("java.home"), "bin", "java").toString());
        if (!options.containsKey("TRACEAI_TEST_NO_AGENT")) command.add("-javaagent:" + AgentE2ETest.agentJar());
        command.addAll(List.of("-cp", cp, ToolApp.class.getName(), "http://127.0.0.1:" + server.getAddress().getPort() + "/v1/chat/completions", scenario));
        Path output = Files.createTempFile(Path.of("target"), "tool-e2e-", ".log");
        ProcessBuilder pb = new ProcessBuilder(command).redirectErrorStream(true).redirectOutput(output.toFile());
        Map<String, String> env = pb.environment(); env.keySet().removeIf(k -> k.startsWith("FI_") || k.startsWith("OTEL_"));
        env.put("FI_CONFIG_FILE", "target/no-e2e-config.toml"); env.put("FI_BASE_URL", "http://127.0.0.1:" + server.getAddress().getPort());
        env.put("FI_API_KEY", "test-api-key"); env.put("FI_SECRET_KEY", "test-secret-key");
        env.putAll(options);
        Process process = pb.start();
        if (!process.waitFor(30, TimeUnit.SECONDS)) { process.destroyForcibly(); fail("Child timed out: " + Files.readString(output)); }
        String log = Files.readString(output); assertEquals(0, process.exitValue(), log);
        assertTrue(log.contains("DONE"), log);
        return log;
    }
    static Map<String, Object> attributes(Map<String, Object> span) {
        Map<String, Object> attrs = new LinkedHashMap<>();
        for (Object raw : WireCore.list(span.get("attributes"))) {
            Map<String, Object> value = WireCore.map(WireCore.get(raw, "value"));
            attrs.put(WireCore.str(WireCore.get(raw, "key")), WireCore.or(value.get("stringValue"), value.get("intValue"), value.get("boolValue"), value.get("doubleValue"), value.get("arrayValue")));
        }
        return attrs;
    }
    List<Map<String, Object>> named(String prefix) { return spans.stream().filter(s -> String.valueOf(s.get("name")).startsWith(prefix)).toList(); }
    Map<String, Object> toolSpan() {
        List<Map<String, Object>> tools = named("execute_tool");
        assertEquals(1, tools.size(), "one execute_tool span per tool invocation: " + spans);
        return tools.get(0);
    }
    static int statusCode(Map<String, Object> span) { return ((Number)WireCore.get(span.get("status"), "code")).intValue(); }
    static String line(String log, String prefix) { return AgentE2ETest.line(log, prefix); }

    /** The tool's own provider call is a child of the tool span; the control call after the loop is a root. Both chat spans keep their usual shape. */
    void assertChatSpans(String log, Map<String, Object> tool) {
        List<Map<String, Object>> chats = named("chat ");
        assertEquals(2, chats.size(), spans.toString());
        for (var chat : chats) {
            Map<String, Object> attrs = attributes(chat);
            assertEquals("chat test-model", chat.get("name")); assertEquals(3, ((Number)chat.get("kind")).intValue());
            assertEquals("wire", attrs.get("traceai.capture.layer")); assertEquals("chat", attrs.get("gen_ai.operation.name"));
            assertEquals("Hello world", attrs.get("output.value"));
            assertEquals(1, statusCode(chat));
        }
        String parentOfNested = tool == null ? line(log, "PARENT=") : String.valueOf(tool.get("spanId"));
        assertEquals(1, chats.stream().filter(c -> parentOfNested.equals(c.get("parentSpanId"))).count(), "the tool's provider call is parented by " + parentOfNested + ": " + chats);
        assertEquals(1, chats.stream().filter(c -> c.get("parentSpanId") == null).count(), "the control call stays a root: " + chats);
    }
    void assertToolShape(Map<String, Object> tool, String log, String name, String callId) {
        Map<String, Object> attrs = attributes(tool);
        assertEquals("execute_tool " + name, tool.get("name"));
        assertEquals(1, ((Number)tool.get("kind")).intValue(), "INTERNAL");
        assertEquals(line(log, "PARENT="), tool.get("parentSpanId"), "the current application span is the parent");
        assertEquals(line(log, "TRACE="), tool.get("traceId"));
        assertEquals("execute_tool", attrs.get("gen_ai.operation.name"));
        assertEquals(name, attrs.get("gen_ai.tool.name"));
        assertEquals("function", attrs.get("gen_ai.tool.type"));
        assertEquals("TOOL", attrs.get("gen_ai.span.kind"));
        assertEquals(callId, attrs.get("gen_ai.tool.call.id"));
    }
    void assertNoRawSecret() {
        String all = String.join("\n", payloads);
        assertFalse(all.contains(ToolApp.SECRET), "credential values in tool arguments are redacted");
    }

    @Test void langchain4jToolCallIsTraced() throws Exception {
        String log = run("lc4j", Map.of());
        assertEquals("answer: " + ToolApp.RESULT, line(log, "RESULT="));
        Map<String, Object> tool = toolSpan(); assertToolShape(tool, log, "lookup", "call-1");
        Map<String, Object> attrs = attributes(tool);
        assertEquals(1, statusCode(tool));
        assertTrue(String.valueOf(attrs.get("gen_ai.tool.call.arguments")).contains(ToolApp.CITY), attrs.toString());
        assertEquals(ToolApp.RESULT, attrs.get("gen_ai.tool.call.result"));
        assertTrue(String.valueOf(attrs.get("input.value")).contains(ToolApp.CITY), attrs.toString());
        assertEquals(ToolApp.RESULT, attrs.get("output.value"));
        assertFalse(attrs.containsKey("error.type"));
        assertNoRawSecret();
        assertChatSpans(log, tool);
    }
    @Test void langchain4jToolFailureIsAnErrorSpanAndTheFrameworkBehavesAsWithoutTheAgent() throws Exception {
        String baseline = line(run("lc4j_error", Map.of("TRACEAI_TEST_NO_AGENT", "1")), "RESULT=");
        assertTrue(spans.isEmpty());
        String log = run("lc4j_error", Map.of());
        assertEquals(baseline, line(log, "RESULT="), "LangChain4j still hands the tool error to the model");
        Map<String, Object> tool = toolSpan(); assertToolShape(tool, log, "lookup", "call-1");
        Map<String, Object> attrs = attributes(tool);
        assertEquals(2, statusCode(tool), "ERROR");
        assertEquals(IllegalStateException.class.getName(), attrs.get("error.type"));
        assertEquals(ToolApp.ERROR, WireCore.get(tool.get("status"), "message"));
        assertFalse(attrs.containsKey("gen_ai.tool.call.result"));
        assertNoRawSecret();
    }
    @Test void springAiToolCallbackIsTraced() throws Exception {
        String log = run("spring", Map.of());
        assertEquals("\"" + ToolApp.RESULT + "\"", line(log, "RESULT="));
        Map<String, Object> tool = toolSpan(); assertToolShape(tool, log, "weather", null);
        Map<String, Object> attrs = attributes(tool);
        assertEquals(1, statusCode(tool));
        assertTrue(String.valueOf(attrs.get("gen_ai.tool.call.arguments")).contains(ToolApp.CITY), attrs.toString());
        assertEquals("\"" + ToolApp.RESULT + "\"", attrs.get("gen_ai.tool.call.result"));
        assertNoRawSecret();
        assertChatSpans(log, tool);
    }
    @Test void springAiToolFailureIsAnErrorSpanAndTheExceptionPropagatesUnchanged() throws Exception {
        String baseline = line(run("spring_error", Map.of("TRACEAI_TEST_NO_AGENT", "1")), "THROWN=");
        String log = run("spring_error", Map.of());
        assertEquals(baseline, line(log, "THROWN="), "the application sees the same exception");
        Map<String, Object> tool = toolSpan(); assertToolShape(tool, log, "weather", null);
        Map<String, Object> attrs = attributes(tool);
        assertEquals(2, statusCode(tool), "ERROR");
        assertEquals(IllegalStateException.class.getName(), attrs.get("error.type"));
        assertFalse(attrs.containsKey("gen_ai.tool.call.result"));
        assertNoRawSecret();
    }
    @ParameterizedTest @ValueSource(strings = {"lc4j", "lc4j_error", "spring", "spring_error"})
    void captureOffExportsNoToolContent(String scenario) throws Exception {
        String log = run(scenario, Map.of("FI_HIDE_INPUTS", "true", "FI_HIDE_OUTPUTS", "true"));
        Map<String, Object> tool = toolSpan();
        Map<String, Object> attrs = attributes(tool);
        String name = scenario.startsWith("lc4j") ? "lookup" : "weather";
        assertToolShape(tool, log, name, scenario.startsWith("lc4j") ? "call-1" : null);
        for (String key : List.of("gen_ai.tool.call.arguments", "gen_ai.tool.call.result", "input.value", "output.value", "input.mime_type", "output.mime_type"))
            assertFalse(attrs.containsKey(key), key + " is content: " + attrs);
        assertEquals("", WireCore.get(tool.get("status"), "message"), "error text is content too");
        String all = String.join("\n", payloads);
        for (String raw : List.of(ToolApp.CITY, ToolApp.RESULT, ToolApp.ERROR, ToolApp.SECRET)) assertFalse(all.contains(raw), raw + " exported with capture off");
        if (scenario.endsWith("_error")) assertEquals(IllegalStateException.class.getName(), attrs.get("error.type"));
    }
    @ParameterizedTest @ValueSource(strings = {"lc4j", "spring"})
    void hideInputTextKeepsResultsAndHideOutputTextKeepsArguments(String scenario) throws Exception {
        run(scenario, Map.of("FI_HIDE_INPUT_TEXT", "true"));
        Map<String, Object> attrs = attributes(toolSpan());
        assertFalse(attrs.containsKey("gen_ai.tool.call.arguments")); assertFalse(attrs.containsKey("input.value"));
        assertTrue(attrs.containsKey("gen_ai.tool.call.result"), attrs.toString()); assertTrue(attrs.containsKey("gen_ai.tool.name"));
        run(scenario, Map.of("FI_HIDE_OUTPUT_TEXT", "true"));
        attrs = attributes(toolSpan());
        assertTrue(attrs.containsKey("gen_ai.tool.call.arguments"), attrs.toString());
        assertFalse(attrs.containsKey("gen_ai.tool.call.result")); assertFalse(attrs.containsKey("output.value"));
    }
    /** CONTROL: excluding the framework (or running with no tool at all) leaves only the unchanged chat spans. */
    @ParameterizedTest @ValueSource(strings = {"lc4j", "spring"})
    void excludedFrameworkLeavesChatSpansUnchanged(String scenario) throws Exception {
        String log = run(scenario, Map.of("FI_DISABLED_INSTRUMENTATIONS", "langchain4j,spring-ai"));
        assertTrue(named("execute_tool").isEmpty(), spans.toString());
        assertChatSpans(log, null);
        log = run(scenario, Map.of("FI_INSTRUMENTATIONS", "wire"));
        assertTrue(named("execute_tool").isEmpty(), spans.toString());
        assertChatSpans(log, null);
    }
}
