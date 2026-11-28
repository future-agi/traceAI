package ai.traceai.auto;

import com.sun.net.httpserver.HttpServer;
import io.opentelemetry.sdk.common.CompletableResultCode;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.Test;

import java.net.InetAddress;
import java.net.InetSocketAddress;
import java.time.Duration;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;

import static org.junit.jupiter.api.Assertions.*;

/** The agent's exporter reports honestly: flush and shutdown wait for a send in flight, and nothing is sent after shutdown. */
class ExporterLifecycleTest {
    final List<HttpServer> servers = new CopyOnWriteArrayList<>();
    final CountDownLatch release = new CountDownLatch(1);

    @AfterEach void stop() { release.countDown(); servers.forEach(s -> s.stop(0)); }

    HttpServer collector(boolean hold, AtomicInteger hits) throws Exception {
        HttpServer server = HttpServer.create(new InetSocketAddress(InetAddress.getByName("127.0.0.1"), 0), 0);
        server.createContext("/", exchange -> {
            try (exchange) {
                hits.incrementAndGet(); exchange.getRequestBody().readAllBytes();
                if (hold) release.await(30, TimeUnit.SECONDS);
                exchange.sendResponseHeaders(200, -1);
            } catch (InterruptedException ignored) { }
        });
        server.start(); servers.add(server);
        return server;
    }
    static OtlpJsonExporter exporter(HttpServer server, Duration wait) {
        return new OtlpJsonExporter(AutoConfig.resolve(Map.of("api_key", "agent-api-key", "secret_key", "agent-secret-key",
            "base_url", "http://127.0.0.1:" + server.getAddress().getPort()), Map.of()), wait);
    }

    @Test void flushAndShutdownWaitForASendInFlightThenReportFailure() throws Exception {
        AtomicInteger hits = new AtomicInteger();
        OtlpJsonExporter exporter = exporter(collector(true, hits), Duration.ofMillis(500));
        Thread sender = new Thread(() -> exporter.export(KeyTransportAgentTest.spans()));
        sender.setDaemon(true); sender.start();
        long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(10);
        while (hits.get() == 0 && System.nanoTime() < deadline) Thread.sleep(20);
        assertEquals(1, hits.get());
        long started = System.nanoTime();
        CompletableResultCode flush = exporter.flush().join(5, TimeUnit.SECONDS);
        assertFalse(flush.isSuccess(), "flush with a send in flight");
        assertTrue(TimeUnit.NANOSECONDS.toMillis(System.nanoTime() - started) >= 400, "flush waited");
        assertFalse(exporter.shutdown().join(5, TimeUnit.SECONDS).isSuccess(), "shutdown with a send in flight");
    }

    @Test void anExportAfterShutdownSendsNothingAndFails() throws Exception {
        AtomicInteger hits = new AtomicInteger();
        OtlpJsonExporter exporter = exporter(collector(false, hits), Duration.ofSeconds(5));
        assertTrue(exporter.export(KeyTransportAgentTest.spans()).isSuccess());
        assertTrue(exporter.flush().join(5, TimeUnit.SECONDS).isSuccess());
        assertTrue(exporter.shutdown().join(5, TimeUnit.SECONDS).isSuccess());
        assertFalse(exporter.export(KeyTransportAgentTest.spans()).isSuccess());
        assertFalse(exporter.flush().join(5, TimeUnit.SECONDS).isSuccess());
        assertEquals(1, hits.get());
    }
}
