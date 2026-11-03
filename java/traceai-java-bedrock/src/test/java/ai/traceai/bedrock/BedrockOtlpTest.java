package ai.traceai.bedrock;

import ai.traceai.FITracer;
import ai.traceai.TraceConfig;
import com.google.gson.JsonParser;
import com.sun.net.httpserver.HttpServer;
import io.opentelemetry.exporter.otlp.http.trace.OtlpHttpSpanExporter;
import io.opentelemetry.proto.collector.trace.v1.ExportTraceServiceRequest;
import io.opentelemetry.proto.common.v1.AnyValue;
import io.opentelemetry.proto.trace.v1.Span;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.export.SimpleSpanProcessor;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;

import java.net.InetSocketAddress;
import java.util.HashMap;
import java.util.Map;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.TimeUnit;

import static org.assertj.core.api.Assertions.*;

/** Exercises the actual OTLP/HTTP protobuf exporter, never an FI or model endpoint. */
class BedrockOtlpTest {
    @ParameterizedTest
    @ValueSource(booleans = {false, true})
    void exportsStructuredContentOrPrivateMetadataOnly(boolean hide) throws Exception {
        CompletableFuture<ExportTraceServiceRequest> received = new CompletableFuture<>();
        HttpServer server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        server.createContext("/v1/traces", exchange -> {
            try {
                received.complete(ExportTraceServiceRequest.parseFrom(exchange.getRequestBody().readAllBytes()));
                exchange.sendResponseHeaders(200, -1);
            } catch (Exception error) {
                received.completeExceptionally(error);
            } finally {
                exchange.close();
            }
        });
        server.start();
        try (SdkTracerProvider provider = SdkTracerProvider.builder().addSpanProcessor(SimpleSpanProcessor.create(
            OtlpHttpSpanExporter.builder().setEndpoint("http://127.0.0.1:" + server.getAddress().getPort() + "/v1/traces").build())).build()) {
            FITracer tracer = new FITracer(provider.get("bedrock-wire-test"), TraceConfig.builder()
                .apiKey("offline").secretKey("offline").projectName("offline")
                .hideInputs(hide).hideOutputs(hide).hideErrorDetails(true).build());
            new TracedBedrockRuntimeClient(BedrockTracingTest.stub(BedrockTracingTest.response()), tracer)
                .converse(BedrockTracingTest.request());
            provider.forceFlush().join(5, TimeUnit.SECONDS);
            ExportTraceServiceRequest request = received.get(5, TimeUnit.SECONDS);
            assertThat(request.getResourceSpansCount()).isEqualTo(1);
            assertThat(request.getResourceSpans(0).getScopeSpans(0).getSpansCount()).isEqualTo(1);
            Span span = request.getResourceSpans(0).getScopeSpans(0).getSpans(0);
            Map<String, AnyValue> attributes = new HashMap<>();
            span.getAttributesList().forEach(attribute -> attributes.put(attribute.getKey(), attribute.getValue()));
            assertThat(attributes.get("fi.span.kind").getStringValue()).isEqualTo("LLM");
            assertThat(attributes.get("gen_ai.provider.name").getStringValue()).isEqualTo("aws.bedrock");
            assertThat(attributes.get("gen_ai.bedrock.model_provider").getStringValue()).isEqualTo("anthropic");
            assertThat(attributes.get("gen_ai.usage.total_tokens").getIntValue()).isEqualTo(8);
            assertThat(request.toString()).doesNotContain("private-binary", "cHJpdmF0ZS1iaW5hcnk=");
            if (hide) {
                assertThat(attributes).doesNotContainKeys("gen_ai.input.messages", "gen_ai.output.messages",
                    "gen_ai.tool.definitions", "fi.raw_input", "fi.raw_output", "input.value", "output.value");
                assertThat(request.toString()).doesNotContain("question", "lookup", "tool-1", "tool-2");
            } else {
                String input = attributes.get("gen_ai.input.messages").getStringValue();
                assertThat(JsonParser.parseString(input).getAsJsonArray()).hasSize(3);
                assertThat(input).contains("image", "toolUse", "toolResult", "omitted");
                assertThat(attributes.get("gen_ai.output.messages").getStringValue()).contains("tool-2");
                assertThat(JsonParser.parseString(attributes.get("fi.raw_input").getStringValue()).isJsonObject()).isTrue();
            }
        } finally {
            server.stop(0);
        }
    }
}
