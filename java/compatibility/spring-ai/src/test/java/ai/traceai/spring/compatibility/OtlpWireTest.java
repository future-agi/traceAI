package ai.traceai.spring.compatibility;

import ai.traceai.spring.SpringAIObservationFilter;
import com.google.protobuf.util.JsonFormat;
import com.sun.net.httpserver.HttpServer;
import io.opentelemetry.exporter.internal.otlp.traces.TraceRequestMarshaler;
import io.opentelemetry.exporter.otlp.http.trace.OtlpHttpSpanExporter;
import io.opentelemetry.proto.collector.trace.v1.ExportTraceServiceRequest;
import io.opentelemetry.proto.common.v1.AnyValue;
import io.opentelemetry.proto.common.v1.KeyValue;
import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.net.InetSocketAddress;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.List;
import java.util.concurrent.LinkedBlockingQueue;
import java.util.concurrent.TimeUnit;
import java.util.zip.GZIPInputStream;
import java.util.zip.GZIPOutputStream;
import org.junit.jupiter.api.Timeout;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.CsvSource;
import org.springframework.ai.chat.prompt.Prompt;
import static org.junit.jupiter.api.Assertions.*;

@Timeout(30)
class OtlpWireTest {
    @ParameterizedTest
    @CsvSource({"false,false,false", "false,true,false", "true,false,false", "true,true,false",
                "false,false,true", "false,true,true", "true,false,true", "true,true,true"})
    void nativeSpanSurvivesWireWithoutUnrequestedContent(boolean json, boolean gzip, boolean content) throws Exception {
        var received = new LinkedBlockingQueue<Received>();
        var receiver = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        receiver.createContext("/tracer/v1/traces", exchange -> {
            received.add(new Received(exchange.getRequestBody().readAllBytes(),
                exchange.getRequestHeaders().getFirst("Content-Type"),
                exchange.getRequestHeaders().getFirst("Content-Encoding"),
                exchange.getRequestHeaders().getFirst("X-Api-Key"),
                exchange.getRequestHeaders().getFirst("X-Secret-Key")));
            exchange.sendResponseHeaders(200, 0);
            exchange.close();
        });
        receiver.start();
        String endpoint = "http://127.0.0.1:" + receiver.getAddress().getPort() + "/tracer/v1/traces";
        try (var f = new NativeSpringAITest.Fixture(new SpringAIObservationFilter(content, content))) {
            f.reply(NativeSpringAITest.CHAT);
            Models.chat(f.url(), f.registry).call(new Prompt(NativeSpringAITest.INPUT));
            var span = f.onlySpan();
            if (json) {
                // Java's OTLP HTTP exporter uses protobuf. Exercise its official JSON marshaler
                // separately; this is a wire-compatibility check, not a custom production exporter.
                var bytes = new ByteArrayOutputStream();
                TraceRequestMarshaler.create(List.of(span)).writeJsonTo(bytes);
                byte[] body = bytes.toByteArray();
                if (gzip) {
                    var compressed = new ByteArrayOutputStream();
                    try (var stream = new GZIPOutputStream(compressed)) { stream.write(body); }
                    body = compressed.toByteArray();
                }
                var request = HttpRequest.newBuilder(URI.create(endpoint)).timeout(Duration.ofSeconds(5))
                    .header("Content-Type", "application/json")
                    .header("X-Api-Key", "synthetic-api-key").header("X-Secret-Key", "synthetic-secret-key");
                if (gzip) request.header("Content-Encoding", "gzip");
                {
                    var http = HttpClient.newHttpClient();
                    assertEquals(200, http.send(request.POST(HttpRequest.BodyPublishers.ofByteArray(body)).build(),
                        HttpResponse.BodyHandlers.discarding()).statusCode());
                }
            } else {
                var builder = OtlpHttpSpanExporter.builder().setEndpoint(endpoint)
                    .setTimeout(Duration.ofSeconds(5)).addHeader("X-Api-Key", "synthetic-api-key")
                    .addHeader("X-Secret-Key", "synthetic-secret-key");
                if (gzip) builder.setCompression("gzip");
                try (var exporter = builder.build()) {
                    var result = exporter.export(List.of(span));
                    result.join(5, TimeUnit.SECONDS);
                    assertTrue(result.isSuccess());
                }
            }
            Received wire = received.poll(5, TimeUnit.SECONDS);
            assertNotNull(wire);
            assertEquals(json ? "application/json" : "application/x-protobuf", wire.contentType());
            assertEquals(gzip ? "gzip" : null, wire.encoding());
            assertEquals("synthetic-api-key", wire.apiKey());
            assertEquals("synthetic-secret-key", wire.secretKey());
            byte[] payload = wire.body();
            if (gzip) try (var stream = new GZIPInputStream(new ByteArrayInputStream(payload))) { payload = stream.readAllBytes(); }
            ExportTraceServiceRequest request;
            if (json) {
                var builder = ExportTraceServiceRequest.newBuilder();
                var tree = com.google.gson.JsonParser.parseString(new String(payload, StandardCharsets.UTF_8));
                normalizeOtlpIds(tree);
                JsonFormat.parser().merge(tree.toString(), builder);
                request = builder.build();
            } else request = ExportTraceServiceRequest.parseFrom(payload);
            assertEquals(1, request.getResourceSpansCount());
            var resource = request.getResourceSpans(0);
            assertEquals("spring-ai-test", value(resource.getResource().getAttributesList(), "project_name"));
            assertEquals(1, resource.getScopeSpansCount());
            assertEquals(1, resource.getScopeSpans(0).getSpansCount());
            var exported = resource.getScopeSpans(0).getSpans(0);
            assertEquals("LLM", value(exported.getAttributesList(), "fi.span.kind"));
            assertEquals("synthetic-model", value(exported.getAttributesList(), "gen_ai.response.model"));
            assertEquals("3", value(exported.getAttributesList(), "gen_ai.usage.input_tokens"));
            assertEquals(content ? NativeSpringAITest.INPUT : null, value(exported.getAttributesList(), "input.value"));
            assertEquals(content ? NativeSpringAITest.OUTPUT : null, value(exported.getAttributesList(), "output.value"));
            assertEquals(span.getSpanId(), java.util.HexFormat.of().formatHex(exported.getSpanId().toByteArray()));
            if (!content) {
                String decoded = JsonFormat.printer().print(request);
                assertFalse(decoded.contains(NativeSpringAITest.INPUT));
                assertFalse(decoded.contains(NativeSpringAITest.OUTPUT));
            }
        } finally { receiver.stop(0); }
    }

    // OTLP JSON uses hex IDs, unlike protobuf's generic JSON mapping (base64 bytes).
    private static void normalizeOtlpIds(com.google.gson.JsonElement element) {
        if (element.isJsonArray()) {
            element.getAsJsonArray().forEach(OtlpWireTest::normalizeOtlpIds);
        } else if (element.isJsonObject()) {
            var object = element.getAsJsonObject();
            for (var entry : object.entrySet()) {
                if (List.of("traceId", "spanId", "parentSpanId").contains(entry.getKey())) {
                    byte[] id = java.util.HexFormat.of().parseHex(entry.getValue().getAsString());
                    entry.setValue(new com.google.gson.JsonPrimitive(java.util.Base64.getEncoder().encodeToString(id)));
                } else normalizeOtlpIds(entry.getValue());
            }
        }
    }

    private static String value(List<KeyValue> attributes, String key) {
        return attributes.stream().filter(v -> v.getKey().equals(key)).map(KeyValue::getValue)
            .map(AnyValue::getStringValue).findFirst().orElse(null);
    }
    private record Received(byte[] body, String contentType, String encoding, String apiKey, String secretKey) {}
}
