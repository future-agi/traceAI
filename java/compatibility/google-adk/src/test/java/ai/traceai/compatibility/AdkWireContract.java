package ai.traceai.compatibility;

import static org.junit.jupiter.api.Assertions.*;

import ai.traceai.nativeotel.GoogleAdkTelemetry;
import com.google.adk.agents.LlmAgent;
import com.google.adk.events.Event;
import com.google.adk.models.BaseLlm;
import com.google.adk.models.BaseLlmConnection;
import com.google.adk.models.LlmRequest;
import com.google.adk.models.LlmResponse;
import com.google.adk.runner.InMemoryRunner;
import com.google.adk.sessions.Session;
import com.google.adk.tools.Annotations.Schema;
import com.google.adk.tools.FunctionTool;
import com.google.genai.types.Content;
import com.google.genai.types.FunctionCall;
import com.google.genai.types.GenerateContentResponseUsageMetadata;
import com.google.genai.types.Part;
import com.google.gson.JsonParser;
import com.sun.net.httpserver.HttpServer;
import io.opentelemetry.api.baggage.Baggage;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.context.Context;
import io.opentelemetry.context.Scope;
import io.opentelemetry.exporter.otlp.http.trace.OtlpHttpSpanExporter;
import io.opentelemetry.proto.collector.trace.v1.ExportTraceServiceRequest;
import io.opentelemetry.proto.common.v1.AnyValue;
import io.opentelemetry.proto.common.v1.KeyValue;
import io.opentelemetry.proto.trace.v1.Status;
import io.opentelemetry.sdk.OpenTelemetrySdk;
import io.reactivex.rxjava3.core.Flowable;
import io.reactivex.rxjava3.schedulers.Schedulers;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.stream.Collectors;

/** Real ADK runner/model/tool execution, no network except a loopback OTLP receiver. */
final class AdkWireContract {
    static void run(boolean captureContent) throws Exception {
        List<ExportTraceServiceRequest> requests = new CopyOnWriteArrayList<>();
        List<String> headerFailures = new CopyOnWriteArrayList<>();
        List<byte[]> bodies = new CopyOnWriteArrayList<>();
        HttpServer receiver = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        receiver.createContext("/tracer/v1/traces", exchange -> {
            if (!"test-key".equals(exchange.getRequestHeaders().getFirst("X-Api-Key"))) headerFailures.add("api key");
            if (!"test-secret".equals(exchange.getRequestHeaders().getFirst("X-Secret-Key"))) headerFailures.add("secret key");
            if (!"application/x-protobuf".equals(exchange.getRequestHeaders().getFirst("Content-Type"))) headerFailures.add("content type");
            if (exchange.getRequestHeaders().getFirst("Content-Encoding") != null) headerFailures.add("compression");
            byte[] bytes = exchange.getRequestBody().readAllBytes();
            bodies.add(bytes);
            requests.add(ExportTraceServiceRequest.parseFrom(bytes));
            exchange.getResponseHeaders().add("Content-Type", "application/x-protobuf");
            exchange.sendResponseHeaders(200, -1);
            exchange.close();
        });
        receiver.start();
        GoogleAdkTelemetry.Config config = new GoogleAdkTelemetry.Config(
                "http://127.0.0.1:" + receiver.getAddress().getPort(), "test-key", "test-secret", "adk-contract");
        // Nothing above refers to ADK's tracer. This is real bootstrap, not setTracerForTesting.
        OpenTelemetrySdk sdk = captureContent
                ? GoogleAdkTelemetry.register(config, OtlpHttpSpanExporter.builder().setEndpoint(config.tracesEndpoint())
                    .addHeader("X-Api-Key", config.apiKey()).addHeader("X-Secret-Key", config.secretKey()).build(), true)
                : GoogleAdkTelemetry.register(config);
        String parentId;
        String traceId;
        try {
            Span parent = sdk.getTracer("test-parent").spanBuilder("request").startSpan();
            parentId = parent.getSpanContext().getSpanId();
            traceId = parent.getSpanContext().getTraceId();
            Context context = Context.root().with(parent).with(Baggage.builder()
                    .put("session.id", "test-session").put("user.id", "test-user").build());
            try (Scope ignored = context.makeCurrent()) {
                assertEquals("private-final", runAgent(false));
                RuntimeException failure = assertThrows(RuntimeException.class, () -> runAgent(true));
                assertTrue(failure.toString().contains("private-error"));
            } finally {
                parent.end();
            }
        } finally {
            // No forceFlush: normal shutdown itself must export the pending batch.
            GoogleAdkTelemetry.shutdown(sdk);
            receiver.stop(0);
        }
        assertFalse(requests.isEmpty(), "shutdown must export pending spans");
        assertTrue(headerFailures.isEmpty(), headerFailures.toString());
        List<io.opentelemetry.proto.trace.v1.Span> nativeSpans = new ArrayList<>();
        for (var request : requests) {
            for (var resource : request.getResourceSpansList()) {
                assertEquals("adk-contract", text(resource.getResource().getAttributesList(), "project_name"));
                assertEquals("observe", text(resource.getResource().getAttributesList(), "project_type"));
                for (var scope : resource.getScopeSpansList()) {
                    if (scope.getScope().getName().equals("gcp.vertex.agent")) nativeSpans.addAll(scope.getSpansList());
                }
            }
        }
        assertFalse(nativeSpans.isEmpty());
        HashSet<String> spanIds = new HashSet<>();
        spanIds.add(parentId);
        for (var span : nativeSpans) {
            assertTrue(spanIds.add(hex(span.getSpanId())), "a native span must not be exported twice");
            assertEquals(traceId, hex(span.getTraceId()), span.getName());
            assertEquals("test-session", text(span.getAttributesList(), "session.id"), span.getName());
            assertEquals("test-user", text(span.getAttributesList(), "user.id"), span.getName());
            assertTrue(span.getEndTimeUnixNano() >= span.getStartTimeUnixNano());
        }
        for (var span : nativeSpans) {
            assertTrue(spanIds.contains(hex(span.getParentSpanId())), "lost RxJava parent for " + span.getName());
        }
        List<io.opentelemetry.proto.trace.v1.Span> llms = nativeSpans.stream()
                .filter(s -> s.getName().equals("call_llm") && s.getStatus().getCode() != Status.StatusCode.STATUS_CODE_ERROR).toList();
        assertEquals(2, llms.size(), "one native span per scripted model call");
        for (var llm : llms) {
            assertEquals("LLM", text(llm.getAttributesList(), "fi.span.kind"));
            assertEquals("offline-model", text(llm.getAttributesList(), "gen_ai.request.model"));
            assertEquals(7, attr(llm.getAttributesList(), "gen_ai.usage.input_tokens").getIntValue());
            assertEquals(3, attr(llm.getAttributesList(), "gen_ai.usage.output_tokens").getIntValue());
        }
        var tools = nativeSpans.stream().filter(s -> s.getName().startsWith("execute_tool ")).toList();
        assertEquals(1, tools.size());
        var tool = tools.get(0);
        assertEquals("TOOL", text(tool.getAttributesList(), "fi.span.kind"));
        assertTrue(nativeSpans.stream().anyMatch(s -> "AGENT".equals(text(s.getAttributesList(), "fi.span.kind"))));
        assertTrue(nativeSpans.stream().anyMatch(s -> "CHAIN".equals(text(s.getAttributesList(), "fi.span.kind"))));
        assertTrue(nativeSpans.stream().anyMatch(s -> s.getStatus().getCode() == Status.StatusCode.STATUS_CODE_ERROR));
        if (captureContent) {
            assertEquals("private-paris", JsonParser.parseString(text(tool.getAttributesList(), "input.value"))
                    .getAsJsonObject().get("city").getAsString());
            assertEquals("private-sunny", JsonParser.parseString(text(tool.getAttributesList(), "output.value"))
                    .getAsJsonObject().get("weather").getAsString());
            assertEquals("application/json", text(tool.getAttributesList(), "input.mime_type"));
            assertEquals("call-weather", text(tool.getAttributesList(), "gen_ai.tool.call.id"));
            String messages = llms.stream().map(s -> text(s.getAttributesList(), "gen_ai.input.messages"))
                    .collect(Collectors.joining());
            assertTrue(messages.contains("private-question"));
            assertTrue(messages.contains("tool_call_response"));
            String outputs = llms.stream().map(s -> text(s.getAttributesList(), "gen_ai.output.messages"))
                    .collect(Collectors.joining());
            assertTrue(outputs.contains("tool_call"));
            assertTrue(outputs.contains("private-final"));
            assertTrue(outputs.contains("assistant"));
            assertTrue(bodies.stream().anyMatch(bytes -> new String(bytes, StandardCharsets.ISO_8859_1).contains("private-error")));
        } else {
            for (byte[] bytes : bodies) {
                assertFalse(new String(bytes, StandardCharsets.ISO_8859_1).contains("private-"), "content/error leaked onto OTLP wire");
            }
            for (var span : nativeSpans) {
                assertTrue(span.getStatus().getMessage().isEmpty());
                assertEquals(0, span.getEventsCount());
                assertNull(attr(span.getAttributesList(), "input.value"));
                assertNull(attr(span.getAttributesList(), "gen_ai.input.messages"));
            }
        }
    }

    private static String runAgent(boolean fail) {
        LlmAgent agent = LlmAgent.builder().name("weather_agent")
                .model(new ScriptedModel(fail)).instruction("Use the weather tool.")
                .tools(FunctionTool.create(Weather.class, "weather")).build();
        InMemoryRunner runner = new InMemoryRunner(agent);
        Session session = runner.sessionService().createSession(runner.appName(), "test-user").blockingGet();
        StringBuilder answer = new StringBuilder();
        runner.runAsync("test-user", session.id(), Content.fromParts(Part.fromText("private-question")))
                .blockingForEach(event -> {
                    if (event.finalResponse()) answer.append(event.stringifyContent());
                });
        return answer.toString();
    }

    public static final class Weather {
        @Schema(name = "weather", description = "Weather lookup")
        public static Map<String, Object> weather(@Schema(name = "city", description = "City") String city) {
            return Map.of("weather", "private-sunny", "city", city);
        }
    }

    private static final class ScriptedModel extends BaseLlm {
        private final AtomicInteger calls = new AtomicInteger();
        private final boolean fail;
        ScriptedModel(boolean fail) { super("offline-model"); this.fail = fail; }
        @Override public Flowable<LlmResponse> generateContent(LlmRequest request, boolean stream) {
            if (fail) return Flowable.<LlmResponse>error(new IllegalStateException("private-error")).subscribeOn(Schedulers.computation());
            Part part = calls.getAndIncrement() == 0
                    ? Part.builder().functionCall(FunctionCall.builder().id("call-weather").name("weather")
                        .args(Map.of("city", "private-paris")).build()).build()
                    : Part.fromText("private-final");
            LlmResponse response = LlmResponse.builder()
                    .content(Content.builder().role("model").parts(List.of(part)).build())
                    .usageMetadata(GenerateContentResponseUsageMetadata.builder().promptTokenCount(7)
                        .candidatesTokenCount(3).totalTokenCount(10).build()).build();
            // Force an actual RxJava thread hop, not only same-thread synthetic spans.
            return Flowable.just(response).subscribeOn(Schedulers.computation());
        }
        @Override public BaseLlmConnection connect(LlmRequest request) { throw new UnsupportedOperationException("offline fixture"); }
    }

    private static AnyValue attr(List<KeyValue> attributes, String key) {
        return attributes.stream().filter(a -> a.getKey().equals(key)).map(KeyValue::getValue).findFirst().orElse(null);
    }
    private static String text(List<KeyValue> attributes, String key) {
        AnyValue value = attr(attributes, key);
        return value == null ? null : value.getStringValue();
    }
    private static String hex(com.google.protobuf.ByteString bytes) {
        return java.util.HexFormat.of().formatHex(bytes.toByteArray());
    }
}
