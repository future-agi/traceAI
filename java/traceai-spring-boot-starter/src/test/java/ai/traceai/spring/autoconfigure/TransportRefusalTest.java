package ai.traceai.spring.autoconfigure;

import ai.traceai.FITracer;
import ai.traceai.TraceAI;
import com.sun.net.httpserver.HttpServer;
import io.opentelemetry.api.GlobalOpenTelemetry;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.boot.autoconfigure.AutoConfigurations;
import org.springframework.boot.test.context.runner.ApplicationContextRunner;

import java.net.InetAddress;
import java.net.InetSocketAddress;
import java.util.concurrent.atomic.AtomicInteger;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * How the starter surfaces a refused key transport. The opt-in property is the explicit form of FI_ALLOW_INSECURE_HTTP=1 (other
 * hosts only, never a Future AGI host, never proxied). The failure-mode property only chooses how a refusal shows: throw while
 * the context starts, or log one value-free warning and disable export; it never lets a refused send through.
 */
class TransportRefusalTest {
    private final ApplicationContextRunner runner = new ApplicationContextRunner().withConfiguration(AutoConfigurations.of(TraceAIAutoConfiguration.class));
    private HttpServer server;
    private final AtomicInteger hits = new AtomicInteger();

    @BeforeEach void start() throws Exception {
        TraceAI.shutdown(); GlobalOpenTelemetry.resetForTest();
        server = HttpServer.create(new InetSocketAddress(InetAddress.getByName("127.0.0.2"), 0), 0);
        server.createContext("/", exchange -> { try (exchange) { hits.incrementAndGet(); exchange.getRequestBody().readAllBytes(); exchange.sendResponseHeaders(200, -1); } });
        server.start();
    }
    @AfterEach void stop() { TraceAI.shutdown(); GlobalOpenTelemetry.resetForTest(); server.stop(0); }

    private String target() { return "http://127.0.0.2:" + server.getAddress().getPort(); }
    private ApplicationContextRunner configured(String baseUrl, String... more) {
        String[] common = {"traceai.base-url=" + baseUrl, "traceai.api-key=synthetic-api-key", "traceai.secret-key=synthetic-secret-key", "traceai.project-name=p", "traceai.export-interval-ms=60000"};
        String[] all = new String[common.length + more.length];
        System.arraycopy(common, 0, all, 0, common.length); System.arraycopy(more, 0, all, common.length, more.length);
        return runner.withPropertyValues(all);
    }

    @Test void aRefusedTargetFailsTheContextByDefaultWithAValueFreeMessage() {
        configured(target()).run(context -> {
            assertThat(context).hasFailed();
            String message = String.valueOf(context.getStartupFailure());
            assertThat(message).contains("FI_ALLOW_INSECURE_HTTP").doesNotContain("synthetic-api-key").doesNotContain("synthetic-secret-key").doesNotContain("127.0.0.2");
        });
        assertThat(hits).hasValue(0);
    }

    @Test void theOptInPropertyAllowsPlaintextToAnotherHostAndExports() {
        configured(target(), "traceai.allow-insecure-http=true").run(context -> {
            assertThat(context).hasNotFailed();
            context.getBean(FITracer.class).startSpan("probe", ai.traceai.FISpanKind.CHAIN).end();
            TraceAI.shutdown();
        });
        assertThat(hits).hasValue(1);
    }

    @Test void theOptInPropertyNeverAppliesToAFutureAgiHost() {
        configured("http://api.futureagi.com", "traceai.allow-insecure-http=true").run(context -> assertThat(context).hasFailed());
        configured("http://api.futureagi.com", "traceai.allow-insecure-http=true", "traceai.fail-on-refused-transport=false").run(context -> assertThat(context).hasNotFailed());
        assertThat(hits).hasValue(0);
    }

    @Test void theFailureModePropertyOnlyChoosesHowARefusalShowsAndExportsNothing() {
        configured(target(), "traceai.fail-on-refused-transport=false").run(context -> {
            assertThat(context).hasNotFailed();
            context.getBean(FITracer.class).startSpan("probe", ai.traceai.FISpanKind.CHAIN).end();
            TraceAI.shutdown();
        });
        assertThat(hits).as("the collector saw a request for a refused configuration").hasValue(0);
    }
}
