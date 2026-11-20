package ai.traceai;

import com.sun.net.httpserver.HttpServer;
import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.sdk.common.CompletableResultCode;
import io.opentelemetry.sdk.testing.exporter.InMemorySpanExporter;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.data.SpanData;
import io.opentelemetry.sdk.trace.export.SimpleSpanProcessor;
import io.opentelemetry.sdk.trace.export.SpanExporter;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.Test;

import java.io.ByteArrayOutputStream;
import java.net.InetAddress;
import java.net.InetSocketAddress;
import java.net.ProxySelector;
import java.net.SocketAddress;
import java.net.URI;
import java.time.Duration;
import java.util.Collection;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;

import static org.assertj.core.api.Assertions.assertThat;

/** The keyed exporter reports honestly: flush and shutdown wait for a send in flight, and nothing is sent after shutdown. */
class KeyedExporterLifecycleTest {
    private final List<HttpServer> servers = new CopyOnWriteArrayList<>();
    private final CountDownLatch release = new CountDownLatch(1);
    private final List<byte[]> bodies = new CopyOnWriteArrayList<>();

    @AfterEach void stop() { release.countDown(); servers.forEach(s -> s.stop(0)); }

    private HttpServer collector(boolean hold, AtomicInteger hits) throws Exception {
        HttpServer server = HttpServer.create(new InetSocketAddress(InetAddress.getByName("127.0.0.1"), 0), 0);
        server.createContext("/", exchange -> {
            try (exchange) {
                hits.incrementAndGet();
                bodies.add(exchange.getRequestBody().readAllBytes());
                if (hold) release.await(30, TimeUnit.SECONDS);
                exchange.sendResponseHeaders(200, -1);
            } catch (InterruptedException ignored) { }
        });
        server.start(); servers.add(server);
        return server;
    }
    private static String url(HttpServer server) { return "http://127.0.0.1:" + server.getAddress().getPort(); }
    private static KeyedOtlpSpanExporter exporter(HttpServer server, Duration wait) {
        return new KeyedOtlpSpanExporter(KeyTransport.check(url(server), true, Map.of(), false), "synthetic-api-key", "synthetic-secret-key", "p", wait);
    }
    private static List<SpanData> spans(String name) {
        InMemorySpanExporter memory = InMemorySpanExporter.create();
        try (SdkTracerProvider provider = SdkTracerProvider.builder().addSpanProcessor(SimpleSpanProcessor.create(memory)).build()) {
            provider.get("test").spanBuilder(name).startSpan().end();
            return memory.getFinishedSpanItems();
        }
    }

    @Test void flushAndShutdownWaitForASendInFlightThenReportFailure() throws Exception {
        AtomicInteger hits = new AtomicInteger();
        HttpServer held = collector(true, hits);
        KeyedOtlpSpanExporter exporter = exporter(held, Duration.ofMillis(500));
        exporter.export(spans("probe"));
        long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(10);
        while (hits.get() == 0 && System.nanoTime() < deadline) Thread.sleep(20);
        assertThat(hits).hasValue(1);
        long started = System.nanoTime();
        CompletableResultCode flush = exporter.flush().join(5, TimeUnit.SECONDS);
        long waited = TimeUnit.NANOSECONDS.toMillis(System.nanoTime() - started);
        assertThat(flush.isSuccess()).as("flush with a send in flight").isFalse();
        assertThat(waited).as("flush waited").isGreaterThanOrEqualTo(400);
        assertThat(exporter.shutdown().join(5, TimeUnit.SECONDS).isSuccess()).as("shutdown with a send in flight").isFalse();
    }

    @Test void flushSucceedsOnceTheSendHasCompleted() throws Exception {
        AtomicInteger hits = new AtomicInteger();
        KeyedOtlpSpanExporter exporter = exporter(collector(false, hits), Duration.ofSeconds(5));
        assertThat(exporter.export(spans("probe")).join(10, TimeUnit.SECONDS).isSuccess()).isTrue();
        assertThat(exporter.flush().join(5, TimeUnit.SECONDS).isSuccess()).isTrue();
        assertThat(exporter.shutdown().join(5, TimeUnit.SECONDS).isSuccess()).isTrue();
    }

    @Test void anExportAfterShutdownSendsNothingAndFails() throws Exception {
        AtomicInteger hits = new AtomicInteger();
        KeyedOtlpSpanExporter exporter = exporter(collector(false, hits), Duration.ofSeconds(5));
        assertThat(exporter.shutdown().join(5, TimeUnit.SECONDS).isSuccess()).isTrue();
        assertThat(exporter.export(spans("late")).join(5, TimeUnit.SECONDS).isSuccess()).isFalse();
        assertThat(exporter.flush().join(5, TimeUnit.SECONDS).isSuccess()).isFalse();
        Thread.sleep(200);
        assertThat(hits).hasValue(0);
    }

    @Test void aRedirectFailsTheBatchAndNothingIsSentElsewhere() throws Exception {
        AtomicInteger first = new AtomicInteger(), second = new AtomicInteger();
        HttpServer elsewhere = collector(false, second);
        HttpServer origin = HttpServer.create(new InetSocketAddress(InetAddress.getByName("127.0.0.1"), 0), 0);
        origin.createContext("/", exchange -> { try (exchange) { first.incrementAndGet(); exchange.getRequestBody().readAllBytes();
            exchange.getResponseHeaders().set("Location", url(elsewhere) + "/tracer/v1/traces"); exchange.sendResponseHeaders(307, -1); } });
        origin.start(); servers.add(origin);
        assertThat(exporter(origin, Duration.ofSeconds(5)).export(spans("probe")).join(10, TimeUnit.SECONDS).isSuccess()).isFalse();
        assertThat(first).hasValue(1);
        assertThat(second).hasValue(0);
    }

    /** What a wrapper installed in front of the keyed exporter (for example a secret-replacing one) produces is what goes on the wire. */
    @Test void aWrapperInFrontOfTheKeyedExporterDecidesWhatIsSent() throws Exception {
        AtomicInteger hits = new AtomicInteger();
        KeyedOtlpSpanExporter keyed = exporter(collector(false, hits), Duration.ofSeconds(5));
        SpanExporter wrapper = new SpanExporter() {
            @Override public CompletableResultCode export(Collection<SpanData> data) { return keyed.export(spans("replaced-name")); }
            @Override public CompletableResultCode flush() { return keyed.flush(); }
            @Override public CompletableResultCode shutdown() { return keyed.shutdown(); }
        };
        assertThat(wrapper.export(spans("original-secret-name")).join(10, TimeUnit.SECONDS).isSuccess()).isTrue();
        String wire = new String(bodies.get(0), java.nio.charset.StandardCharsets.ISO_8859_1);
        assertThat(wire).contains("replaced-name").doesNotContain("original-secret-name");
    }

    /** Literal loopback is plaintext without the opt-in, and a configured proxy sees no connection. */
    @Test void aLiteralLoopbackPlaintextSendIgnoresAConfiguredProxy() throws Exception {
        AtomicInteger hits = new AtomicInteger(), proxied = new AtomicInteger();
        HttpServer target = collector(false, hits);
        AtomicInteger connections = new AtomicInteger();
        try (java.net.ServerSocket proxy = new java.net.ServerSocket(0, 5, InetAddress.getByName("127.0.0.1"))) {
            Thread acceptor = new Thread(() -> { try { while (true) { proxy.accept().close(); connections.incrementAndGet(); } } catch (Exception ignored) { } });
            acceptor.setDaemon(true); acceptor.start();
            ProxySelector previous = ProxySelector.getDefault();
            String oldHost = System.getProperty("http.proxyHost"), oldPort = System.getProperty("http.proxyPort"), oldNon = System.getProperty("http.nonProxyHosts");
            System.setProperty("http.proxyHost", "127.0.0.1"); System.setProperty("http.proxyPort", String.valueOf(proxy.getLocalPort())); System.setProperty("http.nonProxyHosts", "");
            ProxySelector.setDefault(new ProxySelector() {
                @Override public List<java.net.Proxy> select(URI uri) { return List.of(new java.net.Proxy(java.net.Proxy.Type.HTTP, new InetSocketAddress("127.0.0.1", proxy.getLocalPort()))); }
                @Override public void connectFailed(URI uri, SocketAddress address, java.io.IOException failure) { }
            });
            try {
                assertThat(exporter(target, Duration.ofSeconds(5)).export(spans("probe")).join(10, TimeUnit.SECONDS).isSuccess()).isTrue();
            } finally {
                ProxySelector.setDefault(previous);
                restore("http.proxyHost", oldHost); restore("http.proxyPort", oldPort); restore("http.nonProxyHosts", oldNon);
            }
        }
        assertThat(hits).hasValue(1);
        assertThat(connections).hasValue(0);
    }
    private static void restore(String key, String value) { if (value == null) System.clearProperty(key); else System.setProperty(key, value); }

    @Test void shutdownCalledFromTheCompletionCallbackReturnsPromptly() throws Exception {
        AtomicInteger hits = new AtomicInteger();
        HttpServer server = collector(false, hits);
        KeyedOtlpSpanExporter exporter = exporter(server, Duration.ofSeconds(3));
        long[] millis = {-1}; boolean[] ok = {false};
        CountDownLatch called = new CountDownLatch(1);
        CompletableResultCode result = exporter.export(spans("probe"));
        result.whenComplete(() -> {
            long start = System.nanoTime();
            ok[0] = exporter.shutdown().join(10, TimeUnit.SECONDS).isSuccess();
            millis[0] = (System.nanoTime() - start) / 1_000_000;
            called.countDown();
        });
        assertThat(called.await(15, TimeUnit.SECONDS)).isTrue();
        assertThat(millis[0]).as("shutdown from the completion callback waited out its own send (ms)").isLessThan(1000);
        assertThat(ok[0]).as("shutdown from the callback reports success: nothing else was in flight").isTrue();
    }
}
