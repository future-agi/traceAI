package ai.traceai.vertexai;

import ai.traceai.TraceAI;
import ai.traceai.TraceConfig;
import com.google.cloud.vertexai.generativeai.GenerativeModel;
import com.sun.net.httpserver.HttpServer;
import io.grpc.Status;
import io.opentelemetry.api.GlobalOpenTelemetry;
import io.opentelemetry.proto.collector.trace.v1.ExportTraceServiceRequest;
import io.opentelemetry.proto.trace.v1.Span;
import org.junit.jupiter.api.Test;

import java.net.InetSocketAddress;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.stream.Collectors;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

class VertexAIOtlpWireTest {
    @Test
    void exportsThroughTheMaintainedSdkWithCorrectHeadersAndSemanticFields() throws Exception {
        var bodies = new CopyOnWriteArrayList<byte[]>();
        var apiKeys = new CopyOnWriteArrayList<String>();
        var secretKeys = new CopyOnWriteArrayList<String>();
        var contentTypes = new CopyOnWriteArrayList<String>();
        HttpServer receiver = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        receiver.createContext("/tracer/v1/traces", exchange -> {
            bodies.add(exchange.getRequestBody().readAllBytes());
            apiKeys.add(exchange.getRequestHeaders().getFirst("X-Api-Key"));
            secretKeys.add(exchange.getRequestHeaders().getFirst("X-Secret-Key"));
            contentTypes.add(exchange.getRequestHeaders().getFirst("Content-Type"));
            exchange.sendResponseHeaders(200, -1);
            exchange.close();
        });
        receiver.start();
        GlobalOpenTelemetry.resetForTest();
        try (var service = new FakeVertexService()) {
            TraceAI.init(TraceConfig.builder()
                .baseUrl("http://127.0.0.1:" + receiver.getAddress().getPort())
                .apiKey("synthetic-api-key").secretKey("synthetic-secret-key")
                .projectName("vertex-test").hideErrorDetails(true).build());
            var traced = new TracedGenerativeModel(new GenerativeModel("gemini-test", service.vertexAI));
            service.streamChunks = List.of(VertexAIStreamTracingTest.chunk("Hel"), VertexAIStreamTracingTest.lastChunk("lo"));
            assertThat(traced.generateContentStream("hello").stream().count()).isEqualTo(2);
            service.streamError = Status.INVALID_ARGUMENT.withDescription("private-diagnostic");
            assertThatThrownBy(() -> traced.generateContentStream("hello").stream().count())
                .isInstanceOf(RuntimeException.class);
            TraceAI.shutdown();
            assertThat(bodies).isNotEmpty();
            assertThat(apiKeys).allSatisfy(value -> assertThat(value).isEqualTo("synthetic-api-key"));
            assertThat(secretKeys).allSatisfy(value -> assertThat(value).isEqualTo("synthetic-secret-key"));
            assertThat(contentTypes).allSatisfy(value -> assertThat(value).isEqualTo("application/x-protobuf"));
            List<Span> spans = new ArrayList<>();
            for (byte[] body : bodies) {
                var decoded = ExportTraceServiceRequest.parseFrom(body);
                assertThat(decoded.toString()).doesNotContain("private-diagnostic");
                decoded.getResourceSpansList().forEach(resource -> {
                    var attrs = resource.getResource().getAttributesList().stream()
                        .collect(Collectors.toMap(a -> a.getKey(), a -> a.getValue()));
                    assertThat(attrs.get("project_name").getStringValue()).isEqualTo("vertex-test");
                    resource.getScopeSpansList().forEach(scope -> spans.addAll(scope.getSpansList()));
                });
            }
            assertThat(spans).hasSize(2);
            assertThat(spans).allSatisfy(span -> {
                var attrs = span.getAttributesList().stream().collect(Collectors.toMap(a -> a.getKey(), a -> a.getValue()));
                assertThat(attrs.get("fi.span.kind").getStringValue()).isEqualTo("LLM");
                assertThat(attrs.get("gen_ai.provider.name").getStringValue()).isEqualTo("google");
                assertThat(attrs.get("gen_ai.request.model").getStringValue()).isEqualTo("gemini-test");
                assertThat(attrs.get("output.value").getStringValue()).isEqualTo("Hello");
                assertThat(attrs.get("gen_ai.usage.total_tokens").getIntValue()).isEqualTo(5);
            });
            assertThat(spans.get(0).getStatus().getCodeValue()).isEqualTo(1);
            assertThat(spans.get(1).getStatus().getCodeValue()).isEqualTo(2);
        } finally {
            TraceAI.shutdown();
            GlobalOpenTelemetry.resetForTest();
            receiver.stop(0);
        }
    }
}
