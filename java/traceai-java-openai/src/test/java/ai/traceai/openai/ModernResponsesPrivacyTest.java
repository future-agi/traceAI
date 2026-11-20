package ai.traceai.openai;

import ai.traceai.FITracer;
import ai.traceai.TraceConfig;
import com.openai.client.OpenAIClientImpl;
import com.openai.client.okhttp.OkHttpClient;
import com.openai.core.ClientOptions;
import com.openai.core.JsonValue;
import com.openai.models.responses.Response;
import com.openai.models.responses.ResponseCreateParams;
import com.openai.models.responses.Tool;
import com.sun.net.httpserver.HttpServer;
import io.opentelemetry.exporter.otlp.http.trace.OtlpHttpSpanExporter;
import io.opentelemetry.proto.collector.trace.v1.ExportTraceServiceRequest;
import io.opentelemetry.proto.common.v1.AnyValue;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.export.SimpleSpanProcessor;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.Arguments;
import org.junit.jupiter.params.provider.MethodSource;
import java.net.InetSocketAddress;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.Map;
import java.util.concurrent.LinkedBlockingQueue;
import java.util.concurrent.TimeUnit;
import java.util.stream.Collectors;
import java.util.stream.Stream;
import static org.assertj.core.api.Assertions.assertThat;

/** Real provider HTTP and decoded OTLP boundaries, including attributes, events and resources. */
class ModernResponsesPrivacyTest {
    private static final String INSTRUCTION = "synthetic-private-instruction";
    private static final String TOKEN = "synthetic-mcp-token";
    private static final String HEADER = "synthetic-mcp-header";
    private static final String CUSTOM_HEADER = "synthetic-custom-header";
    private static final String API_KEY = "synthetic-openai-key";
    private static final String MCP = "{\"type\":\"mcp\",\"server_label\":\"example\","
        + "\"server_url\":\"https://mcp.example.invalid/mcp\",\"authorization\":\"" + TOKEN
        + "\",\"headers\":{\"Authorization\":\"Bearer " + HEADER + "\",\"X-Custom\":\"" + CUSTOM_HEADER + "\"}}";

    static Stream<Arguments> credentialCases() {
        return Stream.of(false, true).flatMap(stream -> Stream.of("none", "inputs", "inputMessages", "outputs", "outputMessages")
            .map(hiding -> Arguments.of(stream, hiding)));
    }

    @ParameterizedTest @MethodSource("credentialCases")
    void mcpAuthenticationReachesProviderButNotOtlp(boolean stream, String hiding) throws Exception {
        // Some compatible servers echo tool definitions. Neither direction is an authentication log.
        String json = OpenAILoopback.RESPONSE.replace("\"output\":[", "\"tools\":[" + MCP + "],\"output\":[");
        try (var wire = new OtlpReceiver();
             var server = new OpenAILoopback(stream ? "text/event-stream" : "application/json", payload(json, stream));
             var provider = wire.provider()) {
            var client = new OpenAIClientImpl(ClientOptions.builder().httpClient(TracingHttpClient.wrap(
                OkHttpClient.builder().build(), new FITracer(provider.get("privacy"), config(hiding))))
                .baseUrl(server.url()).apiKey(API_KEY).maxRetries(0).build());
            try (AutoCloseable close = client::close) {
                var mcp = Tool.Mcp.builder().serverLabel("example").serverUrl("https://mcp.example.invalid/mcp")
                    .authorization(TOKEN).headers(Tool.Mcp.Headers.builder()
                        .putAdditionalProperty("Authorization", JsonValue.from("Bearer " + HEADER))
                        .putAdditionalProperty("X-Custom", JsonValue.from(CUSTOM_HEADER)).build()).build();
                var params = ResponseCreateParams.builder().model("requested-model").input("public-question")
                    .addTool(Tool.ofMcp(mcp)).build();
                var result = call(client, params, stream);
                assertThat(result.tools().get(0).mcp().orElseThrow().authorization()).contains(TOKEN);
                assertThat(result.tools().get(0).mcp().orElseThrow().headers().orElseThrow().toString())
                    .contains(HEADER, CUSTOM_HEADER);
                String request = server.requests.poll(10, TimeUnit.SECONDS);
                assertThat(request).contains(TOKEN, HEADER, CUSTOM_HEADER, API_KEY);
                var exported = wire.receive("credentials-" + stream + "-" + hiding, request);
                var attributes = attributes(exported);
                assertMetadata(exported, attributes);
                if (!hiding.equals("inputs") && !hiding.equals("inputMessages")) {
                    assertThat(attributes.get("fi.raw_input").getStringValue()).contains("public-question", "server_label");
                } else assertThat(attributes).doesNotContainKeys("fi.raw_input", "input.value");
                if (!hiding.equals("outputs") && !hiding.equals("outputMessages")) {
                    assertThat(attributes.get("output.value").getStringValue()).contains("private-answer");
                    assertThat(attributes.get("fi.raw_output").getStringValue()).contains("server_label");
                } else assertThat(attributes).doesNotContainKeys("fi.raw_output", "output.value");
                assertThat(exported.toString()).doesNotContain(TOKEN, HEADER, CUSTOM_HEADER, API_KEY);
            }
        }
    }

    static Stream<Arguments> instructionCases() {
        return Stream.of(false, true).flatMap(stream -> Stream.of(false, true).flatMap(messages ->
            Stream.of("none", "inputs", "inputMessages", "outputs", "outputMessages")
                .map(hiding -> Arguments.of(stream, messages, hiding))));
    }

    @ParameterizedTest @MethodSource("instructionCases")
    void echoedInstructionsFollowInputPrivacy(boolean stream, boolean messages, String hiding) throws Exception {
        String instructions = messages
            ? "[{\"type\":\"message\",\"role\":\"developer\",\"content\":\"" + INSTRUCTION + "\"}]"
            : "\"" + INSTRUCTION + "\"";
        String json = OpenAILoopback.RESPONSE.replace("\"output\":[", "\"instructions\":" + instructions + ",\"output\":[");
        try (var wire = new OtlpReceiver();
             var server = new OpenAILoopback(stream ? "text/event-stream" : "application/json", payload(json, stream));
             var provider = wire.provider()) {
            var client = new OpenAIClientImpl(ClientOptions.builder().httpClient(TracingHttpClient.wrap(
                OkHttpClient.builder().build(), new FITracer(provider.get("privacy"), config(hiding))))
                .baseUrl(server.url()).apiKey(API_KEY).maxRetries(0).build());
            try (AutoCloseable close = client::close) {
                var params = ResponseCreateParams.builder().model("requested-model").input("public-question")
                    .instructions(INSTRUCTION).build();
                assertThat(call(client, params, stream).instructions().orElseThrow().toString()).contains(INSTRUCTION);
                String request = server.requests.poll(10, TimeUnit.SECONDS);
                assertThat(request).contains(INSTRUCTION, "public-question");
                var exported = wire.receive("instructions-" + stream + "-" + messages + "-" + hiding, request);
                var attributes = attributes(exported);
                assertMetadata(exported, attributes);
                if (hiding.equals("inputs") || hiding.equals("inputMessages")) {
                    assertThat(attributes).doesNotContainKeys("fi.raw_input", "input.value");
                    // This Java adapter documents omission, not a replacement string, for hidden fields.
                    assertThat(attributes.get("fi.raw_output").getStringValue()).doesNotContain("instructions");
                    assertThat(exported.toString()).doesNotContain(INSTRUCTION, "public-question");
                } else {
                    assertThat(attributes.get("fi.raw_input").getStringValue()).contains(INSTRUCTION);
                    if (hiding.equals("none")) assertThat(attributes.get("fi.raw_output").getStringValue()).contains(INSTRUCTION);
                }
                if (hiding.equals("outputs") || hiding.equals("outputMessages")) {
                    assertThat(attributes).doesNotContainKeys("output.value", "fi.raw_output");
                    assertThat(exported.toString()).doesNotContain("private-answer");
                } else assertThat(attributes.get("output.value").getStringValue()).contains("private-answer");
                assertThat(exported.toString()).doesNotContain(API_KEY);
            }
        }
    }

    private static TraceConfig config(String hiding) {
        return TraceConfig.builder().hideInputs(hiding.equals("inputs")).hideOutputs(hiding.equals("outputs"))
            .hideInputMessages(hiding.equals("inputMessages")).hideOutputMessages(hiding.equals("outputMessages")).build();
    }

    private static String payload(String json, boolean stream) {
        return stream ? "event: response.completed\ndata: {\"type\":\"response.completed\",\"sequence_number\":1,\"response\":"
            + json + "}\n\n" : json;
    }

    private static Response call(com.openai.client.OpenAIClient client, ResponseCreateParams params, boolean stream) {
        if (!stream) return client.responses().create(params);
        try (var events = client.responses().createStreaming(params)) {
            var values = events.stream().toList();
            assertThat(values).hasSize(1);
            return values.get(0).completed().orElseThrow().response();
        }
    }

    private static Map<String, AnyValue> attributes(ExportTraceServiceRequest exported) {
        assertThat(exported.getResourceSpansList()).hasSize(1);
        var resource = exported.getResourceSpans(0);
        assertThat(resource.getScopeSpansList()).hasSize(1);
        assertThat(resource.getScopeSpans(0).getSpansList()).hasSize(1);
        return resource.getScopeSpans(0).getSpans(0).getAttributesList().stream()
            .collect(Collectors.toMap(a -> a.getKey(), a -> a.getValue()));
    }

    private static void assertMetadata(ExportTraceServiceRequest exported, Map<String, AnyValue> attributes) {
        assertThat(exported.getResourceSpans(0).getScopeSpans(0).getSpans(0).getStatus().getCode().name())
            .isEqualTo("STATUS_CODE_OK");
        assertThat(attributes.get("gen_ai.response.id").getStringValue()).isEqualTo("resp-test");
        assertThat(attributes.get("gen_ai.response.model").getStringValue()).isEqualTo("served-model");
        assertThat(attributes.get("gen_ai.usage.total_tokens").getIntValue()).isEqualTo(15);
    }

    private static final class OtlpReceiver implements AutoCloseable {
        private final LinkedBlockingQueue<byte[]> received = new LinkedBlockingQueue<>();
        private final HttpServer server;

        OtlpReceiver() throws Exception {
            server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
            server.createContext("/v1/traces", exchange -> {
                received.add(exchange.getRequestBody().readAllBytes());
                exchange.sendResponseHeaders(200, -1);
                exchange.close();
            });
            server.start();
        }

        SdkTracerProvider provider() {
            var exporter = OtlpHttpSpanExporter.builder()
                .setEndpoint("http://127.0.0.1:" + server.getAddress().getPort() + "/v1/traces").build();
            return SdkTracerProvider.builder().addSpanProcessor(SimpleSpanProcessor.create(exporter)).build();
        }

        ExportTraceServiceRequest receive(String name, String request) throws Exception {
            byte[] bytes = received.poll(10, TimeUnit.SECONDS);
            assertThat(bytes).isNotNull();
            var exported = ExportTraceServiceRequest.parseFrom(bytes);
            Path evidence = Path.of("target", "privacy-wire");
            Files.createDirectories(evidence);
            Files.write(evidence.resolve(name + ".pb"), bytes);
            Files.writeString(evidence.resolve(name + ".txt"), exported.toString());
            Files.writeString(evidence.resolve(name + "-provider.txt"), request);
            return exported;
        }

        @Override public void close() { server.stop(0); }
    }
}
