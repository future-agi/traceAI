package ai.traceai.auto;

import io.opentelemetry.sdk.testing.exporter.InMemorySpanExporter;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.export.SimpleSpanProcessor;
import org.junit.jupiter.api.Test;
import java.util.*;
import java.util.concurrent.atomic.AtomicReference;
import static org.junit.jupiter.api.Assertions.*;

/**
 * OTLP header environment variables set for another backend must not change what the agent's
 * exporter sends to Future AGI. The environment is passed as a map (never process-global) and
 * the receiver binds 127.0.0.1 with synthetic values.
 */
class AmbientHeadersTest {
    private static Map<String, String> exportWith(Map<String, String> environment) throws Exception {
        AtomicReference<Map<String, String>> seen = new AtomicReference<>();
        var server = com.sun.net.httpserver.HttpServer.create(new java.net.InetSocketAddress("127.0.0.1", 0), 0);
        server.createContext("/tracer/v1/traces", exchange -> {
            Map<String, String> headers = new HashMap<>();
            exchange.getRequestHeaders().forEach((k, v) -> headers.put(k.toLowerCase(Locale.ROOT), v.get(0)));
            seen.set(headers);
            exchange.getRequestBody().readAllBytes(); exchange.sendResponseHeaders(200, -1); exchange.close();
        });
        server.start();
        try {
            AutoConfig config = AutoConfig.resolve(Map.of("api_key", "synthetic-api-key", "secret_key", "synthetic-secret-key",
                "project_name", "ambient-headers-project", "base_url", "http://127.0.0.1:" + server.getAddress().getPort()), environment);
            var memory = InMemorySpanExporter.create();
            var provider = SdkTracerProvider.builder().addSpanProcessor(SimpleSpanProcessor.create(memory)).build();
            try {
                provider.get("test").spanBuilder("ambient-headers-span").startSpan().end();
                assertTrue(new OtlpJsonExporter(config).export(memory.getFinishedSpanItems()).isSuccess());
            } finally { provider.shutdown(); }
            return seen.get();
        } finally { server.stop(0); }
    }
    private static void assertOwnHeadersOnly(Map<String, String> h) {
        assertNotNull(h);
        assertEquals("synthetic-api-key", h.get("x-api-key"));
        assertEquals("synthetic-secret-key", h.get("x-secret-key"));
        assertEquals("ambient-headers-project", h.get("fi-project-name"));
        assertNull(h.get("authorization"));
        assertNull(h.get("x-ambient-marker"));
    }
    @Test void sharedOtlpHeadersVariableDoesNotReachTheExporter() throws Exception {
        assertOwnHeadersOnly(exportWith(Map.of("OTEL_EXPORTER_OTLP_HEADERS", "Authorization=Bearer%20SYNTHETIC-OTHER-BACKEND,X-Ambient-Marker=shared-marker")));
    }
    @Test void tracesOtlpHeadersVariableDoesNotReachTheExporter() throws Exception {
        assertOwnHeadersOnly(exportWith(Map.of("OTEL_EXPORTER_OTLP_TRACES_HEADERS", "Authorization=Bearer%20SYNTHETIC-OTHER-BACKEND,X-Ambient-Marker=traces-marker")));
    }
}
