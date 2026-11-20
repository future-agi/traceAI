package ai.traceai.auto;

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
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import static org.junit.jupiter.api.Assertions.*;

/**
 * The agent keeps its own SDK private. The child JVM runs on the module's classes, not the shaded agent jar, so the
 * OpenTelemetry API the agent uses is the application's: whatever the agent registers globally, the application sees.
 */
class GlobalRegistrationTest {
    static final String CHAT = "{\"id\":\"c\",\"object\":\"chat.completion\",\"model\":\"test-model\",\"choices\":[{\"index\":0,\"message\":{\"role\":\"assistant\",\"content\":\"Hello\"},\"finish_reason\":\"stop\"}],\"usage\":{\"prompt_tokens\":1,\"completion_tokens\":1,\"total_tokens\":2}}";
    HttpServer server;
    final List<String> collector = new CopyOnWriteArrayList<>();

    @BeforeEach void start() throws Exception {
        server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        server.createContext("/", exchange -> {
            byte[] body = exchange.getRequestBody().readAllBytes();
            if (exchange.getRequestURI().getPath().startsWith("/tracer/v1/traces")) { collector.add(new String(body, StandardCharsets.ISO_8859_1)); exchange.sendResponseHeaders(200, -1); }
            else {
                byte[] reply = CHAT.getBytes(StandardCharsets.UTF_8);
                exchange.getResponseHeaders().set("Content-Type", "application/json");
                exchange.sendResponseHeaders(200, reply.length); exchange.getResponseBody().write(reply);
            }
            exchange.close();
        });
        server.start();
    }
    @AfterEach void stop() { server.stop(0); }

    String run(String scenario) throws Exception {
        Path out = Files.createTempFile("global-app-", ".log");
        List<String> command = new ArrayList<>(List.of(Path.of(System.getProperty("java.home"), "bin", "java").toString(),
            "-cp", System.getProperty("java.class.path"), PlainApp.class.getName(), "jdk", "http://127.0.0.1:" + server.getAddress().getPort() + "/v1/chat/completions", scenario));
        ProcessBuilder pb = new ProcessBuilder(command).redirectErrorStream(true).redirectOutput(out.toFile());
        pb.environment().keySet().removeIf(k -> k.startsWith("FI_") || k.startsWith("OTEL_"));
        pb.environment().put("FI_CONFIG_FILE", "target/no-global-config.toml"); pb.environment().put("FI_BASE_URL", "http://127.0.0.1:" + server.getAddress().getPort());
        pb.environment().put("FI_API_KEY", "test-api-key"); pb.environment().put("FI_SECRET_KEY", "test-secret-key");
        Process process = pb.start();
        if (!process.waitFor(40, TimeUnit.SECONDS)) { process.destroyForcibly(); fail("Child timed out: " + Files.readString(out)); }
        String log = Files.readString(out); assertEquals(0, process.exitValue(), log);
        return log;
    }

    @Test void anApplicationWithoutItsOwnGlobalKeepsTheNoOpDefaultAndItsSpansStayLocal() throws Exception {
        String log = run("global_none");
        assertTrue(log.contains("GLOBAL_SPAN_RECORDING=false"), log);
        String exported = String.join("\n", collector);
        assertTrue(exported.contains("http.response.status_code") || exported.contains("test-model"), "the agent's own AI span did not arrive: " + exported);
        for (String text : List.of("global-app-span", "app-library-global")) assertFalse(exported.contains(text), text);
    }

    @Test void anApplicationThatInstallsItsOwnGlobalKeepsItAndItsSpansStayLocal() throws Exception {
        String log = run("global_app");
        assertTrue(log.contains("GLOBAL_SPAN_RECORDING=true"), log); assertTrue(log.contains("APP_EXPORTER_SPANS=1"), log);
        String exported = String.join("\n", collector);
        assertTrue(exported.contains("http.response.status_code") || exported.contains("test-model"), "the agent's own AI span did not arrive: " + exported);
        for (String text : List.of("global-app-span", "app-library-global")) assertFalse(exported.contains(text), text);
    }
}
