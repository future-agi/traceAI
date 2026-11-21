package ai.traceai.auto;

import com.sun.net.httpserver.HttpServer;
import java.io.File;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.nio.file.*;
import java.util.*;
import java.util.concurrent.*;
import java.util.stream.Collectors;
import org.junit.jupiter.api.*;
import static org.junit.jupiter.api.Assertions.*;

/**
 * The agent in a child JVM traces an application whose annotated {@code @Agent} method runs a real Google ADK for Java agent
 * (AnnotatedAdkApp), so annotation spans and ADK spans share one trace and one parent chain.
 */
class AnnotationsWithGoogleAdkE2ETest {
    static final String PROMPT = GoogleAdkE2ETest.PROMPT;
    static final String KEY = "fi-key-0123456789abcdef";
    HttpServer server;
    ExecutorService executor;
    final List<Map<String, Object>> spans = new CopyOnWriteArrayList<>();
    /** Every export request body as the collector received it. */
    final List<String> exported = new CopyOnWriteArrayList<>();

    @BeforeEach void startCollector() throws Exception {
        server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        server.createContext("/v1/chat/completions", exchange -> {
            exchange.getRequestBody().readAllBytes();
            byte[] body = AgentE2ETest.BASIC.getBytes(StandardCharsets.UTF_8);
            exchange.getResponseHeaders().add("Content-Type", "application/json"); exchange.sendResponseHeaders(200, body.length);
            try (var out = exchange.getResponseBody()) { out.write(body); }
        });
        server.createContext("/tracer/v1/traces", exchange -> {
            String payload = new String(exchange.getRequestBody().readAllBytes(), StandardCharsets.UTF_8); exported.add(payload);
            Object root = WireCore.parse(payload);
            for (Object resource : WireCore.list(WireCore.get(root, "resourceSpans")))
                for (Object scope : WireCore.list(WireCore.get(resource, "scopeSpans")))
                    for (Object span : WireCore.list(WireCore.get(scope, "spans"))) spans.add(WireCore.map(span));
            exchange.getResponseHeaders().add("Content-Type", "application/json"); exchange.sendResponseHeaders(200, 2);
            try (var out = exchange.getResponseBody()) { out.write("{}".getBytes(StandardCharsets.UTF_8)); }
        });
        executor = Executors.newFixedThreadPool(3); server.setExecutor(executor); server.start();
    }
    @AfterEach void stopCollector() { server.stop(0); executor.shutdownNow(); }

    /** mode: "plain" or "sdk" (the application installs its own OpenTelemetry SDK, as AdkApp does). */
    String run(String mode, String prompt, Map<String, String> options) throws Exception { return run(mode, prompt, options, "single"); }

    String run(String mode, String prompt, Map<String, String> options, String scenario) throws Exception {
        Path agent = AgentE2ETest.agentJar(); assertTrue(Files.isRegularFile(agent));
        Path module = Path.of("..", "traceai-java-google-adk", "target").toAbsolutePath().normalize();
        Path classpath = module.resolve("adk-app.classpath");
        assertTrue(Files.isRegularFile(classpath), "build the traceai-java-google-adk module first (mvn -pl traceai-javaagent -am verify): " + classpath);
        String testCp = System.getProperty("surefire.test.class.path", System.getProperty("java.class.path"));
        List<String> mine = new ArrayList<>();
        mine.add(Path.of("target", "test-classes").toAbsolutePath().toString());
        for (String p : testCp.split(java.util.regex.Pattern.quote(File.pathSeparator)))
            if (p.contains("traceai-java-annotations")) mine.add(p);
        assertTrue(mine.size() > 1, "the annotations module is on the test class path: " + testCp);
        String cp = String.join(File.pathSeparator, mine) + File.pathSeparator + module.resolve("test-classes") + File.pathSeparator + Files.readString(classpath).trim();
        Path output = Files.createTempFile(Path.of("target"), "annotations-adk-e2e-", ".log");
        List<String> command = new ArrayList<>(List.of(Path.of(System.getProperty("java.home"), "bin", "java").toString(), "-javaagent:" + agent,
            "-cp", cp, "ai.traceai.apptest.AnnotatedAdkApp", mode, "http://127.0.0.1:" + server.getAddress().getPort(), prompt, "subscribe", scenario));
        ProcessBuilder pb = new ProcessBuilder(command).redirectErrorStream(true).redirectOutput(output.toFile());
        Map<String, String> env = pb.environment(); env.keySet().removeIf(k -> k.startsWith("FI_") || k.startsWith("OTEL_") || k.startsWith("ADK_"));
        env.put("FI_CONFIG_FILE", "target/no-e2e-config.toml"); env.put("FI_BASE_URL", "http://127.0.0.1:" + server.getAddress().getPort());
        env.put("FI_API_KEY", "test-api-key"); env.put("FI_SECRET_KEY", "test-secret-key");
        env.putAll(options);
        Process process = pb.start();
        if (!process.waitFor(60, TimeUnit.SECONDS)) { process.destroyForcibly(); fail("Child timed out: " + Files.readString(output)); }
        String log = Files.readString(output); assertEquals(0, process.exitValue(), log);
        assertTrue(log.contains("DONE"), log);
        assertTrue(log.contains("RESULT=planned"), log);
        return log;
    }

    static Map<String, Object> attributes(Map<String, Object> span) { return AnnotationsE2ETest.attrs(span); }
    List<Map<String, Object>> named(String prefix) { return spans.stream().filter(s -> String.valueOf(s.get("name")).startsWith(prefix)).toList(); }
    Map<String, Object> only(String name) {
        List<Map<String, Object>> found = named(name);
        assertEquals(1, found.size(), name + " in " + spans.stream().map(s -> s.get("name")).toList());
        return found.get(0);
    }
    List<Map<String, Object>> byLayer(String layer) { return spans.stream().filter(s -> layer.equals(attributes(s).get("traceai.capture.layer"))).toList(); }

    /** The one parent chain: planner (annotated) > invocation > agent > model calls > tool, all in one trace, each call once. */
    void assertOneChain() {
        Map<String, Object> planner = only("planner"), invocation = only("invocation"), agent = only("invoke_agent weather_agent");
        List<Map<String, Object>> llm = named("call_llm"), tools = named("execute_tool");
        assertEquals(2, llm.size(), spans.toString());
        assertEquals(1, tools.size(), spans.toString());
        assertEquals(6, spans.size(), "no other span, a wire span of the model's HTTP calls or a second span for any call included: " + spans.stream().map(s -> s.get("name")).toList());
        assertNull(planner.get("parentSpanId"), "the annotated method is the root");
        assertEquals(planner.get("spanId"), invocation.get("parentSpanId"), "the ADK invocation nests under the annotated span");
        assertEquals(invocation.get("spanId"), agent.get("parentSpanId"), "the ADK agent run nests under the invocation");
        for (Map<String, Object> call : llm) assertEquals(agent.get("spanId"), call.get("parentSpanId"), "a model call nests under the ADK agent span");
        Set<Object> llmIds = llm.stream().map(s -> s.get("spanId")).collect(Collectors.toSet());
        assertTrue(llmIds.contains(tools.get(0).get("parentSpanId")) || agent.get("spanId").equals(tools.get(0).get("parentSpanId")), "the tool runs under the ADK agent's model call or the agent");
        for (Map<String, Object> span : spans) assertEquals(planner.get("traceId"), span.get("traceId"), "one trace");
        assertEquals(spans.size(), spans.stream().map(s -> s.get("spanId")).distinct().count());
        assertEquals("AGENT", attributes(planner).get("fi.span.kind"));
        assertEquals("AGENT", attributes(agent).get("gen_ai.span.kind"));
        assertEquals(List.of("google-adk"), byLayer("google-adk").stream().map(s -> "google-adk").distinct().toList());
        assertEquals(5, byLayer("google-adk").size(), "every ADK span is the agent's google-adk layer span: " + spans);
    }

    @Test void anAdkAgentRunInsideAnAnnotatedAgentMethodNestsUnderItWithoutAnApplicationSdk() throws Exception {
        run("plain", PROMPT, Map.of());
        assertOneChain();
    }

    @Test void theSameNestingHoldsWhenTheApplicationHasItsOwnOpenTelemetrySdk() throws Exception {
        String log = run("sdk", PROMPT, Map.of());
        assertOneChain();
        // The application's provider still records ADK's own spans, as it does without annotations.
        assertTrue(log.lines().anyMatch(l -> l.startsWith("OWN invocation ")), log);
        assertTrue(log.lines().anyMatch(l -> l.startsWith("OWN invoke_agent weather_agent ")), log);
    }

    @Test void aToolsOwnLlmCallInsideTheAnnotatedMethodIsOneWireSpanUnderTheAdkToolSpanNotTheAnnotatedSpan() throws Exception {
        for (String mode : List.of("plain", "sdk")) {
            spans.clear(); exported.clear();
            run(mode, PROMPT, Map.of(), "tool-call");
            Map<String, Object> planner = only("planner");
            // The scenario also runs an unrelated session on a thread of its own: its spans are a trace of their own.
            List<Map<String, Object>> invocations = named("invocation").stream().filter(s -> planner.get("spanId").equals(s.get("parentSpanId"))).toList();
            assertEquals(1, invocations.size(), mode + ": the ADK invocation of the annotated method nests under it: " + spans.stream().map(s -> s.get("name")).toList());
            List<Map<String, Object>> wire = byLayer("wire");
            assertEquals(1, wire.size(), mode + ": exactly the tool's own HTTP call is a wire span: " + spans.stream().map(s -> s.get("name")).toList());
            Map<String, Object> tool = named("execute_tool").get(0);
            // The ADK span is the parent, not the annotated span the ADK agent itself runs under: one chain, planner > ADK spans > wire.
            Object parent = wire.get(0).get("parentSpanId");
            assertNotEquals(planner.get("spanId"), parent, mode + ": the ADK span wins inside the annotated method");
            assertTrue(parent.equals(tool.get("spanId")) || parent.equals(tool.get("parentSpanId")), mode + ": under the tool's own span or the model call that asked for it");
            assertEquals(wire.get(0).get("traceId"), planner.get("traceId"), mode + ": one trace");
        }
    }

    @Test void disablingAnnotationsLeavesTheAdkSpansAsTheyAre() throws Exception {
        String log = run("plain", PROMPT, Map.of("FI_DISABLED_INSTRUMENTATIONS", "annotations"));
        assertEquals(List.of(), named("planner"), spans.toString());
        assertEquals(5, spans.size(), spans.stream().map(s -> s.get("name")).toList().toString());
        assertEquals(5, byLayer("google-adk").size(), spans.toString());
        Map<String, Object> invocation = only("invocation"), agent = only("invoke_agent weather_agent");
        assertNull(invocation.get("parentSpanId"));
        assertEquals(invocation.get("spanId"), agent.get("parentSpanId"));
        for (Map<String, Object> call : named("call_llm")) assertEquals(agent.get("spanId"), call.get("parentSpanId"));
        assertTrue(log.contains("\"google-adk\":\"instrumented\""), log);
        assertTrue(log.contains("\"annotations\":\"disabled\""), log);
    }

    @Test void disablingGoogleAdkLeavesTheAnnotationSpansAsTheyAre() throws Exception {
        String log = run("plain", PROMPT, Map.of("FI_DISABLED_INSTRUMENTATIONS", "google-adk"));
        assertEquals(List.of(), byLayer("google-adk"), spans.toString());
        Map<String, Object> planner = only("planner");
        assertNull(planner.get("parentSpanId"));
        assertEquals("AGENT", attributes(planner).get("fi.span.kind"));
        assertNotNull(attributes(planner).get("input.value"));
        assertEquals("\"planned\"", String.valueOf(attributes(planner).get("output.value")));
        // Nothing of ADK is a span any more; the model's HTTP calls are the wire spans, under the annotated span.
        assertFalse(named("invocation").size() > 0 || named("invoke_agent").size() > 0, spans.toString());
        List<Map<String, Object>> wire = byLayer("wire");
        assertFalse(wire.isEmpty(), spans.toString());
        for (Map<String, Object> span : wire) assertEquals(planner.get("spanId"), span.get("parentSpanId"), "a wire span is a child of the annotated span");
        assertTrue(log.contains("\"google-adk\":\"disabled\""), log);
        assertTrue(log.contains("\"annotations\":\"instrumented\""), log);
    }

    @Test void aKnownSecretIsReplacedOnTheAnnotationSpanAndTheAdkSpansOnTheKeyTransportRoute() throws Exception {
        // The application passes its own registered key as the annotated method's argument, and ADK runs with it as the prompt.
        String log = run("plain", KEY, Map.of("FI_API_KEY", KEY));
        Map<String, Object> planner = only("planner");
        assertNotNull(attributes(planner).get("input.value"), "the annotation span is still exported");
        assertFalse(exported.isEmpty());
        for (String body : exported) assertFalse(body.contains(KEY), "the registered key reaches the collector in no span, attribute or event");
        assertFalse(spans.toString().contains(KEY));
        assertEquals(6, spans.size(), spans.stream().map(s -> s.get("name")).toList().toString());
        assertTrue(log.lines().filter(l -> l.startsWith("DOCTOR ")).noneMatch(l -> l.contains(KEY)), "the doctor report names no key");
    }
}
