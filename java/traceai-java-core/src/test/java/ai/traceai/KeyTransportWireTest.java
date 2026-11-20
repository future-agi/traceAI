package ai.traceai;

import com.sun.net.httpserver.HttpServer;
import io.opentelemetry.api.GlobalOpenTelemetry;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.PrintStream;
import java.net.InetAddress;
import java.net.InetSocketAddress;
import java.util.List;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.atomic.AtomicInteger;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/** What leaves the process, and what does not, when TraceAI owns the exporter. */
class KeyTransportWireTest {
    private final List<HttpServer> servers = new CopyOnWriteArrayList<>();
    private final List<String> keysSeen = new CopyOnWriteArrayList<>();
    private String oldProxyHost, oldProxyPort, oldNonProxy;
    private PrintStream originalErr;
    private ByteArrayOutputStream err;

    @BeforeEach void setUp() {
        TraceAI.shutdown(); GlobalOpenTelemetry.resetForTest(); KeyTransport.resetWarningForTest();
        oldProxyHost = System.getProperty("http.proxyHost"); oldProxyPort = System.getProperty("http.proxyPort"); oldNonProxy = System.getProperty("http.nonProxyHosts");
        originalErr = System.err; err = new ByteArrayOutputStream(); System.setErr(new PrintStream(err, true));
    }
    @AfterEach void tearDown() {
        System.setErr(originalErr);
        TraceAI.shutdown(); GlobalOpenTelemetry.resetForTest(); KeyTransport.resetWarningForTest();
        servers.forEach(s -> s.stop(0));
        restore("http.proxyHost", oldProxyHost); restore("http.proxyPort", oldProxyPort); restore("http.nonProxyHosts", oldNonProxy);
    }
    private static void restore(String key, String value) { if (value == null) System.clearProperty(key); else System.setProperty(key, value); }

    private HttpServer server(String address, int status, String location, AtomicInteger hits) throws IOException {
        HttpServer server = HttpServer.create(new InetSocketAddress(InetAddress.getByName(address), 0), 0);
        server.createContext("/", exchange -> {
            try (exchange) {
                hits.incrementAndGet();
                String key = exchange.getRequestHeaders().getFirst("X-Api-Key");
                if (key != null) keysSeen.add(key);
                exchange.getRequestBody().readAllBytes();
                if (location != null) exchange.getResponseHeaders().set("Location", location);
                exchange.sendResponseHeaders(status, -1);
            }
        });
        server.start(); servers.add(server);
        return server;
    }
    private static String url(HttpServer server) { return "http://" + server.getAddress().getAddress().getHostAddress() + ":" + server.getAddress().getPort(); }
    private static void exportOneSpan() {
        TraceAI.getTracer().startSpan("probe", FISpanKind.CHAIN).end();
        TraceAI.shutdown();
    }
    private static TraceConfig.Builder config(String baseUrl) {
        return TraceConfig.builder().baseUrl(baseUrl).apiKey("synthetic-api-key").secretKey("synthetic-secret-key").projectName("p").exportIntervalMs(60000);
    }

    @Test void futureAgiOverPlaintextIsRefusedBeforeAnyExporterExists() {
        assertThatThrownBy(() -> TraceAI.init(config("http://api.futureagi.com").build())).isInstanceOf(KeyTransport.Refused.class);
        assertThatThrownBy(() -> TraceAI.init(config("http://api.futureagi.com.").allowInsecureHttp(true).build())).isInstanceOf(KeyTransport.Refused.class);
        assertThat(TraceAI.isInitialized()).isFalse();
    }

    @Test void disagreeingTargetsAreRefusedByInit() {
        for (String target : new String[] {"http://127.0.0.1\\@evil.example", "http://localhost%5C@evil.example", "https://api.futureagi.com@evil.example", "http:///x"})
            assertThatThrownBy(() -> TraceAI.init(config(target).allowInsecureHttp(true).build())).isInstanceOf(KeyTransport.Refused.class);
        assertThat(TraceAI.isInitialized()).isFalse();
    }

    @Test void selfHostedPlaintextIsRefusedByDefaultAndSendsNothing() throws Exception {
        AtomicInteger hits = new AtomicInteger();
        HttpServer target = server("127.0.0.2", 200, null, hits);
        assertThatThrownBy(() -> TraceAI.init(config(url(target)).build())).isInstanceOf(KeyTransport.Refused.class);
        assertThat(hits).hasValue(0);
    }

    @Test void selfHostedPlaintextWithTheOptInSendsDirectlyAndWarnsOnce() throws Exception {
        AtomicInteger hits = new AtomicInteger(), proxied = new AtomicInteger();
        HttpServer target = server("127.0.0.2", 200, null, hits);
        HttpServer proxy = server("127.0.0.1", 200, null, proxied);
        System.setProperty("http.proxyHost", "127.0.0.1"); System.setProperty("http.proxyPort", String.valueOf(proxy.getAddress().getPort()));
        System.setProperty("http.nonProxyHosts", "");
        TraceAI.init(config(url(target)).allowInsecureHttp(true).build());
        exportOneSpan();
        assertThat(hits).hasValue(1);
        assertThat(proxied).hasValue(0);
        assertThat(err.toString().split("FI_ALLOW_INSECURE_HTTP", -1)).hasSize(2);
    }

    @Test void aRedirectIsNotFollowedAndTheKeysGoNowhereElse() throws Exception {
        AtomicInteger first = new AtomicInteger(), second = new AtomicInteger();
        HttpServer elsewhere = server("127.0.0.1", 200, null, second);
        HttpServer origin = server("127.0.0.1", 307, url(elsewhere) + "/tracer/v1/traces", first);
        TraceAI.init(config(url(origin)).build());
        exportOneSpan();
        assertThat(first).hasValue(1);
        assertThat(second).hasValue(0);
        assertThat(keysSeen).containsExactly("synthetic-api-key");
    }

    @Test void anIncompleteKeyPairSendsNoKeyAtAll() throws Exception {
        AtomicInteger hits = new AtomicInteger();
        HttpServer target = server("127.0.0.1", 200, null, hits);
        TraceAI.init(TraceConfig.builder().baseUrl(url(target)).apiKey("synthetic-api-key").projectName("p").exportIntervalMs(60000).build());
        exportOneSpan();
        assertThat(hits).hasValue(0);
        assertThat(keysSeen).isEmpty();
    }

    @Test void literalLoopbackPlaintextStillExports() throws Exception {
        AtomicInteger hits = new AtomicInteger();
        HttpServer target = server("127.0.0.1", 200, null, hits);
        TraceAI.init(config(url(target)).build());
        exportOneSpan();
        assertThat(hits).hasValue(1);
        assertThat(err.toString()).doesNotContain("FI_ALLOW_INSECURE_HTTP");
    }
}
