package ai.traceai.spring.autoconfigure;

import com.sun.net.httpserver.HttpServer;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

import java.io.File;
import java.net.InetAddress;
import java.net.InetSocketAddress;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * "Don't throw" really disables export. Each case runs the starter in a child JVM whose real environment carries FI_BASE_URL,
 * FI_API_KEY and FI_SECRET_KEY and sets traceai.fail-on-refused-transport=false. The context starts, no request reaches any
 * listener (the approved loopback target of the environment included), and exactly one warning names no value.
 */
class NoExportEnvironmentTest {
    static final String ENV_KEY = "environment-api-key-0001", ENV_SECRET = "environment-secret-key-0002", PROP_KEY = "property-api-key-0003", PROP_SECRET = "property-secret-key-0004";
    private final List<HttpServer> servers = new ArrayList<>();
    private final AtomicInteger approvedHits = new AtomicInteger(), refusedHits = new AtomicInteger();
    private HttpServer approved, refused;

    private HttpServer listener(String address, AtomicInteger hits) throws Exception {
        HttpServer server = HttpServer.create(new InetSocketAddress(InetAddress.getByName(address), 0), 0);
        server.createContext("/", exchange -> { try (exchange) { hits.incrementAndGet(); exchange.getRequestBody().readAllBytes(); exchange.sendResponseHeaders(200, -1); } });
        server.start(); servers.add(server);
        return server;
    }
    @BeforeEach void start() throws Exception { approved = listener("127.0.0.1", approvedHits); refused = listener("127.0.0.2", refusedHits); }
    @AfterEach void stop() { servers.forEach(s -> s.stop(0)); }
    private String approvedUrl() { return "http://127.0.0.1:" + approved.getAddress().getPort(); }
    private String refusedUrl() { return "http://127.0.0.2:" + refused.getAddress().getPort(); }

    private String run(Map<String, String> env, String... properties) throws Exception {
        String classpath = System.getProperty("surefire.test.class.path", System.getProperty("java.class.path"));
        List<String> command = new ArrayList<>(List.of(Path.of(System.getProperty("java.home"), "bin", "java").toString(), "-cp", classpath, NoExportChild.class.getName()));
        command.addAll(List.of(properties));
        command.addAll(List.of("traceai.project-name=p", "traceai.export-interval-ms=200"));
        Path output = Files.createTempFile(Path.of("target"), "no-export-", ".log");
        ProcessBuilder builder = new ProcessBuilder(command).redirectErrorStream(true).redirectOutput(output.toFile());
        builder.environment().keySet().removeIf(name -> name.startsWith("FI_") || name.startsWith("OTEL_"));
        builder.environment().putAll(env);
        Process process = builder.start();
        if (!process.waitFor(60, TimeUnit.SECONDS)) { process.destroyForcibly(); throw new AssertionError("child timed out: " + Files.readString(output)); }
        Thread.sleep(500);
        return Files.readString(output);
    }
    private static List<String> warnings(String log) { return log.lines().filter(line -> line.startsWith("WARN: ")).toList(); }
    private Map<String, String> env(String baseUrl) { return Map.of("FI_BASE_URL", baseUrl, "FI_API_KEY", ENV_KEY, "FI_SECRET_KEY", ENV_SECRET); }
    private static void valueFree(String log) {
        for (String value : List.of(ENV_KEY, ENV_SECRET, PROP_KEY, PROP_SECRET, "127.0.0.1", "127.0.0.2")) assertThat(warnings(log).toString()).doesNotContain(value);
    }

    @Test void aRefusedEnvironmentConfigurationWithFailureOffStartsAndSendsNothingAnywhere() throws Exception {
        String log = run(env(refusedUrl()), "traceai.fail-on-refused-transport=false");
        assertThat(log).as("the context must start: " + log).contains("STARTED=true");
        assertThat(refusedHits).as("requests to the refused target").hasValue(0);
        assertThat(approvedHits).as("requests to the approved target").hasValue(0);
        assertThat(warnings(log)).as("warnings: " + log).hasSize(1);
        valueFree(log);
    }

    @Test void aRefusedPropertyWithAnApprovedEnvironmentTargetAndFailureOffExportsNothingAnywhere() throws Exception {
        String log = run(env(approvedUrl()), "traceai.base-url=" + refusedUrl(), "traceai.api-key=" + PROP_KEY, "traceai.secret-key=" + PROP_SECRET, "traceai.fail-on-refused-transport=false");
        assertThat(log).as("the context must start: " + log).contains("STARTED=true");
        assertThat(refusedHits).as("requests to the refused property target").hasValue(0);
        assertThat(approvedHits).as("requests to the approved environment target after export was disabled").hasValue(0);
        assertThat(warnings(log)).as("warnings: " + log).hasSize(1);
        valueFree(log);
    }

    @Test void aRefusedConfigurationWithFailureOnPropagatesTheRefusal() throws Exception {
        String log = run(env(refusedUrl()), "traceai.fail-on-refused-transport=true");
        assertThat(log).contains("STARTED=false").contains("FAILURE=").contains("FI_ALLOW_INSECURE_HTTP");
        assertThat(refusedHits).hasValue(0);
        assertThat(approvedHits).hasValue(0);
        valueFree(log.lines().filter(line -> !line.startsWith("FAILURE=")).reduce("", (a, b) -> a + "\n" + b));
    }

    @Test void controlAnApprovedEnvironmentTargetExportsWhenNothingIsRefused() throws Exception {
        String log = run(env(approvedUrl()), "traceai.fail-on-refused-transport=false");
        assertThat(log).contains("STARTED=true");
        assertThat(approvedHits.get()).as("the control must export, so the zero counts above mean something: " + log).isGreaterThan(0);
        assertThat(warnings(log)).isEmpty();
    }
}
