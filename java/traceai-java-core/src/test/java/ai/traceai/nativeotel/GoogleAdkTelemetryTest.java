package ai.traceai.nativeotel;

import static org.junit.jupiter.api.Assertions.*;

import com.google.gson.JsonParser;
import io.opentelemetry.api.GlobalOpenTelemetry;
import io.opentelemetry.api.baggage.Baggage;
import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.context.Context;
import io.opentelemetry.context.Scope;
import io.opentelemetry.sdk.OpenTelemetrySdk;
import io.opentelemetry.sdk.common.CompletableResultCode;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.data.SpanData;
import io.opentelemetry.sdk.trace.export.SimpleSpanProcessor;
import io.opentelemetry.sdk.trace.export.SpanExporter;
import java.util.Collection;
import java.util.List;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.TimeUnit;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;

class GoogleAdkTelemetryTest {
    private static final GoogleAdkTelemetry.Config CONFIG = new GoogleAdkTelemetry.Config(
            "http://localhost:4318/", "test-key", "test-secret", "test-project");

    static final class Capture implements SpanExporter {
        final List<SpanData> spans = new CopyOnWriteArrayList<>();
        int shutdowns;
        int flushes;
        @Override public CompletableResultCode export(Collection<SpanData> batch) {
            spans.addAll(batch);
            return CompletableResultCode.ofSuccess();
        }
        @Override public CompletableResultCode flush() { flushes++; return CompletableResultCode.ofSuccess(); }
        @Override public CompletableResultCode shutdown() { shutdowns++; return CompletableResultCode.ofSuccess(); }
    }

    static String value(SpanData span, String key) {
        return span.getAttributes().get(AttributeKey.stringKey(key));
    }

    @Test void configDoesNotExposeCredentialsAndValidatesDestination() {
        assertEquals("http://localhost:4318/tracer/v1/traces", CONFIG.tracesEndpoint());
        assertFalse(CONFIG.toString().contains("test-key"));
        assertFalse(CONFIG.toString().contains("test-secret"));
        for (String url : List.of("not-a-url", "https://user:password@localhost", "https://localhost?q=secret", "ftp://localhost")) {
            IllegalArgumentException error = assertThrows(IllegalArgumentException.class,
                    () -> new GoogleAdkTelemetry.Config(url, "key", "secret", "project"));
            assertFalse(error.toString().contains(url));
        }
        assertThrows(IllegalArgumentException.class,
                () -> new GoogleAdkTelemetry.Config("http://localhost", "key", "secret", " "));
    }

    @Test void environmentDefaultsToPrivate() {
        assertFalse(GoogleAdkTelemetry.captureContentFromEnv());
    }

    @ParameterizedTest @ValueSource(booleans = {false, true})
    void privacyIsDestinationLocalAndNativeSpanIdentityIsPreserved(boolean captureContent) {
        Capture destination = new Capture();
        Capture otherDestination = new Capture();
        SdkTracerProvider provider = SdkTracerProvider.builder()
                .setResource(GoogleAdkTelemetry.resource(CONFIG))
                .addSpanProcessor(GoogleAdkTelemetry.spanProcessor(destination, captureContent))
                .addSpanProcessor(SimpleSpanProcessor.create(otherDestination)).build();
        Span span = provider.get("gcp.vertex.agent").spanBuilder("call_llm").startSpan();
        span.setAttribute("gcp.vertex.agent.llm_request", "{\"contents\":[{\"role\":\"user\",\"parts\":[{\"text\":\"private-input\"}]}]}");
        span.setAttribute("gcp.vertex.agent.llm_response", "{\"content\":{\"role\":\"model\",\"parts\":[{\"text\":\"private-output\"}]}}");
        span.setAttribute("gen_ai.request.model", "test-model");
        span.setAttribute("gen_ai.usage.input_tokens", 7L);
        span.setAttribute("error.message", "private-error");
        span.setStatus(StatusCode.ERROR, "private-status");
        span.recordException(new RuntimeException("private-exception"));
        span.end();
        provider.shutdown().join(10, TimeUnit.SECONDS);
        assertEquals(1, destination.spans.size());
        assertEquals(1, otherDestination.spans.size());
        SpanData projected = destination.spans.get(0);
        SpanData original = otherDestination.spans.get(0);
        assertEquals(original.getSpanId(), projected.getSpanId());
        assertEquals(original.getTraceId(), projected.getTraceId());
        assertEquals(original.getStartEpochNanos(), projected.getStartEpochNanos());
        assertEquals(original.getEndEpochNanos(), projected.getEndEpochNanos());
        assertEquals(StatusCode.ERROR, projected.getStatus().getStatusCode());
        assertEquals("LLM", value(projected, "fi.span.kind"));
        assertEquals("test-project", projected.getResource().getAttribute(AttributeKey.stringKey("project_name")));
        assertEquals(7L, projected.getAttributes().get(AttributeKey.longKey("gen_ai.usage.input_tokens")));
        assertEquals("private-status", original.getStatus().getDescription());
        assertEquals(1, original.getEvents().size());
        assertNull(value(original, "input.value"));
        if (captureContent) {
            assertEquals(value(original, "gcp.vertex.agent.llm_request"), value(projected, "input.value"));
            assertEquals("application/json", value(projected, "input.mime_type"));
            assertTrue(value(projected, "gen_ai.input.messages").contains("private-input"));
            assertEquals("assistant", JsonParser.parseString(value(projected, "gen_ai.output.messages"))
                    .getAsJsonArray().get(0).getAsJsonObject().get("role").getAsString());
            assertEquals("private-status", projected.getStatus().getDescription());
            assertEquals(1, projected.getEvents().size());
        } else {
            assertFalse(projected.getAttributes().toString().contains("private-"));
            assertTrue(projected.getStatus().getDescription().isEmpty());
            assertTrue(projected.getEvents().isEmpty());
            assertEquals(0, projected.getTotalRecordedEvents());
        }
        assertEquals(1, destination.shutdowns);
    }

    @Test void mapsToolsRatherThanEmptyAdkLlmPlaceholdersAndPreservesExistingInput() {
        Capture capture = new Capture();
        OpenTelemetrySdk sdk = GoogleAdkTelemetry.build(CONFIG, capture, true);
        Span span = sdk.getTracer("gcp.vertex.agent").spanBuilder("execute_tool weather").startSpan();
        span.setAttribute("gcp.vertex.agent.tool_call_args", "{\"city\":\"Paris\"}");
        span.setAttribute("gcp.vertex.agent.tool_response", "{\"weather\":\"sunny\"}");
        span.setAttribute("gcp.vertex.agent.llm_request", "{}");
        span.setAttribute("gcp.vertex.agent.llm_response", "{}");
        span.setAttribute("gen_ai.tool_call.id", "call-1");
        span.setAttribute("input.value", "custom-input");
        span.setAttribute("input.mime_type", "text/plain");
        span.end();
        GoogleAdkTelemetry.shutdown(sdk);
        SpanData data = capture.spans.get(0);
        assertEquals("custom-input", value(data, "input.value"));
        assertEquals("text/plain", value(data, "input.mime_type"));
        assertEquals("{\"city\":\"Paris\"}", value(data, "gen_ai.tool.call.arguments"));
        assertEquals("{\"weather\":\"sunny\"}", value(data, "output.value"));
        assertEquals("call-1", value(data, "gen_ai.tool.call.id"));
        assertNull(value(data, "gen_ai.input.messages"));
    }

    @Test void mapsStructuredSystemTextAndFunctionPartsWithoutFlatteningThem() {
        Capture capture = new Capture();
        OpenTelemetrySdk sdk = GoogleAdkTelemetry.build(CONFIG, capture, true);
        Span span = sdk.getTracer("gcp.vertex.agent").spanBuilder("call_llm").startSpan();
        span.setAttribute("gcp.vertex.agent.llm_request", """
                {"config":{"system_instruction":{"parts":[{"text":"system"}]}},"contents":[
                {"role":"model","parts":[{"functionCall":{"id":"1","name":"tool","args":{"x":1}}}]},
                {"role":"user","parts":[{"function_response":{"id":"1","name":"tool","response":{"ok":true}}}]}]}
                """);
        span.end();
        GoogleAdkTelemetry.shutdown(sdk);
        var messages = JsonParser.parseString(value(capture.spans.get(0), "gen_ai.input.messages")).getAsJsonArray();
        assertEquals(3, messages.size());
        assertEquals("system", messages.get(0).getAsJsonObject().get("role").getAsString());
        var tool = messages.get(1).getAsJsonObject().getAsJsonArray("parts").get(0).getAsJsonObject();
        assertEquals("tool_call", tool.get("type").getAsString());
        assertEquals(1, tool.getAsJsonObject("arguments").get("x").getAsInt());
        assertEquals("tool_call_response", messages.get(2).getAsJsonObject().getAsJsonArray("parts")
                .get(0).getAsJsonObject().get("type").getAsString());
    }

    @ParameterizedTest @ValueSource(strings = {"{broken", "{}", "null", "[]", "{\"contents\":true}", "{\"contents\":[{\"parts\":5}]}", "N/A"})
    void unsupportedContentDoesNotBreakExport(String content) {
        Capture capture = new Capture();
        OpenTelemetrySdk sdk = GoogleAdkTelemetry.build(CONFIG, capture, true);
        Span span = sdk.getTracer("gcp.vertex.agent").spanBuilder("call_llm").startSpan();
        span.setAttribute("gcp.vertex.agent.llm_request", content);
        span.end();
        GoogleAdkTelemetry.shutdown(sdk);
        assertEquals(1, capture.spans.size());
        assertEquals(content, value(capture.spans.get(0), "gcp.vertex.agent.llm_request"));
    }

    @Test void onlyNativeScopeGetsKindAndBaggage() {
        Capture capture = new Capture();
        OpenTelemetrySdk sdk = GoogleAdkTelemetry.build(CONFIG, capture, false);
        Context context = Context.root().with(Baggage.builder().put("session.id", "session-1").put("user.id", "user-1").build());
        try (Scope ignored = context.makeCurrent()) {
            sdk.getTracer("unrelated").spanBuilder("call_llm").startSpan().end();
            sdk.getTracer("gcp.vertex.agent").spanBuilder("invoke_agent a").startSpan().end();
        }
        Span fallback = sdk.getTracer("gcp.vertex.agent").spanBuilder("call_llm").startSpan();
        fallback.setAttribute("gcp.vertex.agent.session_id", "native-session");
        fallback.end();
        GoogleAdkTelemetry.shutdown(sdk);
        assertNull(value(capture.spans.get(0), "fi.span.kind"));
        assertNull(value(capture.spans.get(0), "session.id"));
        assertEquals("AGENT", value(capture.spans.get(1), "fi.span.kind"));
        assertEquals("session-1", value(capture.spans.get(1), "session.id"));
        assertEquals("user-1", value(capture.spans.get(1), "user.id"));
        assertEquals("native-session", value(capture.spans.get(2), "session.id"));
        assertNull(value(capture.spans.get(2), "user.id"));
    }

    @Test void lateRegistrationClosesRejectedExporterAndDoesNotReplaceTheGlobal() {
        GlobalOpenTelemetry.resetForTest();
        Capture original = new Capture();
        Capture rejected = new Capture();
        OpenTelemetrySdk sdk = GoogleAdkTelemetry.register(CONFIG, original, false);
        try {
            assertThrows(IllegalStateException.class, () -> GoogleAdkTelemetry.register(CONFIG, rejected, false));
            assertEquals(1, rejected.shutdowns);
            GlobalOpenTelemetry.getTracer("gcp.vertex.agent").spanBuilder("invocation").startSpan().end();
        } finally {
            GoogleAdkTelemetry.shutdown(sdk);
            GlobalOpenTelemetry.resetForTest();
        }
        assertEquals(1, original.spans.size());
        assertEquals(1, original.shutdowns);
    }

    @Test void projectionPreservesDroppedAttributeCount() {
        Capture projected = new Capture();
        Capture original = new Capture();
        var provider = SdkTracerProvider.builder()
                .setSpanLimits(io.opentelemetry.sdk.trace.SpanLimits.builder().setMaxNumberOfAttributes(4).build())
                .addSpanProcessor(GoogleAdkTelemetry.spanProcessor(projected, false))
                .addSpanProcessor(SimpleSpanProcessor.create(original)).build();
        Span span = provider.get("gcp.vertex.agent").spanBuilder("call_llm").startSpan();
        for (int i = 0; i < 10; i++) span.setAttribute("field-" + i, i);
        span.end();
        provider.shutdown().join(5, TimeUnit.SECONDS);
        SpanData before = original.spans.get(0);
        SpanData after = projected.spans.get(0);
        assertEquals(6, before.getTotalAttributeCount() - before.getAttributes().size());
        assertEquals(6, after.getTotalAttributeCount() - after.getAttributes().size());
    }

    @Test void exporterPropagatesDelegateFailureRatherThanClaimingSuccess() {
        CompletableResultCode failed = CompletableResultCode.ofFailure();
        SpanExporter delegate = new SpanExporter() {
            @Override public CompletableResultCode export(Collection<SpanData> spans) { return failed; }
            @Override public CompletableResultCode flush() { return failed; }
            @Override public CompletableResultCode shutdown() { return failed; }
        };
        GoogleAdkSpanExporter exporter = new GoogleAdkSpanExporter(delegate, false);
        assertSame(failed, exporter.export(List.of()));
        assertSame(failed, exporter.flush());
        assertSame(failed, exporter.shutdown());
    }

    @Test void exporterForwardsLifecycle() {
        Capture capture = new Capture();
        GoogleAdkSpanExporter exporter = new GoogleAdkSpanExporter(capture, false);
        assertTrue(exporter.flush().isSuccess());
        assertTrue(exporter.shutdown().isSuccess());
        assertEquals(1, capture.flushes);
        assertEquals(1, capture.shutdowns);
    }
}
