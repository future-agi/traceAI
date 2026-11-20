package ai.traceai;

import com.sun.net.httpserver.HttpServer;
import io.opentelemetry.api.GlobalOpenTelemetry;
import io.opentelemetry.api.trace.Span;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.PrintStream;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.util.List;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.logging.Handler;
import java.util.logging.LogRecord;
import java.util.logging.Logger;

import static org.assertj.core.api.Assertions.assertThat;

/** The Future AGI export (OTLP) and the console exporter TraceAI creates replace known secrets. */
class TraceAIKnownSecretsTest {
    private HttpServer server;
    private final List<byte[]> bodies = new CopyOnWriteArrayList<>();

    @BeforeEach
    void start() throws IOException {
        TraceAI.shutdown();
        GlobalOpenTelemetry.resetForTest();
        server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        server.createContext("/", exchange -> {
            try (exchange) {
                bodies.add(exchange.getRequestBody().readAllBytes());
                exchange.getResponseHeaders().set("Content-Type", "application/x-protobuf");
                exchange.sendResponseHeaders(200, -1);
            }
        });
        server.start();
    }

    @AfterEach
    void stop() {
        TraceAI.shutdown();
        server.stop(0);
        GlobalOpenTelemetry.resetForTest();
    }

    @Test
    void configuredKeysNeverReachTheOtlpOrConsoleExportRaw() {
        String apiKey = "PFX-api-key-value-0001";
        String secretKey = "PFX-secret-key-value-0002";
        String later = "PFX-api-key-value-0001-and-more-learned-later";
        Logger logger = Logger.getLogger("io.opentelemetry.exporter.logging.LoggingSpanExporter");
        StringBuilder console = new StringBuilder();
        Handler handler = new Handler() {
            @Override public void publish(LogRecord record) { console.append(record.getMessage()); }
            @Override public void flush() { }
            @Override public void close() { }
        };
        logger.addHandler(handler);
        try {
            TraceAI.init(TraceConfig.builder().baseUrl("http://127.0.0.1:" + server.getAddress().getPort())
                .apiKey(apiKey).secretKey(secretKey).projectName("p").exportIntervalMs(60000)
                .enableConsoleExporter(true).build());
            FITracer tracer = TraceAI.getTracer();
            Span span = tracer.startSpan("span " + apiKey, FISpanKind.LLM);
            tracer.setAttribute(span, "custom.attribute", "a " + apiKey + " b " + secretKey + " c " + later);
            tracer.setError(span, new IllegalStateException("failed with " + secretKey));
            // A longer secret that contains a registered one, learned after the span was captured.
            ai.traceai.privacy.KnownSecrets.process().register(later);
            span.end();
            TraceAI.shutdown();
        } finally {
            logger.removeHandler(handler);
        }
        assertThat(bodies).isNotEmpty();
        String wire = new String(bodies.get(0), StandardCharsets.ISO_8859_1);
        assertThat(wire).contains("custom.attribute").contains("<redacted>");
        for (String secret : List.of("api-key-value", "secret-key-value", "more-learned-later", "PFX-")) {
            assertThat(wire).doesNotContain(secret);
        }
        assertThat(console.toString()).contains("custom.attribute");
        for (String secret : List.of("api-key-value", "secret-key-value", "more-learned-later", "PFX-")) {
            assertThat(console.toString()).doesNotContain(secret);
        }
    }
}
