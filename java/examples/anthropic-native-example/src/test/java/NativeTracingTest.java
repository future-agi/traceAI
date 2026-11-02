import ai.traceai.FITracer;
import ai.traceai.anthropic.TracedAnthropicClient;
import com.anthropic.client.AnthropicClient;
import com.anthropic.client.okhttp.AnthropicOkHttpClient;
import com.anthropic.core.opentelemetry.OpenTelemetryContentMode;
import com.anthropic.core.opentelemetry.OpenTelemetryOptions;
import com.anthropic.core.opentelemetry.OpenTelemetryTraces;
import com.anthropic.models.messages.MessageCreateParams;
import com.sun.net.httpserver.HttpServer;
import io.opentelemetry.api.GlobalOpenTelemetry;
import io.opentelemetry.api.common.Attributes;
import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.context.propagation.ContextPropagators;
import io.opentelemetry.api.trace.propagation.W3CTraceContextPropagator;
import io.opentelemetry.sdk.OpenTelemetrySdk;
import io.opentelemetry.sdk.resources.Resource;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.export.SimpleSpanProcessor;
import io.opentelemetry.exporter.otlp.http.trace.OtlpHttpSpanExporter;
import io.opentelemetry.proto.collector.trace.v1.ExportTraceServiceRequest;
import io.opentelemetry.proto.trace.v1.Span;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;

import java.io.ByteArrayInputStream;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.TimeUnit;
import java.util.zip.GZIPInputStream;

import static org.junit.jupiter.api.Assertions.*;

class NativeTracingTest {
    static final String MODEL = "claude-opus-5-5";
    static final String INPUT = "synthetic-input-canary";
    static final String OUTPUT = "synthetic-output-canary";
    static final String MESSAGE = """
        {"id":"msg_synthetic","type":"message","role":"assistant",
         "model":"claude-opus-5-5","content":[{"type":"text","text":"synthetic-output-canary"}],
         "stop_reason":"end_turn","stop_sequence":null,
         "usage":{"input_tokens":11,"output_tokens":7,"cache_read_input_tokens":3,"cache_creation_input_tokens":2}}
        """;
    HttpServer server;
    SdkTracerProvider provider;
    AnthropicClient client;
    final List<ExportTraceServiceRequest> exports = new CopyOnWriteArrayList<>();
    final List<String> requests = new CopyOnWriteArrayList<>();
    final List<String> traceparents = new CopyOnWriteArrayList<>();
    final List<String> encodings = new CopyOnWriteArrayList<>();
    volatile String response = MESSAGE;
    volatile int status = 200;
    volatile String responseType = "application/json";
    volatile boolean finishTool;

    @BeforeEach
    void startServer() throws Exception {
        GlobalOpenTelemetry.resetForTest();
        server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        server.createContext("/v1/messages", exchange -> {
            requests.add(new String(exchange.getRequestBody().readAllBytes(), StandardCharsets.UTF_8));
            traceparents.add(String.valueOf(exchange.getRequestHeaders().getFirst("traceparent")));
            exchange.getResponseHeaders().set("Content-Type", responseType);
            exchange.getResponseHeaders().set("request-id", "req_synthetic");
            byte[] bytes = response.getBytes(StandardCharsets.UTF_8);
            if (finishTool) response = MESSAGE;
            exchange.sendResponseHeaders(status, bytes.length);
            exchange.getResponseBody().write(bytes);
            exchange.close();
        });
        server.createContext("/tracer/v1/traces", exchange -> {
            String encoding = exchange.getRequestHeaders().getFirst("Content-Encoding");
            encodings.add(String.valueOf(encoding));
            assertEquals("application/x-protobuf", exchange.getRequestHeaders().getFirst("Content-Type"));
            assertEquals("synthetic-fi-key", exchange.getRequestHeaders().getFirst("X-Api-Key"));
            assertEquals("synthetic-fi-secret", exchange.getRequestHeaders().getFirst("X-Secret-Key"));
            assertEquals("anthropic-java", exchange.getRequestHeaders().getFirst("X-Project-Name"));
            byte[] bytes = exchange.getRequestBody().readAllBytes();
            if ("gzip".equals(encoding)) {
                bytes = new GZIPInputStream(new ByteArrayInputStream(bytes)).readAllBytes();
            }
            exports.add(ExportTraceServiceRequest.parseFrom(bytes));
            exchange.getResponseHeaders().set("Content-Type", "application/x-protobuf");
            exchange.sendResponseHeaders(200, -1);
            exchange.close();
        });
        server.start();
    }

    String baseUrl() { return "http://127.0.0.1:" + server.getAddress().getPort(); }

    void configure(boolean content, boolean nativeTracing, boolean gzip) {
        var exporter = OtlpHttpSpanExporter.builder()
            .setEndpoint(baseUrl() + "/tracer/v1/traces")
            .setTimeout(Duration.ofSeconds(5))
            .addHeader("X-Api-Key", "synthetic-fi-key")
            .addHeader("X-Secret-Key", "synthetic-fi-secret")
            .addHeader("X-Project-Name", "anthropic-java");
        if (gzip) exporter.setCompression("gzip");
        provider = SdkTracerProvider.builder()
            .setResource(Resource.create(Attributes.of(AttributeKey.stringKey("project_name"), "anthropic-java")))
            .addSpanProcessor(SimpleSpanProcessor.create(exporter.build())).build();
        OpenTelemetrySdk.builder().setTracerProvider(provider)
            .setPropagators(ContextPropagators.create(W3CTraceContextPropagator.getInstance()))
            .buildAndRegisterGlobal();
        client = AnthropicOkHttpClient.builder().apiKey("synthetic-anthropic-key")
            .baseUrl(baseUrl()).maxRetries(0)
            .openTelemetry(OpenTelemetryOptions.builder()
                .traces(OpenTelemetryTraces.builder().enabled(nativeTracing)
                    .contentMode(content ? OpenTelemetryContentMode.CONTENT : OpenTelemetryContentMode.METADATA_ONLY)
                    .build()).build())
            .build();
    }

    MessageCreateParams params() {
        return MessageCreateParams.builder().model(MODEL).maxTokens(64).addUserMessage(INPUT).build();
    }

    List<Span> spans() {
        provider.forceFlush().join(5, TimeUnit.SECONDS);
        List<Span> result = new ArrayList<>();
        for (var request : exports) for (var resource : request.getResourceSpansList())
            for (var scope : resource.getScopeSpansList()) result.addAll(scope.getSpansList());
        return result;
    }

    static Object attr(Span span, String key) {
        return span.getAttributesList().stream().filter(a -> a.getKey().equals(key)).findFirst()
            .map(a -> a.getValue().hasIntValue() ? (Object) a.getValue().getIntValue() : a.getValue().getStringValue())
            .orElse(null);
    }

    @Test
    void legacyWrapperWithNativeTracingDisabledHasOneOwner() {
        configure(false, false, false);
        new TracedAnthropicClient(client, new FITracer(provider.get("legacy"))).createMessage(params());
        assertEquals(1, spans().size(), "Do not wrap a native-instrumented SDK: it duplicates the request observation");
    }

    @ParameterizedTest
    @ValueSource(booleans = {false, true})
    void nativeSyncExportsOneMetadataSpan(boolean gzip) throws Exception {
        configure(false, true, gzip);
        var parent = provider.get("application").spanBuilder("application").startSpan();
        try (var scope = parent.makeCurrent()) {
            assertEquals("msg_synthetic", client.messages().create(params()).id());
        } finally { parent.end(); }
        var all = spans();
        assertEquals(2, all.size());
        Span span = all.stream().filter(s -> s.getName().equals("anthropic.messages.create")).findFirst().orElseThrow();
        assertEquals(parent.getSpanContext().getSpanId(), java.util.HexFormat.of().formatHex(span.getParentSpanId().toByteArray()));
        assertEquals("anthropic", attr(span, "gen_ai.provider.name"));
        assertEquals(MODEL, attr(span, "gen_ai.request.model"));
        assertEquals(MODEL, attr(span, "gen_ai.response.model"));
        assertEquals(16L, attr(span, "gen_ai.usage.input_tokens"));
        assertEquals(7L, attr(span, "gen_ai.usage.output_tokens"));
        assertFalse(exports.toString().contains(INPUT));
        assertFalse(exports.toString().contains(OUTPUT));
        assertFalse(exports.toString().contains("synthetic-anthropic-key"));
        assertTrue(traceparents.get(0).matches("00-[a-f0-9]{32}-[a-f0-9]{16}-01"));
        assertEquals(gzip ? "gzip" : "null", encodings.get(0));
        String capture = System.getProperty("wire.capture");
        if (capture != null && !gzip) {
            var attributes = new java.util.LinkedHashMap<String, Object>();
            for (var attribute : span.getAttributesList()) {
                var value = attribute.getValue();
                if (value.hasIntValue()) attributes.put(attribute.getKey(), value.getIntValue());
                else if (value.hasStringValue()) attributes.put(attribute.getKey(), value.getStringValue());
            }
            java.nio.file.Files.writeString(java.nio.file.Path.of(capture + ".json"),
                new com.google.gson.Gson().toJson(attributes));
            java.nio.file.Files.write(java.nio.file.Path.of(capture + ".pb"), exports.get(0).toByteArray());
        }
    }

    @Test
    void nativeAsyncExportsOneSpan() throws Exception {
        configure(false, true, false);
        var parent = provider.get("application").spanBuilder("async-parent").startSpan();
        java.util.concurrent.CompletableFuture<com.anthropic.models.messages.Message> pending;
        try (var scope = parent.makeCurrent()) {
            pending = client.async().messages().create(params());
        }
        try {
            assertEquals("msg_synthetic", pending.get(10, TimeUnit.SECONDS).id());
            assertEquals(1, spans().size());
            Span call = spans().get(0);
            assertEquals(7L, attr(call, "gen_ai.usage.output_tokens"));
            assertEquals(parent.getSpanContext().getSpanId(), java.util.HexFormat.of().formatHex(call.getParentSpanId().toByteArray()));
        } finally { parent.end(); }
    }

    @Test
    void nativeContentOptIn() {
        configure(true, true, true);
        client.messages().create(params());
        Span span = spans().get(0);
        assertTrue(attr(span, "gen_ai.input.messages").toString().contains(INPUT));
        assertTrue(attr(span, "gen_ai.output.messages").toString().contains(OUTPUT));
        assertEquals(1, spans().size());
    }

    @Test
    void nativeErrorsDoNotExportResponseBody() {
        configure(false, true, true);
        status = 400;
        response = "{\"type\":\"error\",\"error\":{\"type\":\"invalid_request_error\",\"message\":\"synthetic-error-canary\"}}";
        assertThrows(com.anthropic.errors.BadRequestException.class, () -> client.messages().create(params()));
        var all = spans();
        assertEquals(1, all.size());
        assertEquals(io.opentelemetry.proto.trace.v1.Status.StatusCode.STATUS_CODE_ERROR, all.get(0).getStatus().getCode());
        assertNotNull(attr(all.get(0), "error.type"));
        assertFalse(exports.toString().contains("synthetic-error-canary"));
        assertFalse(exports.toString().contains(INPUT));
    }

    @Test
    void combiningTracingOwnersDuplicatesUsage() {
        configure(false, true, false);
        new TracedAnthropicClient(client, new FITracer(provider.get("legacy"))).createMessage(params());
        var all = spans();
        assertEquals(2, all.size());
        assertTrue(all.stream().anyMatch(s -> s.getName().equals("Anthropic Message")));
        assertTrue(all.stream().anyMatch(s -> s.getName().equals("anthropic.messages.create")));
        assertTrue(all.stream().allMatch(s -> attr(s, "gen_ai.usage.output_tokens") != null
            || attr(s, "llm.token_count.completion") != null));
    }

    void streamResponse() {
        responseType = "text/event-stream";
        response = """
            event: message_start
            data: {"type":"message_start","message":{"id":"msg_stream","type":"message","role":"assistant","model":"claude-opus-5-5","content":[],"stop_reason":null,"stop_sequence":null,"usage":{"input_tokens":11,"output_tokens":0,"cache_read_input_tokens":3,"cache_creation_input_tokens":2}}}

            event: content_block_start
            data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}

            event: content_block_delta
            data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"synthetic-output-canary"}}

            event: content_block_stop
            data: {"type":"content_block_stop","index":0}

            event: message_delta
            data: {"type":"message_delta","delta":{"stop_reason":"end_turn","stop_sequence":null},"usage":{"output_tokens":7}}

            event: message_stop
            data: {"type":"message_stop"}

            """;
    }

    @ParameterizedTest
    @ValueSource(booleans = {false, true})
    void streamEndsOnceAfterConsumption(boolean content) {
        configure(content, true, true);
        streamResponse();
        try (var stream = client.messages().createStreaming(params())) {
            assertTrue(spans().isEmpty(), "An open stream must not end the span");
            assertEquals(6, stream.stream().count());
        }
        var all = spans();
        assertEquals(1, all.size());
        assertEquals(16L, attr(all.get(0), "gen_ai.usage.input_tokens"));
        assertEquals(7L, attr(all.get(0), "gen_ai.usage.output_tokens"));
        assertEquals(content, exports.toString().contains(OUTPUT));
    }

    @Test
    void earlyStreamCloseEndsExactlyOnce() {
        configure(false, true, false);
        streamResponse();
        try (var stream = client.messages().createStreaming(params())) {
            assertTrue(stream.stream().findFirst().isPresent());
        }
        assertEquals(1, spans().size());
        assertEquals(io.opentelemetry.proto.trace.v1.Status.StatusCode.STATUS_CODE_UNSET, spans().get(0).getStatus().getCode());
    }

    @Test
    void toolUseContentIsCapturedOnlyWhenEnabled() {
        configure(true, true, false);
        response = MESSAGE.replace("{\"type\":\"text\",\"text\":\"synthetic-output-canary\"}",
            "{\"type\":\"tool_use\",\"id\":\"tool_synthetic\",\"name\":\"get_weather\",\"input\":{\"city\":\"Paris\"}}")
            .replace("end_turn", "tool_use");
        var message = client.messages().create(params());
        assertTrue(message.content().get(0).isToolUse());
        var all = spans();
        assertEquals(1, all.size());
        assertTrue(attr(all.get(0), "gen_ai.output.messages").toString().contains("get_weather"));
        assertTrue(attr(all.get(0), "gen_ai.output.messages").toString().contains("Paris"));
    }

    @com.fasterxml.jackson.annotation.JsonClassDescription("Return synthetic weather")
    public static class GetWeather implements java.util.function.Supplier<String> {
        public String city;
        @Override public String get() { return "Sunny in " + city; }
    }

    @ParameterizedTest
    @ValueSource(booleans = {false, true})
    void nativeToolRunnerPreservesHierarchyAndCountsRequestsOnly(boolean content) {
        configure(content, true, true);
        finishTool = true;
        response = MESSAGE.replace("{\"type\":\"text\",\"text\":\"synthetic-output-canary\"}",
            "{\"type\":\"tool_use\",\"id\":\"tool_synthetic\",\"name\":\"get_weather\",\"input\":{\"city\":\"Paris\"}}")
            .replace("end_turn", "tool_use");
        var runner = client.beta().messages().toolRunner(
            com.anthropic.models.beta.messages.MessageCreateParams.builder()
                .model(MODEL).maxTokens(64).addTool(GetWeather.class).addUserMessage(INPUT).build());
        int messages = 0;
        for (var message : runner) messages++;
        assertEquals(2, messages);
        var all = spans();
        assertEquals(4, all.size(), () -> "Spans: " + all.stream().map(Span::getName).toList() + "; requests: " + requests);
        Span root = all.stream().filter(s -> s.getName().equals("anthropic.messages.tool_runner")).findFirst().orElseThrow();
        assertNull(attr(root, "gen_ai.usage.input_tokens"));
        var calls = all.stream().filter(s -> s.getName().equals("anthropic.messages.create")).toList();
        assertEquals(2, calls.size());
        for (Span call : calls) assertEquals(root.getSpanId(), call.getParentSpanId());
        assertEquals(32L, calls.stream().mapToLong(s -> (Long) attr(s, "gen_ai.usage.input_tokens")).sum());
        Span tool = all.stream().filter(s -> s.getName().equals("anthropic.custom_tool_use get_weather")).findFirst().orElseThrow();
        assertEquals(root.getSpanId(), tool.getParentSpanId());
        assertNull(attr(tool, "gen_ai.usage.input_tokens"));
        assertTrue(requests.get(1).contains("Sunny in Paris"));
        assertEquals(content, exports.toString().contains("Paris"));
    }

    @Test
    void documentedExampleExportsFromCleanProcess() throws Exception {
        var processBuilder = new ProcessBuilder(
            System.getProperty("java.home") + "/bin/java", "-Djava.net.preferIPv4Stack=true",
            "-cp", System.getProperty("java.class.path"), "AnthropicTracingExample");
        processBuilder.environment().clear();
        processBuilder.environment().putAll(Map.of(
            "ANTHROPIC_API_KEY", "synthetic-anthropic-key",
            "ANTHROPIC_BASE_URL", baseUrl(),
            "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", baseUrl() + "/tracer/v1/traces",
            "FI_API_KEY", "synthetic-fi-key", "FI_SECRET_KEY", "synthetic-fi-secret",
            "FI_PROJECT_NAME", "anthropic-java"));
        processBuilder.redirectErrorStream(true);
        var process = processBuilder.start();
        try {
            assertTrue(process.waitFor(20, TimeUnit.SECONDS));
            String output = new String(process.getInputStream().readAllBytes(), StandardCharsets.UTF_8);
            assertEquals(0, process.exitValue(), output);
            assertTrue(output.contains("msg_synthetic"));
            assertEquals(1, exports.size());
            var resource = exports.get(0).getResourceSpans(0);
            assertTrue(resource.getResource().getAttributesList().stream().anyMatch(a ->
                a.getKey().equals("project_name") && a.getValue().getStringValue().equals("anthropic-java")));
            assertEquals(1, resource.getScopeSpans(0).getSpansCount());
            assertEquals("anthropic.messages.create", resource.getScopeSpans(0).getSpans(0).getName());
        } finally { process.destroyForcibly(); }
    }

    @AfterEach
    void close() {
        if (client != null) client.close();
        if (provider != null) provider.close();
        if (server != null) server.stop(0);
        GlobalOpenTelemetry.resetForTest();
    }
}
