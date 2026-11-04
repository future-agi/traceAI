package ai.traceai.weaviate;

import ai.traceai.FITracer;
import ai.traceai.TraceConfig;
import com.sun.net.httpserver.HttpServer;
import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.sdk.testing.exporter.InMemorySpanExporter;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.data.SpanData;
import io.opentelemetry.sdk.trace.export.SimpleSpanProcessor;
import io.weaviate.client.Config;
import io.weaviate.client.WeaviateClient;
import io.weaviate.client.v1.data.model.WeaviateObject;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.Arguments;
import org.junit.jupiter.params.provider.MethodSource;
import org.junit.jupiter.params.provider.ValueSource;

import java.io.IOException;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.nio.file.Path;
import java.util.Arrays;
import java.util.Map;
import java.util.concurrent.TimeUnit;
import java.util.stream.Stream;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.SoftAssertions.assertSoftly;

/** Real client 5.5.0 against loopback HTTP; no Weaviate service or credentials required. */
class TracedWeaviateLegacyTest {
    private static final String PRIVATE = "synthetic-private-document-and-credential";
    private static final String ERROR_BODY = "{\"error\":[{\"message\":\"" + PRIVATE + "\"}]}";
    private static final String SUCCESS = "{\"id\":\"00000000-0000-0000-0000-000000000001\","
            + "\"class\":\"Article\",\"result\":{\"status\":\"SUCCESS\"}}";
    private static final String FAILED = "{\"id\":\"00000000-0000-0000-0000-000000000002\","
            + "\"class\":\"Article\",\"result\":{\"status\":\"FAILED\",\"errors\":" + ERROR_BODY + "}}";

    @ParameterizedTest
    @ValueSource(booleans = {true, false})
    void nearVectorErrorRespectsExplicitPrivacy(boolean hide) throws Exception {
        assertNearVectorError(TraceConfig.builder().hideErrorDetails(hide).build(), hide);
    }

    @Test
    void nearVectorErrorRespectsEnvironmentPrivacy() throws Exception {
        // Use a fresh JVM so this also covers the actual environment default without
        // reflection into System.getenv or leaking environment changes to other tests.
        ProcessBuilder builder = new ProcessBuilder(
                Path.of(System.getProperty("java.home"), "bin", "java").toString(),
                "-cp", System.getProperty("surefire.test.class.path", System.getProperty("java.class.path")),
                EnvironmentPrivacyProbe.class.getName()).redirectErrorStream(true);
        builder.environment().put("FI_HIDE_ERROR_DETAILS", "true");
        Process child = builder.start();
        try {
            assertThat(child.waitFor(30, TimeUnit.SECONDS)).as("environment privacy probe completed").isTrue();
            String output = new String(child.getInputStream().readAllBytes(), StandardCharsets.UTF_8);
            assertThat(child.exitValue()).as("environment privacy probe: %s", output).isZero();
        } finally {
            child.destroyForcibly();
        }
    }

    public static class EnvironmentPrivacyProbe {
        public static void main(String[] args) throws Exception {
            TraceConfig config = TraceConfig.builder().build();
            assertThat(config.isHideErrorDetails()).isTrue();
            assertNearVectorError(config, true);
            // An explicit opt-out must still override the environment default.
            assertNearVectorError(TraceConfig.builder().hideErrorDetails(false).build(), false);
        }
    }

    private static void assertNearVectorError(TraceConfig config, boolean hide) throws Exception {
        try (Fixture fixture = new Fixture(422, ERROR_BODY, config)) {
            var result = fixture.traced.nearVectorSearch("Article", new Float[]{1f, 2f}, 1, "title");
            // Privacy is for telemetry, not the SDK result the caller needs to handle.
            assertThat(result.hasErrors()).isTrue();
            assertThat(result.getError().getStatusCode()).isEqualTo(422);
            assertThat(result.getError().toString()).contains(PRIVATE);
            SpanData span = fixture.onlySpan();
            assertThat(span.getStatus().getStatusCode()).isEqualTo(StatusCode.ERROR);
            assertThat(span.getAttributes().get(AttributeKey.booleanKey("weaviate.has_errors"))).isTrue();
            if (hide) {
                assertThat(span.getAttributes().get(AttributeKey.stringKey("weaviate.error"))).isNull();
                assertPrivate(span);
            } else {
                assertThat(span.getAttributes().get(AttributeKey.stringKey("weaviate.error"))).contains(PRIVATE);
            }
        }
    }

    @Test
    void graphqlErrorsInHttpSuccessKeepErrorStatusWithoutPrivateDetails() throws Exception {
        String body = "{\"data\":{},\"errors\":[{\"message\":\"" + PRIVATE + "\"}]}";
        try (Fixture fixture = new Fixture(200, body, TraceConfig.builder().hideErrorDetails(true).build())) {
            var result = fixture.traced.nearVectorSearch("Article", new Float[]{1f, 2f}, 1, "title");
            assertThat(result.getResult().getErrors()).hasSize(1);
            assertThat(fixture.onlySpan().getStatus().getStatusCode()).isEqualTo(StatusCode.ERROR);
            assertPrivate(fixture.onlySpan());
        }
    }

    static Stream<Arguments> batchResponses() {
        return Stream.of(
                Arguments.of("all success", "[" + SUCCESS + "," + SUCCESS + "]", 2, 2L, 0L),
                Arguments.of("mixed", "[" + SUCCESS + "," + FAILED + "]", 2, 1L, 1L),
                Arguments.of("all failed", "[" + FAILED + "]", 1, 0L, 1L),
                Arguments.of("failed status only", "[{\"result\":{\"status\":\"FAILED\"}}]", 1, 0L, 1L),
                Arguments.of("errors despite success status", "[" + FAILED.replace("FAILED", "SUCCESS") + "]", 1, 0L, 1L),
                Arguments.of("empty errors", "[{\"result\":{\"status\":\"SUCCESS\",\"errors\":{\"error\":[]}}}]", 1, 1L, 0L),
                Arguments.of("missing result", "[{}]", 1, 0L, 1L),
                Arguments.of("null item", "[null]", 1, 0L, 1L));
    }

    @ParameterizedTest(name = "{0}")
    @MethodSource("batchResponses")
    void batchCountsOnlySuccessfulObjects(String name, String body, int size, long imported, long failed) throws Exception {
        // Batch bodies are never captured, even when error-detail hiding is disabled.
        try (Fixture fixture = new Fixture(200, body, TraceConfig.builder().hideErrorDetails(false).build())) {
            WeaviateObject[] objects = new WeaviateObject[size];
            Arrays.fill(objects, object());
            var result = fixture.traced.batchImport(objects);
            assertThat(result.hasErrors()).as("outer SDK result is still HTTP success").isFalse();
            assertThat(result.getResult()).hasSize(size);
            if (body.contains(PRIVATE)) {
                assertThat(Arrays.toString(result.getResult())).as("caller retains item error details").contains(PRIVATE);
            }
            SpanData span = fixture.onlySpan();
            assertSoftly(soft -> {
                soft.assertThat(span.getAttributes().get(AttributeKey.longKey("weaviate.batch_size"))).isEqualTo((long) size);
                soft.assertThat(span.getAttributes().get(AttributeKey.longKey("weaviate.imported_count"))).isEqualTo(imported);
                soft.assertThat(span.getAttributes().get(AttributeKey.longKey("weaviate.failed_count"))).isEqualTo(failed);
                soft.assertThat(span.getStatus().getStatusCode()).isEqualTo(failed > 0 ? StatusCode.ERROR : StatusCode.OK);
                if (failed > 0) {
                    soft.assertThat(span.getAttributes().get(AttributeKey.booleanKey("weaviate.has_errors"))).isTrue();
                }
            });
            assertPrivate(span);
        }
    }

    @Test
    void batchTransportErrorRemainsAnErrorWithoutInventingItemCounts() throws Exception {
        try (Fixture fixture = new Fixture(422, ERROR_BODY, TraceConfig.builder().hideErrorDetails(true).build())) {
            var result = fixture.traced.batchImport(object());
            assertThat(result.hasErrors()).isTrue();
            assertThat(result.getError().toString()).contains(PRIVATE);
            SpanData span = fixture.onlySpan();
            assertThat(span.getStatus().getStatusCode()).isEqualTo(StatusCode.ERROR);
            assertThat(span.getAttributes().get(AttributeKey.booleanKey("weaviate.has_errors"))).isTrue();
            assertThat(span.getAttributes().get(AttributeKey.longKey("weaviate.imported_count"))).isNull();
            assertThat(span.getAttributes().get(AttributeKey.longKey("weaviate.failed_count"))).isNull();
            assertPrivate(span);
        }
    }

    private static WeaviateObject object() {
        return WeaviateObject.builder().className("Article").properties(Map.of("title", PRIVATE)).build();
    }

    private static void assertPrivate(SpanData span) {
        assertThat(span.getAttributes().toString() + span.getEvents() + span.getStatus()).doesNotContain(PRIVATE);
    }

    private static class Fixture implements AutoCloseable {
        private final HttpServer server;
        private final InMemorySpanExporter exporter = InMemorySpanExporter.create();
        private final SdkTracerProvider provider = SdkTracerProvider.builder()
                .addSpanProcessor(SimpleSpanProcessor.create(exporter)).build();
        private final TracedWeaviateClient traced;

        Fixture(int status, String body, TraceConfig config) throws IOException {
            server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
            server.createContext("/", exchange -> {
                exchange.getRequestBody().readAllBytes();
                // The legacy batch client fetches metadata before submitting objects.
                boolean meta = exchange.getRequestURI().getPath().equals("/v1/meta");
                byte[] bytes = (meta ? "{\"version\":\"1.30.0\"}" : body).getBytes(StandardCharsets.UTF_8);
                exchange.getResponseHeaders().set("Content-Type", "application/json");
                exchange.sendResponseHeaders(meta ? 200 : status, bytes.length);
                exchange.getResponseBody().write(bytes);
                exchange.close();
            });
            server.start();
            WeaviateClient client = new WeaviateClient(new Config("http", "127.0.0.1:" + server.getAddress().getPort()));
            traced = new TracedWeaviateClient(client, new FITracer(provider.get("legacy-test"), config));
        }

        SpanData onlySpan() {
            assertThat(exporter.getFinishedSpanItems()).hasSize(1);
            return exporter.getFinishedSpanItems().get(0);
        }

        @Override
        public void close() {
            server.stop(0);
            provider.close();
        }
    }
}
