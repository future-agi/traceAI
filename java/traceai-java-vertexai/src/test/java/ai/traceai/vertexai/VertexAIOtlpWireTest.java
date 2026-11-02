package ai.traceai.vertexai;

import ai.traceai.TraceAI;
import ai.traceai.TraceConfig;
import com.google.cloud.vertexai.generativeai.GenerativeModel;
import com.google.cloud.vertexai.api.Content;
import com.google.cloud.vertexai.api.Part;
import com.google.gson.JsonParser;
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
    void exportsUnicodePrefixesAndRawTruncationFlagsWithoutCorruptingPayloads() throws Exception {
        var bodies = new CopyOnWriteArrayList<byte[]>();
        HttpServer receiver = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        receiver.createContext("/tracer/v1/traces", exchange -> {
            bodies.add(exchange.getRequestBody().readAllBytes());
            exchange.sendResponseHeaders(200, -1);
            exchange.close();
        });
        receiver.start();
        GlobalOpenTelemetry.resetForTest();
        try (var service = new FakeVertexService()) {
            TraceAI.init(TraceConfig.builder()
                .baseUrl("http://127.0.0.1:" + receiver.getAddress().getPort())
                .apiKey("synthetic-api-key").secretKey("synthetic-secret-key")
                .projectName("vertex-boundaries").build());
            var traced = new TracedGenerativeModel(new GenerativeModel("gemini-test", service.vertexAI));
            String prefix = "a".repeat(31999);
            String answer = prefix + "🚀rest";
            service.streamChunks = List.of(VertexAIStreamTracingTest.chunk(answer),
                VertexAIStreamTracingTest.lastChunk("later"));
            String returned = traced.generateContentStream("hello").stream()
                .map(r -> r.getCandidates(0).getContent().getParts(0).getText()).collect(Collectors.joining());
            assertThat(returned).isEqualTo(answer + "later");
            String large = "b".repeat(40000);
            service.unaryResponse = VertexAIStreamTracingTest.lastChunk(large);
            assertThat(traced.generateContent(List.of(Content.newBuilder().setRole("user")
                .addParts(Part.newBuilder().setText(large)).build()))).isEqualTo(service.unaryResponse);
            TraceAI.shutdown();
            List<Span> spans = new ArrayList<>();
            for (byte[] body : bodies) {
                ExportTraceServiceRequest.parseFrom(body).getResourceSpansList().forEach(resource ->
                    resource.getScopeSpansList().forEach(scope -> spans.addAll(scope.getSpansList())));
            }
            assertThat(spans).hasSize(2);
            for (Span span : spans) {
                var attrs = span.getAttributesList().stream().collect(Collectors.toMap(a -> a.getKey(), a -> a.getValue()));
                assertThat(span.getStatus().getCodeValue()).isEqualTo(1);
                if (span.getName().endsWith("(Stream)")) {
                    String value = attrs.get("output.value").getStringValue();
                    assertThat(value).isEqualTo(prefix);
                    assertThat(returned).startsWith(value);
                    assertThat(attrs.get("fi.stream.raw_output_truncated").getBoolValue()).isTrue();
                    assertThat(JsonParser.parseString(attrs.get("raw.output").getStringValue()).isJsonArray()).isTrue();
                } else {
                    assertThat(attrs).doesNotContainKeys("raw.input", "raw.output");
                    assertThat(attrs.get("fi.raw_input_truncated").getBoolValue()).isTrue();
                    assertThat(attrs.get("fi.raw_output_truncated").getBoolValue()).isTrue();
                    assertThat(JsonParser.parseString(attrs.get("gen_ai.input.messages").getStringValue()).isJsonArray()).isTrue();
                }
                assertThat(JsonParser.parseString(attrs.get("gen_ai.output.messages").getStringValue()).isJsonArray()).isTrue();
            }
        } finally {
            TraceAI.shutdown();
            GlobalOpenTelemetry.resetForTest();
            receiver.stop(0);
        }
    }

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
            service.unaryResponse = VertexAIStreamTracingTest.lastChunk("Hello");
            traced.generateContent(List.of(Content.newBuilder().setRole("user")
                .addParts(Part.newBuilder().setText("hello")).build()));
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
            assertThat(spans).hasSize(3);
            assertThat(spans).allSatisfy(span -> {
                var attrs = span.getAttributesList().stream().collect(Collectors.toMap(a -> a.getKey(), a -> a.getValue()));
                assertThat(attrs.get("fi.span.kind").getStringValue()).isEqualTo("LLM");
                assertThat(attrs.get("gen_ai.provider.name").getStringValue()).isEqualTo("google");
                assertThat(attrs.get("gen_ai.request.model").getStringValue()).isEqualTo("gemini-test");
                assertThat(attrs.get("output.value").getStringValue()).isEqualTo("Hello");
                assertThat(attrs).doesNotContainKeys("fi.raw_input", "fi.raw_output");
                assertThat(attrs.get("raw.output").hasStringValue()).isTrue();
                var rawOutput = JsonParser.parseString(attrs.get("raw.output").getStringValue());
                if (span.getName().endsWith("(Stream)")) {
                    assertThat(rawOutput.isJsonArray()).isTrue();
                } else {
                    assertThat(rawOutput.isJsonObject()).isTrue();
                    assertThat(attrs.get("raw.input").hasStringValue()).isTrue();
                    var rawInput = JsonParser.parseString(attrs.get("raw.input").getStringValue()).getAsJsonArray();
                    assertThat(rawInput.get(0).getAsJsonObject().get("role").getAsString()).isEqualTo("user");
                }
                assertThat(attrs.get("gen_ai.response.finish_reasons").hasArrayValue()).isTrue();
                assertThat(attrs.get("gen_ai.response.finish_reasons").getArrayValue().getValuesList())
                    .singleElement().satisfies(reason -> {
                        assertThat(reason.hasStringValue()).isTrue();
                        assertThat(reason.getStringValue()).isEqualTo("STOP");
                    });
                assertThat(attrs.get("gen_ai.usage.total_tokens").getIntValue()).isEqualTo(5);
            });
            assertThat(spans.get(0).getStatus().getCodeValue()).isEqualTo(1);
            assertThat(spans.get(1).getStatus().getCodeValue()).isEqualTo(2);
            assertThat(spans.get(2).getStatus().getCodeValue()).isEqualTo(1);
        } finally {
            TraceAI.shutdown();
            GlobalOpenTelemetry.resetForTest();
            receiver.stop(0);
        }
    }
}
