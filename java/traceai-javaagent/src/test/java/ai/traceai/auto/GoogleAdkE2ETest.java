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

/** The packaged agent, in a child JVM, traces a real Google ADK for Java agent with no application code (AdkApp). */
class GoogleAdkE2ETest {
    static final String PROMPT = "SENTINEL-PROMPT-weather-in-Kyoto";
    static final String ANSWER = "SENTINEL-ANSWER-sunny";
    static final String TOOL_RESULT = "SENTINEL-TOOL-RESULT";

    /** ADK 1.0.0 ends the invocation span before the agent span it contains, so that span cannot carry what the agent span reports. */
    boolean adkVersion100;
    HttpServer server;
    ExecutorService executor;
    final java.util.concurrent.atomic.AtomicInteger modelCalls = new java.util.concurrent.atomic.AtomicInteger();
    final List<Map<String, Object>> spans = new CopyOnWriteArrayList<>();

    @BeforeEach void startServer() throws Exception {
        server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        server.createContext("/v1/chat/completions", exchange -> {
            exchange.getRequestBody().readAllBytes(); modelCalls.incrementAndGet();
            byte[] body = AgentE2ETest.BASIC.getBytes(StandardCharsets.UTF_8);
            exchange.getResponseHeaders().add("Content-Type", "application/json"); exchange.sendResponseHeaders(200, body.length);
            try (var out = exchange.getResponseBody()) { out.write(body); }
        });
        server.createContext("/tracer/v1/traces", exchange -> {
            Object root = WireCore.parse(new String(exchange.getRequestBody().readAllBytes(), StandardCharsets.UTF_8));
            for (Object resource : WireCore.list(WireCore.get(root, "resourceSpans")))
                for (Object scope : WireCore.list(WireCore.get(resource, "scopeSpans")))
                    for (Object span : WireCore.list(WireCore.get(scope, "spans"))) spans.add(WireCore.map(span));
            exchange.getResponseHeaders().add("Content-Type", "application/json"); exchange.sendResponseHeaders(200, 2);
            try (var out = exchange.getResponseBody()) { out.write("{}".getBytes(StandardCharsets.UTF_8)); }
        });
        executor = Executors.newFixedThreadPool(3); server.setExecutor(executor); server.start();
    }
    @AfterEach void stopServer() { server.stop(0); executor.shutdownNow(); }

    String run(String mode, String prompt, Map<String, String> options) throws Exception {
        Path agent = AgentE2ETest.agentJar(); assertTrue(Files.isRegularFile(agent));
        // The application is the google-adk module's test application (AdkApp) with ADK and its dependencies, which the
        // module's build lists; the agent's classes come only from the packaged jar.
        Path module = Path.of("..", "traceai-java-google-adk", "target").toAbsolutePath().normalize();
        Path classpath = module.resolve("adk-app.classpath");
        assertTrue(Files.isRegularFile(classpath), "build the traceai-java-google-adk module first (mvn -pl traceai-javaagent -am verify): " + classpath);
        String cp = module.resolve("test-classes") + File.pathSeparator + Files.readString(classpath).trim();
        adkVersion100 = cp.contains("google-adk-1.0.0.jar");
        Path output = Files.createTempFile(Path.of("target"), "adk-e2e-", ".log");
        List<String> command = new ArrayList<>(List.of(Path.of(System.getProperty("java.home"), "bin", "java").toString(), "-javaagent:" + agent,
            "-cp", cp, "ai.traceai.googleadk.AdkApp", mode, "http://127.0.0.1:" + server.getAddress().getPort(), prompt));
        ProcessBuilder pb = new ProcessBuilder(command).redirectErrorStream(true).redirectOutput(output.toFile());
        Map<String, String> env = pb.environment(); env.keySet().removeIf(k -> k.startsWith("FI_") || k.startsWith("OTEL_") || k.startsWith("ADK_"));
        env.put("FI_CONFIG_FILE", "target/no-e2e-config.toml"); env.put("FI_BASE_URL", "http://127.0.0.1:" + server.getAddress().getPort());
        env.put("FI_API_KEY", "test-api-key"); env.put("FI_SECRET_KEY", "test-secret-key");
        env.putAll(options);
        Process process = pb.start();
        if (!process.waitFor(60, TimeUnit.SECONDS)) { process.destroyForcibly(); fail("Child timed out: " + Files.readString(output)); }
        String log = Files.readString(output); assertEquals(0, process.exitValue(), log);
        assertTrue(log.contains("DONE"), log);
        return log;
    }

    static Map<String, Object> attributes(Map<String, Object> span) { return IncomingRequestsE2ETest.attributes(span); }
    Map<String, Object> only(String name) {
        List<Map<String, Object>> found = spans.stream().filter(s -> name.equals(s.get("name"))).toList();
        assertEquals(1, found.size(), name + " in " + spans);
        return found.get(0);
    }
    /** The tool span: ADK 1.0.0 names it "execute_tool [weather]", later versions "execute_tool weather". */
    Map<String, Object> tool() {
        List<Map<String, Object>> found = named("execute_tool");
        assertEquals(1, found.size(), spans.toString());
        assertTrue(String.valueOf(found.get(0).get("name")).matches("execute_tool \\[?weather]?"), String.valueOf(found.get(0).get("name")));
        return found.get(0);
    }
    List<Map<String, Object>> named(String prefix) { return spans.stream().filter(s -> String.valueOf(s.get("name")).startsWith(prefix)).toList(); }
    List<Map<String, Object>> byLayer(String layer) { return spans.stream().filter(s -> layer.equals(attributes(s).get("traceai.capture.layer"))).toList(); }

    @Test void invocationAgentModelAndToolCallsAreSpansWithTheirAttributes() throws Exception {
        run("plain", PROMPT, Map.of());
        Map<String, Object> invocation = only("invocation"), agent = only("invoke_agent weather_agent"), tool = tool();
        List<Map<String, Object>> llm = named("call_llm");
        assertEquals(2, llm.size(), spans.toString());
        assertEquals(5, spans.size(), "no other span, the wire spans of the model's HTTP calls included: " + spans);
        // One trace, the parent chain ADK made.
        assertNull(invocation.get("parentSpanId"));
        assertEquals(invocation.get("spanId"), agent.get("parentSpanId"));
        for (Map<String, Object> call : llm) assertEquals(agent.get("spanId"), call.get("parentSpanId"));
        assertEquals(llm.stream().map(s -> s.get("spanId")).collect(Collectors.toSet()).contains(tool.get("parentSpanId")), true, "the tool runs under the call that asked for it");
        for (Map<String, Object> span : spans) assertEquals(invocation.get("traceId"), span.get("traceId"));

        Map<String, Object> top = attributes(invocation);
        assertEquals("CHAIN", top.get("gen_ai.span.kind"));
        if (!adkVersion100) {
            assertEquals("user-1", top.get("user.id"));
            assertNotNull(top.get("session.id"));
            assertEquals(PROMPT, top.get("input.value"));
            assertEquals(ANSWER, top.get("output.value"));
        }
        Map<String, Object> a = attributes(agent);
        assertEquals("AGENT", a.get("gen_ai.span.kind"));
        assertEquals("weather_agent", a.get("gen_ai.agent.name"));
        assertEquals("user-1", a.get("user.id"));
        assertNotNull(a.get("session.id"));
        assertEquals(PROMPT, a.get("input.value"));
        assertEquals(ANSWER, a.get("output.value"));

        Map<String, Object> second = llm.stream().map(GoogleAdkE2ETest::attributes).filter(x -> x.containsKey("gen_ai.input.messages.3.message.role")).findFirst().orElseThrow();
        assertEquals("LLM", second.get("gen_ai.span.kind"));
        assertEquals("gemini-2.0-flash", second.get("gen_ai.request.model"));
        assertEquals("system", second.get("gen_ai.input.messages.0.message.role"));
        assertEquals(PROMPT, second.get("gen_ai.input.messages.1.message.content"));
        assertEquals("weather", second.get("gen_ai.input.messages.2.message.tool_calls.0.tool_call.function.name"));
        assertEquals("tool", second.get("gen_ai.input.messages.3.message.role"));
        assertEquals(ANSWER, second.get("gen_ai.output.messages.0.message.content"));
        assertEquals("11", String.valueOf(second.get("gen_ai.usage.input_tokens")));
        assertEquals("7", String.valueOf(second.get("gen_ai.usage.output_tokens")));
        assertTrue(String.valueOf(second.get("gen_ai.tool.definitions.0.tool.json_schema")).contains("weather"));
        Map<String, Object> first = llm.stream().map(GoogleAdkE2ETest::attributes).filter(x -> !x.containsKey("gen_ai.input.messages.3.message.role")).findFirst().orElseThrow();
        assertEquals("call-1", first.get("gen_ai.output.messages.0.message.tool_calls.0.tool_call.id"));

        Map<String, Object> t = attributes(tool);
        assertEquals("TOOL", t.get("gen_ai.span.kind"));
        assertEquals("weather", t.get("gen_ai.tool.name"));
        assertEquals("call-1", t.get("gen_ai.tool.call.id"));
        assertTrue(String.valueOf(t.get("gen_ai.tool.call.arguments")).contains("Kyoto"));
        assertTrue(String.valueOf(t.get("gen_ai.tool.call.result")).contains(TOOL_RESULT));
    }

    @Test void theModelsHttpCallIsNotTracedASecondTime() throws Exception {
        run("plain", PROMPT, Map.of());
        assertEquals(2, modelCalls.get(), "the model stub made its two HTTP calls");
        assertEquals(List.of(), byLayer("wire"), "the wire capture stays out of an ADK model call: " + spans);
        assertTrue(spans.stream().noneMatch(s -> String.valueOf(s.get("name")).startsWith("chat ")), spans.toString());
    }

    @Test void disablingGoogleAdkLeavesOnlyTheWireSpans() throws Exception {
        String log = run("plain", PROMPT, Map.of("FI_DISABLED_INSTRUMENTATIONS", "google-adk"));
        assertEquals(List.of(), byLayer("google-adk"), spans.toString());
        assertEquals(2, byLayer("wire").size(), "each HTTP call is a wire span again: " + spans);
        assertTrue(log.contains("\"google-adk\":\"disabled\""), log);
    }

    @Test void hideInputsAndOutputsKeepContentOutOfEverySpan() throws Exception {
        run("plain", PROMPT, Map.of("FI_HIDE_INPUTS", "true", "FI_HIDE_OUTPUTS", "true"));
        assertEquals(5, spans.size(), spans.toString());
        String everything = spans.toString();
        for (String content : List.of(PROMPT, ANSWER, TOOL_RESULT, "Kyoto")) assertFalse(everything.contains(content), content + " in " + everything);
        assertEquals("11", String.valueOf(attributes(named("call_llm").get(0)).get("gen_ai.usage.input_tokens")), "counts stay");
    }

    @Test void hideInputsAloneKeepsThePromptAndToolArgumentsOut() throws Exception {
        run("plain", PROMPT, Map.of("FI_HIDE_INPUTS", "true"));
        String everything = spans.toString();
        assertFalse(everything.contains(PROMPT), everything);
        for (Map<String, Object> span : spans) {
            assertFalse(attributes(span).containsKey("gen_ai.tool.call.arguments"), "tool arguments are input: " + span);
            assertFalse(attributes(span).keySet().stream().anyMatch(k -> k.startsWith("gen_ai.input.")), "input messages: " + span);
        }
        assertTrue(everything.contains(ANSWER), "outputs are still recorded: " + everything);
    }

    @Test void aKnownSecretInThePromptIsReplaced() throws Exception {
        run("plain", PROMPT + " key=test-secret-key", Map.of());
        assertFalse(spans.toString().contains("test-secret-key"), spans.toString());
        assertTrue(spans.toString().contains(PROMPT), spans.toString());
    }

    @Test void anApplicationWithItsOwnSdkKeepsItsSpansAndGetsTheAgentsToo() throws Exception {
        String log = run("sdk", PROMPT, Map.of());
        // The application's provider recorded ADK's own spans, unchanged and still linked.
        for (String name : List.of("invocation", "invoke_agent weather_agent", "call_llm", "execute_tool"))
            assertTrue(log.lines().anyMatch(l -> l.startsWith("OWN " + name + " ") || name.equals("execute_tool") && l.startsWith("OWN execute_tool ")), name + " in " + log);
        assertTrue(log.lines().filter(l -> l.startsWith("OWN ")).anyMatch(l -> l.contains("parent=true")), log);
        assertEquals(5, spans.size(), "the agent's own spans reach Future AGI as well: " + spans);
        assertEquals(List.of(), byLayer("wire"));
    }

    @Test void doctorListsTheIntegration() throws Exception {
        String log = run("plain", PROMPT, Map.of());
        assertTrue(log.lines().anyMatch(l -> l.startsWith("DOCTOR ") && l.contains("\"google-adk\":\"instrumented\"")), log);
    }
}
