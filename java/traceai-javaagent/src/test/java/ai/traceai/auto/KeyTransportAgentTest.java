package ai.traceai.auto;

import com.sun.net.httpserver.HttpServer;
import io.opentelemetry.sdk.testing.exporter.InMemorySpanExporter;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.data.SpanData;
import io.opentelemetry.sdk.trace.export.SimpleSpanProcessor;
import org.junit.jupiter.api.*;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.PrintStream;
import java.net.InetAddress;
import java.net.InetSocketAddress;
import java.util.*;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.atomic.AtomicInteger;

import static org.junit.jupiter.api.Assertions.*;

/** What the agent's exporter sends, and what it never sends, for each key-transport rule. */
class KeyTransportAgentTest {
    final List<HttpServer> servers = new CopyOnWriteArrayList<>();
    final List<String> keys = new CopyOnWriteArrayList<>();
    String oldHost, oldPort, oldNonProxy;
    PrintStream originalErr; ByteArrayOutputStream err;

    @BeforeEach void setUp() {
        ai.traceai.KeyTransport.resetWarningForTest();
        oldHost = System.getProperty("http.proxyHost"); oldPort = System.getProperty("http.proxyPort"); oldNonProxy = System.getProperty("http.nonProxyHosts");
        originalErr = System.err; err = new ByteArrayOutputStream(); System.setErr(new PrintStream(err, true));
    }
    @AfterEach void tearDown() {
        System.setErr(originalErr); ai.traceai.KeyTransport.resetWarningForTest();
        servers.forEach(s -> s.stop(0));
        restore("http.proxyHost", oldHost); restore("http.proxyPort", oldPort); restore("http.nonProxyHosts", oldNonProxy);
    }
    static void restore(String key, String value) { if (value == null) System.clearProperty(key); else System.setProperty(key, value); }

    HttpServer server(String address, int status, String location, AtomicInteger hits) throws IOException {
        HttpServer server = HttpServer.create(new InetSocketAddress(InetAddress.getByName(address), 0), 0);
        server.createContext("/", exchange -> {
            try (exchange) {
                hits.incrementAndGet();
                String key = exchange.getRequestHeaders().getFirst("X-Api-Key");
                if (key != null) keys.add(key);
                exchange.getRequestBody().readAllBytes();
                if (location != null) exchange.getResponseHeaders().set("Location", location);
                exchange.sendResponseHeaders(status, -1);
            }
        });
        server.start(); servers.add(server);
        return server;
    }
    static String url(HttpServer server) { return "http://" + server.getAddress().getAddress().getHostAddress() + ":" + server.getAddress().getPort(); }
    static List<SpanData> spans() {
        InMemorySpanExporter memory = InMemorySpanExporter.create();
        try (SdkTracerProvider provider = SdkTracerProvider.builder().addSpanProcessor(SimpleSpanProcessor.create(memory)).build()) {
            provider.get("test").spanBuilder("probe").startSpan().end();
            return memory.getFinishedSpanItems();
        }
    }
    static OtlpJsonExporter exporter(String base, Map<String, Object> extra, Map<String, String> env) {
        Map<String, Object> options = new LinkedHashMap<>(Map.of("api_key", "agent-api-key", "secret_key", "agent-secret-key", "base_url", base));
        options.putAll(extra);
        return new OtlpJsonExporter(AutoConfig.resolve(options, env));
    }

    @Test void futureAgiOverPlaintextIsRefusedWithAndWithoutOptIn() {
        for (String base : List.of("http://api.futureagi.com", "http://api.futureagi.com.", "http://eu.api.futureagi.com:80")) {
            OtlpJsonExporter plain = exporter(base, Map.of(), Map.of());
            assertFalse(plain.export(spans()).isSuccess());
            assertTrue(plain.lastExport.startsWith("refused"), plain.lastExport);
            OtlpJsonExporter opted = exporter(base, Map.of("allow_insecure_http", "1"), Map.of("FI_ALLOW_INSECURE_HTTP", "1"));
            assertFalse(opted.export(spans()).isSuccess());
            assertTrue(opted.lastExport.startsWith("refused"), opted.lastExport);
        }
    }

    @Test void disagreeingTargetsAreRefused() {
        for (String base : List.of("http://127.0.0.1\\@evil.example", "http://localhost%5C@evil.example", "https://api.futureagi.com@evil.example", "http:///x", "http://localhost:80@evil.example")) {
            OtlpJsonExporter exporter = exporter(base, Map.of(), Map.of("FI_ALLOW_INSECURE_HTTP", "1"));
            assertFalse(exporter.export(spans()).isSuccess(), base);
            assertTrue(exporter.lastExport.startsWith("refused"), exporter.lastExport);
        }
    }

    @Test void selfHostedPlaintextIsRefusedByDefaultAndSendsNothing() throws Exception {
        AtomicInteger hits = new AtomicInteger();
        HttpServer target = server("127.0.0.2", 200, null, hits);
        for (Map<String, String> env : List.of(Map.<String, String>of(), Map.of("FI_ALLOW_INSECURE_HTTP", "true"), Map.of("FI_ALLOW_INSECURE_HTTP", "0"))) {
            OtlpJsonExporter exporter = exporter(url(target), Map.of(), env);
            assertFalse(exporter.export(spans()).isSuccess());
            assertTrue(exporter.lastExport.startsWith("refused"), exporter.lastExport);
        }
        assertEquals(0, hits.get());
    }

    @Test void selfHostedPlaintextWithTheOptInGoesDirectAndWarnsOnce() throws Exception {
        AtomicInteger hits = new AtomicInteger(), proxied = new AtomicInteger();
        HttpServer target = server("127.0.0.2", 200, null, hits);
        HttpServer proxy = server("127.0.0.1", 200, null, proxied);
        System.setProperty("http.proxyHost", "127.0.0.1"); System.setProperty("http.proxyPort", String.valueOf(proxy.getAddress().getPort()));
        System.setProperty("http.nonProxyHosts", "");
        OtlpJsonExporter exporter = exporter(url(target), Map.of(), Map.of("FI_ALLOW_INSECURE_HTTP", "1"));
        assertTrue(exporter.export(spans()).isSuccess());
        assertTrue(exporter.export(spans()).isSuccess());
        assertEquals(2, hits.get()); assertEquals(0, proxied.get());
        assertEquals(2, err.toString().split("FI_ALLOW_INSECURE_HTTP", -1).length);
    }

    @Test void theExplicitOptionIsTheSameOptIn() throws Exception {
        AtomicInteger hits = new AtomicInteger();
        HttpServer target = server("127.0.0.2", 200, null, hits);
        assertTrue(exporter(url(target), Map.of("allow_insecure_http", "1"), Map.of()).export(spans()).isSuccess());
        assertEquals(1, hits.get());
    }

    @Test void aRedirectIsRefusedAndNothingReachesTheRedirectTarget() throws Exception {
        AtomicInteger first = new AtomicInteger(), second = new AtomicInteger();
        HttpServer elsewhere = server("127.0.0.1", 200, null, second);
        HttpServer origin = server("127.0.0.1", 307, url(elsewhere) + "/tracer/v1/traces", first);
        OtlpJsonExporter exporter = exporter(url(origin), Map.of(), Map.of());
        assertFalse(exporter.export(spans()).isSuccess());
        assertEquals("refused (redirect)", exporter.lastExport);
        assertEquals(1, first.get()); assertEquals(0, second.get());
        assertEquals(List.of("agent-api-key"), keys);
    }

    @Test void literalLoopbackPlaintextNeedsNoOptIn() throws Exception {
        AtomicInteger hits = new AtomicInteger();
        HttpServer target = server("127.0.0.1", 200, null, hits);
        assertTrue(exporter(url(target), Map.of(), Map.of()).export(spans()).isSuccess());
        assertEquals(1, hits.get());
        assertFalse(err.toString().contains("FI_ALLOW_INSECURE_HTTP"));
    }

    @Test void anHttpsExportKeepsTheJdkProxySettings() throws Exception {
        List<String> connects = new CopyOnWriteArrayList<>();
        try (java.net.ServerSocket proxy = new java.net.ServerSocket(0, 5, InetAddress.getByName("127.0.0.1"))) {
            Thread acceptor = new Thread(() -> {
                try (java.net.Socket socket = proxy.accept()) {
                    connects.add(new java.io.BufferedReader(new java.io.InputStreamReader(socket.getInputStream())).readLine());
                } catch (IOException ignored) { }
            });
            acceptor.setDaemon(true); acceptor.start();
            System.setProperty("https.proxyHost", "127.0.0.1"); System.setProperty("https.proxyPort", String.valueOf(proxy.getLocalPort()));
            System.setProperty("http.nonProxyHosts", "");
            try {
                exporter("https://api.futureagi.com", Map.of(), Map.of()).export(spans());
                acceptor.join(10000);
            } finally { System.clearProperty("https.proxyHost"); System.clearProperty("https.proxyPort"); }
        }
        assertEquals(1, connects.size());
        assertTrue(connects.get(0).startsWith("CONNECT api.futureagi.com:443"), connects.get(0));
    }
}
