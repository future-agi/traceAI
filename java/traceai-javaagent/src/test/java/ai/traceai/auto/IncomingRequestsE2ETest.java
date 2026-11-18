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

/** The packaged agent, in a child JVM, joins the caller's trace on incoming servlet requests (ServletApp). */
class IncomingRequestsE2ETest {
    static final String TRACE_1 = "4bf92f3577b34da6a3ce929d0e0e4736", PARENT_1 = "00f067aa0ba902b7";
    static final String TRACE_2 = "0af7651916cd43dd8448eb211c80319c", PARENT_2 = "b7ad6b7169203331";
    static final String SAMPLED_1 = "00-" + TRACE_1 + "-" + PARENT_1 + "-01", SAMPLED_2 = "00-" + TRACE_2 + "-" + PARENT_2 + "-01";
    static final String UNSAMPLED_1 = "00-" + TRACE_1 + "-" + PARENT_1 + "-00";

    HttpServer server;
    ExecutorService executor;
    final java.util.concurrent.atomic.AtomicInteger calls = new java.util.concurrent.atomic.AtomicInteger();
    final List<Map<String, Object>> spans = new CopyOnWriteArrayList<>();

    @BeforeEach void startServer() throws Exception {
        server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        server.createContext("/v1/chat/completions", exchange -> {
            exchange.getRequestBody().readAllBytes(); calls.incrementAndGet();
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

    String run(String container, Map<String, String> options, String... requests) throws Exception {
        Path agent = AgentE2ETest.agentJar(); assertTrue(Files.isRegularFile(agent));
        String cp = System.getProperty("surefire.test.class.path", System.getProperty("java.class.path"));
        String classes = Path.of("target", "classes").toAbsolutePath().normalize().toString();
        // The agent's classes come only from the packaged jar.
        cp = Arrays.stream(cp.split(java.util.regex.Pattern.quote(File.pathSeparator)))
            .filter(p -> !Path.of(p).toAbsolutePath().normalize().toString().equals(classes)).collect(Collectors.joining(File.pathSeparator));
        Path output = Files.createTempFile(Path.of("target"), "incoming-e2e-", ".log");
        List<String> command = new ArrayList<>(List.of(Path.of(System.getProperty("java.home"), "bin", "java").toString(), "-javaagent:" + agent,
            "-cp", cp, ServletApp.class.getName(), container, "http://127.0.0.1:" + server.getAddress().getPort() + "/v1/chat/completions"));
        command.addAll(List.of(requests));
        ProcessBuilder pb = new ProcessBuilder(command).redirectErrorStream(true).redirectOutput(output.toFile());
        Map<String, String> env = pb.environment(); env.keySet().removeIf(k -> k.startsWith("FI_") || k.startsWith("OTEL_"));
        env.put("FI_CONFIG_FILE", "target/no-e2e-config.toml"); env.put("FI_BASE_URL", "http://127.0.0.1:" + server.getAddress().getPort());
        env.put("FI_API_KEY", "test-api-key"); env.put("FI_SECRET_KEY", "test-secret-key");
        env.putAll(options);
        Process process = pb.start();
        if (!process.waitFor(40, TimeUnit.SECONDS)) { process.destroyForcibly(); fail("Child timed out: " + Files.readString(output)); }
        String log = Files.readString(output); assertEquals(0, process.exitValue(), log);
        assertTrue(log.contains("DONE"), log);
        assertEquals(requests.length, calls.get(), "every request made its AI call: " + log);
        return log;
    }
    /** Exported spans in the order their calls were made. */
    List<Map<String, Object>> ordered() {
        List<Map<String, Object>> result = new ArrayList<>(spans);
        result.sort(Comparator.comparing(span -> Long.parseLong(String.valueOf(span.get("startTimeUnixNano")))));
        return result;
    }
    static Set<String> threads(String log) {
        return log.lines().filter(line -> line.startsWith("CALL ")).map(line -> line.replaceAll(".*THREAD=(.*) STATUS=.*", "$1")).collect(Collectors.toSet());
    }
    static void assertJoined(Map<String, Object> span, String trace, String parent) {
        assertEquals(trace, span.get("traceId"), span.toString());
        assertEquals(parent, span.get("parentSpanId"), span.toString());
    }
    static void assertNewRoot(Map<String, Object> span, String... foreign) {
        assertNull(span.get("parentSpanId"), span.toString());
        String trace = String.valueOf(span.get("traceId"));
        assertEquals(32, trace.length());
        for (String other : foreign) assertNotEquals(other, trace, span.toString());
    }
    static Map<String, Object> attributes(Map<String, Object> span) {
        Map<String, Object> attrs = new LinkedHashMap<>();
        for (Object raw : WireCore.list(span.get("attributes"))) {
            Map<String, Object> value = WireCore.map(WireCore.get(raw, "value"));
            attrs.put(WireCore.str(WireCore.get(raw, "key")), WireCore.or(value.get("stringValue"), value.get("intValue"), value.get("boolValue"), value.get("doubleValue")));
        }
        return attrs;
    }

    @Test void sampledTraceparentJoinsTheCallersTrace() throws Exception {
        run("tomcat", Map.of(), "/call|" + SAMPLED_1 + "|-");
        assertEquals(1, spans.size(), spans.toString());
        assertJoined(spans.get(0), TRACE_1, PARENT_1);
        assertEquals("chat test-model", spans.get(0).get("name"));
    }

    @Test void unsampledTraceparentExportsNothing() throws Exception {
        // Exactly one AI call is made (run() checks); the default parent-based sampler drops it.
        run("tomcat", Map.of(), "/call|" + UNSAMPLED_1 + "|-");
        assertEquals(List.of(), spans);
    }

    @Test void noTraceparentStartsANewRoot() throws Exception {
        // CONTROL: behaviour without an incoming parent is unchanged.
        run("tomcat", Map.of(), "/call|-|-");
        assertEquals(1, spans.size(), spans.toString());
        assertNewRoot(spans.get(0), TRACE_1, TRACE_2);
    }

    @Test void sequentialRequestsOnOnePooledThreadDoNotShareAParent() throws Exception {
        String log = run("tomcat", Map.of(), "/call|" + SAMPLED_1 + "|-", "/call|-|-", "/call|" + SAMPLED_2 + "|-",
            "/call|00-" + TRACE_1 + "-" + PARENT_1 + "-zz|-");
        assertEquals(1, threads(log).size(), "a single request thread serves every request: " + log);
        List<Map<String, Object>> exported = ordered();
        assertEquals(4, exported.size(), exported.toString());
        assertJoined(exported.get(0), TRACE_1, PARENT_1);
        assertNewRoot(exported.get(1), TRACE_1, TRACE_2);
        assertJoined(exported.get(2), TRACE_2, PARENT_2);
        // An invalid traceparent is ignored, as an absent one.
        assertNewRoot(exported.get(3), TRACE_1, TRACE_2, String.valueOf(exported.get(1).get("traceId")));
    }

    @Test void contextIsRestoredAfterAThrowingOrAsyncRequest() throws Exception {
        String log = run("tomcat", Map.of(), "/throw|" + SAMPLED_1 + "|-", "/call|-|-", "/async|" + SAMPLED_2 + "|-", "/call|-|-");
        assertTrue(log.contains("RESPONSE 0 STATUS=500"), log);
        assertTrue(log.contains("RESPONSE 2 STATUS=200"), log);
        assertEquals(1, threads(log).size(), log);
        List<Map<String, Object>> exported = ordered();
        assertEquals(4, exported.size(), exported.toString());
        assertJoined(exported.get(0), TRACE_1, PARENT_1);
        assertNewRoot(exported.get(1), TRACE_1, TRACE_2);
        assertJoined(exported.get(2), TRACE_2, PARENT_2);
        assertNewRoot(exported.get(3), TRACE_1, TRACE_2);
    }

    @Test void servletOverridingThePublicServiceMethodJoinsToo() throws Exception {
        // Jersey's ServletContainer and CXF override service(ServletRequest, ServletResponse) without calling HttpServlet's.
        String log = run("tomcat", Map.of(), "/own/call|" + SAMPLED_1 + "|-", "/own/call|-|-", "/call|" + SAMPLED_2 + "|-");
        assertEquals(1, threads(log).size(), log);
        List<Map<String, Object>> exported = ordered();
        assertEquals(3, exported.size(), exported.toString());
        assertJoined(exported.get(0), TRACE_1, PARENT_1);
        assertNewRoot(exported.get(1), TRACE_1, TRACE_2);
        assertJoined(exported.get(2), TRACE_2, PARENT_2);
    }

    @Test void applicationSpanInsideTheRequestTakesPrecedence() throws Exception {
        String log = run("tomcat", Map.of(), "/appspan|" + SAMPLED_1 + "|-");
        assertEquals(1, spans.size(), spans.toString());
        String line = log.lines().filter(l -> l.startsWith("APP_SPAN=")).findFirst().orElseThrow();
        assertJoined(spans.get(0), line.replaceAll(".*APP_TRACE=(\\w+).*", "$1"), line.replaceAll("APP_SPAN=(\\w+).*", "$1"));
    }

    @Test void sampledParentRatioIsInherited() throws Exception {
        run("tomcat", Map.of(), "/call|" + SAMPLED_1 + "|fi=r:0.5,vendor=value");
        assertEquals(1, spans.size(), spans.toString());
        assertJoined(spans.get(0), TRACE_1, PARENT_1);
        assertEquals("fi=r:0.5,vendor=value", spans.get(0).get("traceState"));
        assertEquals(0.5, attributes(spans.get(0)).get("fi.sampling.ratio"));
    }

    @Test void javaxServletJoinsAndRestoresAfterAThrow() throws Exception {
        String log = run("javax", Map.of(), "/throw|" + SAMPLED_1 + "|-", "/outside|-|-", "/call|" + SAMPLED_2 + "|-", "/call|" + UNSAMPLED_1 + "|-");
        assertTrue(log.contains("RESPONSE 0 THREW"), log);
        assertEquals(1, threads(log).size(), log);
        List<Map<String, Object>> exported = ordered();
        assertEquals(3, exported.size(), "the unsampled request exports nothing: " + exported);
        assertJoined(exported.get(0), TRACE_1, PARENT_1);
        assertNewRoot(exported.get(1), TRACE_1, TRACE_2);
        assertJoined(exported.get(2), TRACE_2, PARENT_2);
    }

    @Test void disablingTheServletIntegrationKeepsNewRoots() throws Exception {
        // CONTROL: FI_DISABLED_INSTRUMENTATIONS=servlet leaves incoming headers unread.
        run("tomcat", Map.of("FI_DISABLED_INSTRUMENTATIONS", "servlet"), "/call|" + SAMPLED_1 + "|-");
        assertEquals(1, spans.size(), spans.toString());
        assertNewRoot(spans.get(0), TRACE_1);
    }
}
