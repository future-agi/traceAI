package ai.traceai.spring.compatibility;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.sun.net.httpserver.HttpServer;
import java.io.File;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.TimeUnit;
import org.junit.jupiter.api.Timeout;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;
import static org.junit.jupiter.api.Assertions.*;

/**
 * The packaged zero-code agent against a REAL Spring AI native tool call: DefaultToolCallingManager runs the tool inside Spring AI's own
 * tool_call observation, traced by a real Micrometer tracing handler. While the tool runs, that handler's span has no name and no tags yet
 * (they are applied when the observation stops), so only the open observation says the call is already traced: the agent must add no
 * second execute_tool span, and with no observation at all it must add exactly one.
 */
@Timeout(120)
class NativeToolObservationAgentTest {
    static final ObjectMapper JSON = new ObjectMapper();

    record Run(String log, List<JsonNode> agentSpans) {
        List<JsonNode> toolSpans() { return agentSpans.stream().filter(s -> s.path("name").asText().startsWith("execute_tool")).toList(); }
        List<String> appSpans() { return log.lines().filter(l -> l.startsWith("APP_SPAN=")).toList(); }
    }

    static Run run(String handler) throws Exception {
        String agent = System.getProperty("traceai.agent.jar");
        assertNotNull(agent, "traceai.agent.jar is set by the surefire configuration");
        assertTrue(Files.isRegularFile(Path.of(agent)), agent);
        List<JsonNode> spans = new CopyOnWriteArrayList<>();
        HttpServer collector = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        collector.createContext("/tracer/v1/traces", exchange -> {
            JsonNode root = JSON.readTree(exchange.getRequestBody().readAllBytes());
            for (JsonNode resource : root.path("resourceSpans")) for (JsonNode scope : resource.path("scopeSpans")) scope.path("spans").forEach(spans::add);
            exchange.getResponseHeaders().add("Content-Type", "application/json");
            exchange.sendResponseHeaders(200, 2);
            exchange.getResponseBody().write("{}".getBytes(StandardCharsets.UTF_8));
            exchange.close();
        });
        collector.start();
        try {
            Path output = Files.createTempFile(Path.of("target"), "native-tool-", ".log");
            List<String> command = new ArrayList<>(List.of(Path.of(System.getProperty("java.home"), "bin", "java").toString(),
                "-javaagent:" + agent, "-Djava.net.preferIPv4Stack=true", "-cp", System.getProperty("java.class.path"),
                NativeToolObservationApp.class.getName(), handler));
            ProcessBuilder pb = new ProcessBuilder(command).redirectErrorStream(true).redirectOutput(output.toFile());
            var env = pb.environment();
            env.keySet().removeIf(k -> k.startsWith("FI_") || k.startsWith("OTEL_"));
            env.put("FI_CONFIG_FILE", "target/no-native-tool-config.toml");
            env.put("FI_BASE_URL", "http://127.0.0.1:" + collector.getAddress().getPort());
            env.put("FI_API_KEY", "test-api-key"); env.put("FI_SECRET_KEY", "test-secret-key");
            Process process = pb.start();
            if (!process.waitFor(90, TimeUnit.SECONDS)) { process.destroyForcibly(); fail("child timed out: " + Files.readString(output)); }
            String log = Files.readString(output);
            assertEquals(0, process.exitValue(), log);
            assertTrue(log.contains("DONE"), log);
            assertTrue(log.contains("TOOL_CALLS=1 MODEL_REQUESTS=2"), "the tool ran once through the real tool-calling manager: " + log);
            Files.deleteIfExists(output);
            return new Run(log, spans);
        } finally { collector.stop(0); }
    }

    @ParameterizedTest @ValueSource(strings = {"spring", "default"})
    void aRealNativeToolObservationGetsNoSecondToolSpanFromTheAgent(String handler) throws Exception {
        Run run = run(handler);
        assertTrue(run.toolSpans().isEmpty(), "the agent added an execute_tool span beside Spring AI's own: " + run.toolSpans());
        // Spring AI's tool observation produced (named tool_call, framework or execute_tool by the Spring AI version) the application's one tool span, and the agent was active (it exported its other spans).
        assertEquals(1, run.appSpans().stream().filter(s -> s.matches("APP_SPAN=(tool_call|execute_tool|framework) weather\\|.*")).count(), run.log());
    }

    @ParameterizedTest @ValueSource(strings = {"none"})
    void withoutAnObservationTheAgentTracesTheToolOnce(String handler) throws Exception {
        Run run = run(handler);
        assertEquals(1, run.toolSpans().size(), run.agentSpans().toString());
        assertEquals("execute_tool weather", run.toolSpans().get(0).path("name").asText());
    }
}
