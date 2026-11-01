package ai.traceai.weaviate;

import ai.traceai.FITracer;
import ai.traceai.TraceConfig;
import com.sun.net.httpserver.HttpServer;
import com.google.protobuf.util.JsonFormat;
import io.opentelemetry.exporter.internal.otlp.traces.TraceRequestMarshaler;
import io.opentelemetry.exporter.otlp.http.trace.OtlpHttpSpanExporter;
import io.opentelemetry.proto.collector.trace.v1.ExportTraceServiceRequest;
import io.opentelemetry.proto.trace.v1.Status;
import io.opentelemetry.sdk.testing.exporter.InMemorySpanExporter;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.export.SimpleSpanProcessor;
import io.weaviate.client6.v1.api.collections.WeaviateObject;
import io.weaviate.client6.v1.api.collections.data.InsertManyRequest;
import io.weaviate.client6.v1.api.collections.query.FetchObjects;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.CsvSource;

import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.net.InetSocketAddress;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.TimeUnit;
import java.util.zip.GZIPInputStream;
import java.util.zip.GZIPOutputStream;

import static org.assertj.core.api.Assertions.*;

class WeaviateOtlpWireTest {
    @ParameterizedTest
    @CsvSource({"protobuf,false", "protobuf,true", "json,false", "json,true"})
    void exportsSemanticSpansWithPrivateErrors(String format, boolean gzip) throws Exception {
        var received = new CopyOnWriteArrayList<byte[]>();
        var contentTypes = new CopyOnWriteArrayList<String>();
        var encodings = new CopyOnWriteArrayList<String>();
        HttpServer receiver = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        receiver.createContext("/v1/traces", exchange -> {
            byte[] bytes = exchange.getRequestBody().readAllBytes();
            String encoding = exchange.getRequestHeaders().getFirst("Content-Encoding");
            encodings.add(encoding == null ? "identity" : encoding);
            if ("gzip".equals(encoding)) bytes = new GZIPInputStream(new ByteArrayInputStream(bytes)).readAllBytes();
            received.add(bytes);
            contentTypes.add(exchange.getRequestHeaders().getFirst("Content-Type"));
            exchange.sendResponseHeaders(200, -1);
            exchange.close();
        });
        receiver.start();
        String endpoint = "http://127.0.0.1:" + receiver.getAddress().getPort() + "/v1/traces";
        var capture = InMemorySpanExporter.create();
        try (var sdk = SdkTracerProvider.builder().addSpanProcessor(SimpleSpanProcessor.create(capture)).build();
             var server = new WeaviateV6Fixture(); var client = server.client()) {
            var tracer = new FITracer(sdk.get("wire-test"), TraceConfig.builder().hideErrorDetails(true).build());
            var collection = TracedWeaviateCollection.of(client, "Article", tracer);
            var parent = sdk.get("wire-test").spanBuilder("request").startSpan();
            try (var ignored = parent.makeCurrent()) {
                collection.fetchObjects(FetchObjects.of(q -> q.limit(4)));
                server.failSearch = true;
                assertThatThrownBy(() -> collection.fetchObjects(FetchObjects.of())).isInstanceOf(RuntimeException.class);
                server.partialBatch = true;
                collection.insertMany(new InsertManyRequest<>(WeaviateObject.<Map<String, Object>>of(
                        o -> o.properties(Map.of("title", WeaviateV6Fixture.PRIVATE)))));
            } finally {
                parent.end();
            }
            var spans = capture.getFinishedSpanItems();
            assertThat(spans).hasSize(4);
            if (format.equals("protobuf")) {
                var builder = OtlpHttpSpanExporter.builder().setEndpoint(endpoint);
                if (gzip) builder.setCompression("gzip");
                try (var exporter = builder.build()) {
                    assertThat(exporter.export(spans).join(5, TimeUnit.SECONDS).isSuccess()).isTrue();
                }
            } else {
                // The Java HTTP exporter uses protobuf. Validate the official OTel JSON
                // encoding as well, without claiming a new production JSON exporter.
                var bytes = new ByteArrayOutputStream();
                TraceRequestMarshaler.create(spans).writeJsonTo(bytes);
                if (gzip) {
                    var compressed = new ByteArrayOutputStream();
                    try (var stream = new GZIPOutputStream(compressed)) { stream.write(bytes.toByteArray()); }
                    bytes = compressed;
                }
                var request = HttpRequest.newBuilder(URI.create(endpoint)).timeout(Duration.ofSeconds(5))
                        .header("Content-Type", "application/json")
                        .POST(HttpRequest.BodyPublishers.ofByteArray(bytes.toByteArray()));
                if (gzip) request.header("Content-Encoding", "gzip");
                assertThat(HttpClient.newHttpClient().send(request.build(), HttpResponse.BodyHandlers.discarding()).statusCode()).isEqualTo(200);
            }
            assertThat(received).hasSize(1);
            assertThat(contentTypes).containsExactly(format.equals("json") ? "application/json" : "application/x-protobuf");
            assertThat(encodings).containsExactly(gzip ? "gzip" : "identity");
            ExportTraceServiceRequest decoded;
            if (format.equals("json")) {
                var builder = ExportTraceServiceRequest.newBuilder();
                JsonFormat.parser().merge(new String(received.get(0), StandardCharsets.UTF_8), builder);
                decoded = builder.build();
            } else {
                decoded = ExportTraceServiceRequest.parseFrom(received.get(0));
            }
            assertThat(decoded.toString()).doesNotContain(WeaviateV6Fixture.PRIVATE);
            List<io.opentelemetry.proto.trace.v1.Span> wireSpans = new ArrayList<>();
            decoded.getResourceSpansList().forEach(r -> r.getScopeSpansList().forEach(s -> wireSpans.addAll(s.getSpansList())));
            assertThat(wireSpans).hasSize(4);
            var retrieval = wireSpans.stream().filter(s -> s.getName().equals("Weaviate fetchObjects")
                    && s.getStatus().getCode() == Status.StatusCode.STATUS_CODE_OK).findFirst().orElseThrow();
            var attrs = retrieval.getAttributesList().stream().collect(java.util.stream.Collectors.toMap(a -> a.getKey(), a -> a.getValue()));
            assertThat(attrs.get("fi.span.kind").getStringValue()).isEqualTo("RETRIEVER");
            assertThat(attrs.get("db.system").getStringValue()).isEqualTo("weaviate");
            assertThat(attrs.get("db.vector.results.count").getIntValue()).isEqualTo(1);
            assertThat(wireSpans.stream().filter(s -> s.getStatus().getCode() == Status.StatusCode.STATUS_CODE_ERROR)).hasSize(2);
            assertThat(retrieval.getParentSpanId()).isNotEmpty();
            assertThat(wireSpans).allSatisfy(s -> assertThat(s.getTraceId()).isEqualTo(retrieval.getTraceId()));
        } finally {
            receiver.stop(0);
        }
    }
}
