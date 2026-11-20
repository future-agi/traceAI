package ai.traceai.auto;

import com.sun.net.httpserver.HttpServer;
import java.io.File;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.nio.file.*;
import java.util.*;
import java.util.concurrent.*;
import org.junit.jupiter.api.*;
import static org.junit.jupiter.api.Assertions.*;

/** Annotated application methods become typed spans, with the agent attached and with the one-line initialization. */
class AnnotationsE2ETest {
    static final String APP = "ai.traceai.apptest.AnnotatedApp";
    HttpServer server;
    ExecutorService executor;
    final List<Map<String, Object>> spans = new CopyOnWriteArrayList<>();

    @BeforeEach void startCollector() throws Exception {
        server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        server.createContext("/tracer/v1/traces", exchange -> {
            Object root = WireCore.parse(new String(exchange.getRequestBody().readAllBytes(), StandardCharsets.UTF_8));
            for (Object resource : WireCore.list(WireCore.get(root, "resourceSpans")))
                for (Object scope : WireCore.list(WireCore.get(resource, "scopeSpans")))
                    for (Object span : WireCore.list(WireCore.get(scope, "spans"))) spans.add(WireCore.map(span));
            exchange.getResponseHeaders().add("Content-Type", "application/json"); exchange.sendResponseHeaders(200, 2);
            exchange.getResponseBody().write("{}".getBytes(StandardCharsets.UTF_8)); exchange.close();
        });
        executor = Executors.newFixedThreadPool(2); server.setExecutor(executor); server.start();
    }
    @AfterEach void stopCollector() { server.stop(0); executor.shutdownNow(); }

    /** mode: agent (-javaagent), init (agent jar on the class path, initialization first), none (no agent at all). */
    String run(String mode, String scenario, Map<String, String> env) throws Exception {
        Path agent = AgentE2ETest.agentJar();
        String cp = System.getProperty("surefire.test.class.path", System.getProperty("java.class.path"));
        String classes = Path.of("target", "classes").toAbsolutePath().normalize().toString();
        List<String> app = new ArrayList<>(Arrays.stream(cp.split(java.util.regex.Pattern.quote(File.pathSeparator)))
            .filter(p -> !Path.of(p).toAbsolutePath().normalize().toString().equals(classes)).toList());
        List<String> command = new ArrayList<>();
        command.add(Path.of(System.getProperty("traceai.test.java.home", System.getProperty("java.home")), "bin", "java").toString());
        if (mode.equals("agent")) command.add("-javaagent:" + agent);
        if (mode.equals("init")) {
            // Only what an application using the agent jar as a library has: the jar, its own classes, the annotations, Reactor.
            app = new ArrayList<>(app.stream().filter(p -> {
                String name = Path.of(p).getFileName().toString();
                return name.equals("test-classes") || name.startsWith("traceai-java-annotations") || name.startsWith("reactor-core") || name.startsWith("reactive-streams");
            }).toList());
            app.add(0, agent.toString());
        }
        command.addAll(List.of("-XX:+EnableDynamicAgentLoading", "-cp", String.join(File.pathSeparator, app), APP, scenario));
        Path output = Files.createTempFile(Path.of("target"), "annotations-e2e-", ".log");
        ProcessBuilder pb = new ProcessBuilder(command).redirectErrorStream(true).redirectOutput(output.toFile());
        Map<String, String> environment = pb.environment(); environment.keySet().removeIf(k -> k.startsWith("FI_") || k.startsWith("OTEL_"));
        environment.put("FI_CONFIG_FILE", "target/no-e2e-config.toml"); environment.put("FI_BASE_URL", "http://127.0.0.1:" + server.getAddress().getPort());
        environment.put("FI_API_KEY", "test-api-key"); environment.put("FI_SECRET_KEY", "test-secret-key");
        environment.putAll(env);
        Process process = pb.start();
        if (!process.waitFor(40, TimeUnit.SECONDS)) { process.destroyForcibly(); fail("Child timed out: " + Files.readString(output)); }
        String log = Files.readString(output); assertEquals(0, process.exitValue(), log);
        assertTrue(log.contains("DONE"), log);
        long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(5); // the final export can trail the child's exit by a moment
        while (spans.isEmpty() && !mode.equals("none") && System.nanoTime() < deadline) Thread.sleep(50);
        Thread.sleep(300);
        return log;
    }

    static Map<String, Object> attrs(Map<String, Object> span) {
        Map<String, Object> out = new LinkedHashMap<>();
        for (Object a : WireCore.list(span.get("attributes"))) {
            Map<String, Object> value = WireCore.map(WireCore.get(a, "value"));
            out.put(String.valueOf(WireCore.get(a, "key")), value.values().iterator().next());
        }
        return out;
    }
    Map<String, Object> span(String name) {
        List<Map<String, Object>> found = spans.stream().filter(s -> name.equals(s.get("name"))).toList();
        assertEquals(1, found.size(), "exactly one span named " + name + " in " + spans.stream().map(s -> s.get("name")).toList());
        return found.get(0);
    }
    Map<String, Object> parentOf(Map<String, Object> span) {
        Object parent = span.get("parentSpanId");
        return spans.stream().filter(s -> s.get("spanId").equals(parent)).findFirst().orElse(null);
    }
    static Object json(Object text) { return WireCore.parse(String.valueOf(text)); }
    static List<String> names(List<Map<String, Object>> spans) { return spans.stream().map(s -> String.valueOf(s.get("name"))).sorted().toList(); }

    void assertSyncSpans(String log) {
        assertTrue(log.contains("RESULT=docs1|The answer is 4|"), log);
        assertEquals(List.of("calculator", "count", "fetch_docs", "generate", "noArguments", "planner"), names(spans));
        Map<String, Object> planner = span("planner"), tool = span("calculator"), llm = span("generate"), fetch = span("fetch_docs");
        assertEquals("AGENT", attrs(planner).get("fi.span.kind")); assertEquals("agent", attrs(planner).get("gen_ai.span.kind"));
        assertEquals("TOOL", attrs(tool).get("fi.span.kind")); assertEquals("LLM", attrs(llm).get("fi.span.kind"));
        assertEquals("RETRIEVER", attrs(fetch).get("fi.span.kind"));
        assertEquals("CHAIN", attrs(span("count")).get("fi.span.kind"));
        assertNull(planner.get("parentSpanId"), "the outermost annotated call is a root");
        for (Map<String, Object> child : List.of(tool, llm, fetch)) assertSame(planner, parentOf(child), "nested annotated calls parent under the current span");
        assertEquals("calculator", attrs(tool).get("gen_ai.tool.name")); assertEquals("calculator", attrs(tool).get("tool.name"));
        assertEquals("adds numbers", attrs(tool).get("gen_ai.tool.description"));
        // input.value: JSON of the non-excluded arguments, keyed by parameter name; the excluded one is absent everywhere.
        assertEquals(Map.of("question", "what is 2+2?"), json(attrs(planner).get("input.value")));
        assertEquals("application/json", attrs(planner).get("input.mime_type"));
        assertEquals("docs1|The answer is 4|" + AnnotatedAppSecret.LENGTH, json(attrs(planner).get("output.value")));
        assertEquals(Map.of("expression", "what is 2+2?"), json(attrs(tool).get("input.value")));
        assertEquals(Map.of("terms", List.of("what is 2+2?")), json(attrs(fetch).get("input.value")));
        assertEquals(Map.of("n", 41), json(attrs(span("count")).get("input.value")));
        assertEquals(42, ((Number)json(attrs(span("count")).get("output.value"))).intValue());
        assertNull(attrs(span("noArguments")).get("input.value")); assertNull(attrs(span("noArguments")).get("output.value"));
        for (Map<String, Object> s : spans) {
            assertFalse(String.valueOf(s.get("attributes")).contains(AnnotatedAppSecret.VALUE), "excluded argument must not be exported");
            assertNotEquals(2, ((Number)WireCore.map(s.get("status")).get("code")).intValue(), s.get("name") + " must not be an error span");
        }
    }

    @Test void agentAttachedAnnotatedMethodsAreTraced() throws Exception { assertSyncSpans(run("agent", "sync", Map.of())); }
    @Test void oneLineInitializationTracesWithoutTheAgentFlag() throws Exception { assertSyncSpans(run("init", "init_first", Map.of())); }
    @Test void aClassLoadedBeforeInitializationIsStillTraced() throws Exception {
        run("init", "late_class_load", Map.of());
        assertEquals(List.of("calculator", "fetch_docs", "generate", "planner"), names(spans));
    }
    @Test void callingTheInstallerWithTheAgentAttachedDoesNotDoubleSpans() throws Exception {
        run("agent", "init_twice", Map.of());
        assertEquals(List.of("calculator", "count", "fetch_docs", "generate", "noArguments", "planner"), names(spans));
    }
    @Test void exceptionsAreRecordedAndRethrownUnchanged() throws Exception {
        String log = run("agent", "error", Map.of());
        assertTrue(log.contains("CAUGHT=bad x " + AnnotatedAppSecret.VALUE + ";same=true"), log);
        Map<String, Object> boom = span("boom");
        assertEquals(2, ((Number)WireCore.map(boom.get("status")).get("code")).intValue(), "ERROR status");
        assertEquals("java.lang.IllegalStateException", attrs(boom).get("error.type"));
        assertFalse(WireCore.list(boom.get("events")).isEmpty(), "the exception is recorded as an event");
    }
    @Test void aThrowingMethodWithErrorDetailsHiddenRecordsOnlyTheType() throws Exception {
        run("agent", "error", Map.of("FI_HIDE_ERROR_DETAILS", "true"));
        Map<String, Object> boom = span("boom");
        assertEquals("", WireCore.map(boom.get("status")).get("message"));
        assertFalse(String.valueOf(boom).contains("bad x"), "no exception message when error details are hidden");
    }
    @Test void completableFutureSpansEndOnCompletion() throws Exception {
        run("agent", "async", Map.of());
        Map<String, Object> later = span("later");
        long start = Long.parseLong((String)later.get("startTimeUnixNano")), end = Long.parseLong((String)later.get("endTimeUnixNano"));
        assertTrue(end - start >= 250_000_000L, "the span covers the asynchronous work, not just the method call: " + (end - start));
        assertEquals("later:q", json(attrs(later).get("output.value")));
    }
    @Test void aFailedCompletableFutureIsAnErrorSpan() throws Exception {
        run("agent", "async_error", Map.of());
        Map<String, Object> failed = span("laterFails");
        assertEquals(2, ((Number)WireCore.map(failed.get("status")).get("code")).intValue());
    }
    @Test void reactorMonoAndFluxSpansEndOnTermination() throws Exception {
        run("agent", "mono", Map.of());
        Map<String, Object> mono = span("mono");
        assertTrue(Long.parseLong((String)mono.get("endTimeUnixNano")) - Long.parseLong((String)mono.get("startTimeUnixNano")) >= 150_000_000L);
        assertEquals("mono:m", json(attrs(mono).get("output.value")));
        spans.clear();
        run("agent", "flux", Map.of());
        assertEquals(List.of(1, 2, 3), json(attrs(span("flux")).get("output.value")));
    }
    @Test void hideInputsDropsInputValueButKeepsTheToolName() throws Exception {
        run("agent", "sync", Map.of("FI_HIDE_INPUTS", "true"));
        for (Map<String, Object> s : spans) { assertNull(attrs(s).get("input.value"), s.get("name") + " input"); assertNotNull(s.get("name")); }
        assertNotNull(attrs(span("planner")).get("output.value"), "outputs are still recorded");
        assertEquals("calculator", attrs(span("calculator")).get("gen_ai.tool.name"), "the tool name is not withheld");
    }
    @Test void hideOutputsDropsOutputValue() throws Exception {
        run("agent", "sync", Map.of("FI_HIDE_OUTPUTS", "true"));
        for (Map<String, Object> s : spans) assertNull(attrs(s).get("output.value"), s.get("name") + " output");
        assertNotNull(attrs(span("planner")).get("input.value"));
    }
    @Test void aKnownSecretInAnArgumentIsReplacedOnExport() throws Exception {
        run("agent", "secret", Map.of("FI_API_KEY", AnnotatedAppSecret.KEY));
        // The application passes its own registered key as the question; the export never carries it.
        assertFalse(spans.isEmpty());
        assertFalse(spans.toString().contains(AnnotatedAppSecret.KEY), "the registered key is replaced in every exported span");
        assertNotNull(attrs(span("planner")).get("input.value"), "the span is still exported");
    }
    @Test void withoutTheAgentTheApplicationRunsAndNothingIsExported() throws Exception {
        String log = run("none", "sync", Map.of());
        assertTrue(log.contains("RESULT=docs1|The answer is 4|"), log);
        assertTrue(spans.isEmpty());
    }
}
