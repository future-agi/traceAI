package ai.traceai;

import com.sun.net.httpserver.HttpServer;
import org.junit.jupiter.api.Test;

import java.io.File;
import java.net.InetSocketAddress;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.TimeUnit;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * OTLP header environment variables set for another backend must not change what the
 * Future AGI exporter sends. The environment is set on a child JVM only, and the receiver
 * binds 127.0.0.1 with synthetic values.
 */
class AmbientHeadersTest {
    private static final String OTHER_BACKEND = "Bearer SYNTHETIC-OTHER-BACKEND";

    private record Seen(String apiKey, String secretKey, String project, String authorization, String marker) {}

    private Seen exportWith(Map<String, String> env) throws Exception {
        List<Seen> seen = new CopyOnWriteArrayList<>();
        HttpServer server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        server.createContext("/", exchange -> {
            try (exchange) {
                exchange.getRequestBody().readAllBytes();
                var h = exchange.getRequestHeaders();
                seen.add(new Seen(h.getFirst("X-Api-Key"), h.getFirst("X-Secret-Key"),
                    h.getFirst("fi-project-name"), h.getFirst("Authorization"), h.getFirst("X-Ambient-Marker")));
                exchange.sendResponseHeaders(200, -1);
            }
        });
        server.start();
        try {
            ProcessBuilder pb = new ProcessBuilder(
                new File(System.getProperty("java.home"), "bin/java").getPath(),
                "-cp", System.getProperty("java.class.path"),
                AmbientHeadersChild.class.getName(), "http://127.0.0.1:" + server.getAddress().getPort());
            pb.environment().keySet().removeIf(k -> k.startsWith("OTEL_"));
            pb.environment().putAll(env);
            pb.redirectErrorStream(true);
            pb.redirectOutput(ProcessBuilder.Redirect.DISCARD);
            Process p = pb.start();
            assertThat(p.waitFor(60, TimeUnit.SECONDS)).isTrue();
            assertThat(seen).hasSize(1);
            return seen.get(0);
        } finally {
            server.stop(0);
        }
    }

    private static void assertOwnHeadersOnly(Seen s) {
        assertThat(s.apiKey()).isEqualTo("synthetic-api-key");
        assertThat(s.secretKey()).isEqualTo("synthetic-secret-key");
        assertThat(s.project()).isEqualTo("ambient-headers-project");
        assertThat(s.authorization()).isNull();
        assertThat(s.marker()).isNull();
    }

    @Test
    void sharedOtlpHeadersVariableDoesNotReachTheExporter() throws Exception {
        assertOwnHeadersOnly(exportWith(Map.of("OTEL_EXPORTER_OTLP_HEADERS",
            "Authorization=" + OTHER_BACKEND.replace(" ", "%20") + ",X-Ambient-Marker=shared-marker")));
    }

    @Test
    void tracesOtlpHeadersVariableDoesNotReachTheExporter() throws Exception {
        assertOwnHeadersOnly(exportWith(Map.of("OTEL_EXPORTER_OTLP_TRACES_HEADERS",
            "Authorization=" + OTHER_BACKEND.replace(" ", "%20") + ",X-Ambient-Marker=traces-marker")));
    }
}
