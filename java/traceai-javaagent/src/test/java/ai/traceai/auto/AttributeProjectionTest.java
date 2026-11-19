package ai.traceai.auto;

import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.context.Context;
import io.opentelemetry.sdk.testing.exporter.InMemorySpanExporter;
import io.opentelemetry.sdk.trace.*;
import io.opentelemetry.sdk.trace.export.SimpleSpanProcessor;
import java.util.*;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;
import static org.junit.jupiter.api.Assertions.*;

class AttributeProjectionTest {
    @org.junit.jupiter.api.Test void selectionIsDeterministicAtomicAndPreservesNewestSuffix() {
        Map<String, Object> attrs = new LinkedHashMap<>();
        attrs.put("gen_ai.span.kind", "LLM"); attrs.put("gen_ai.usage.input_tokens", 3L); attrs.put("error.type", "IOException");
        attrs.put("gen_ai.output.messages.0.message.role", "assistant"); attrs.put("gen_ai.output.messages.0.message.content", "output");
        attrs.put("output.value", "output"); attrs.put("output.mime_type", "text/plain");
        for (int i = 0; i < 3; i++) {
            attrs.put("gen_ai.input.messages." + i + ".message.role", "user"); attrs.put("gen_ai.input.messages." + i + ".message.content", "m" + i);
        }
        attrs.put("gen_ai.input.messages.2.message.tool_call_id", "call");
        attrs.put("input.value", "aggregate"); attrs.put("input.mime_type", "text/plain"); attrs.put("gen_ai.request.temperature", .5);
        List<String> keys = new ArrayList<>(attrs.keySet()); Collections.reverse(keys); Map<String, Object> reverse = new LinkedHashMap<>(); keys.forEach(k -> reverse.put(k, attrs.get(k)));
        for (int limit = 0; limit <= attrs.size() + 1; limit++) {
            Map<String, Object> selected = AttributeProjection.select(attrs, limit, "chat");
            assertEquals(selected, AttributeProjection.select(reverse, limit, "chat")); assertTrue(selected.size() <= limit);
            assertEquals(selected.containsKey("output.value"), selected.containsKey("output.mime_type"));
            assertEquals(selected.containsKey("input.value"), selected.containsKey("input.mime_type"));
            if (selected.containsKey("gen_ai.input.messages.0.message.role")) assertTrue(selected.containsKey("gen_ai.input.messages.1.message.role"));
            if (selected.containsKey("gen_ai.input.messages.1.message.role")) assertTrue(selected.containsKey("gen_ai.input.messages.2.message.role"));
            if (selected.containsKey("input.value")) assertTrue(selected.containsKey("gen_ai.input.messages.0.message.role"));
            if (selected.containsKey(AttributeProjection.DROPPED)) {
                long omitted = attrs.keySet().stream().filter(AttributeProjection::content).filter(k -> !selected.containsKey(k)).count();
                assertEquals(omitted, selected.get(AttributeProjection.DROPPED));
            }
        }
        var small = AttributeProjection.select(attrs, 10, "chat");
        assertFalse(small.keySet().stream().anyMatch(k -> k.startsWith("gen_ai.input.messages.")), "a smaller old message must not fill the gap left by newest");
    }
    @org.junit.jupiter.api.Test void mimeContextAndPayloadAreAtomicAndSanitizedBeforeAdmission() {
        var config = AutoConfig.resolve(Map.of(), Map.of());
        var match = WireCore.match("POST", "http://127.0.0.1/v1/chat/completions");
        Map<String, Object> source = Map.of("gen_ai.span.kind", "LLM", "parts.0.mime_type", "audio/pcm", "parts.0.content.chunk", "AQIDBA==");
        var clean = AttributeProjection.sanitize(source, config, match);
        assertEquals("<omitted: 8 base64 chars>", clean.get("parts.0.content.chunk"));
        var selected = AttributeProjection.select(clean, 2, "chat");
        assertFalse(selected.containsKey("parts.0.mime_type")); assertFalse(selected.containsKey("parts.0.content.chunk"));
        assertEquals(2L, selected.get(AttributeProjection.DROPPED));
        selected = AttributeProjection.select(clean, 3, "chat");
        assertTrue(selected.containsKey("parts.0.mime_type")); assertTrue(selected.containsKey("parts.0.content.chunk"));
    }
    @org.junit.jupiter.api.Test void protectedOverflowAndPriorDropCountsHaveDeterministicPriority() {
        var source = Map.<String, Object>of("gen_ai.span.kind", "LLM", "gen_ai.usage.total_tokens", 5L, "error.type", "failure", "traceai.wire.incomplete", true,
            "input.value", "text", "input.mime_type", "text/plain", AttributeProjection.DROPPED, 7L);
        var selected = AttributeProjection.select(source, 3, "chat");
        assertEquals(Set.of(AttributeProjection.DROPPED, "traceai.wire.incomplete", "gen_ai.span.kind"), selected.keySet());
        assertEquals(9L, selected.get(AttributeProjection.DROPPED));
        long before = AttributeProjection.ZERO_CAPACITY.get(); assertTrue(AttributeProjection.select(source, 0, "chat").isEmpty());
        assertEquals(before + 1, AttributeProjection.ZERO_CAPACITY.get());
        assertEquals(128, TraceAIAuto.attributeLimit(AutoConfig.resolve(Map.of(), Map.of())));
        assertEquals(32, TraceAIAuto.attributeLimit(AutoConfig.resolve(Map.of(), Map.of("OTEL_ATTRIBUTE_COUNT_LIMIT", "32"))));
        assertEquals(0, TraceAIAuto.attributeLimit(AutoConfig.resolve(Map.of(), Map.of("OTEL_ATTRIBUTE_COUNT_LIMIT", "32", "OTEL_SPAN_ATTRIBUTE_COUNT_LIMIT", "0"))));
    }

    @org.junit.jupiter.api.Test void actualHostLimitsAreCapturedWithoutInstallingAGlobalProvider() throws Exception {
        io.opentelemetry.api.GlobalOpenTelemetry.resetForTest();
        assertNull(ApplicationOtel.sdkProvider(getClass().getClassLoader()));
        OtelBridge.snapshot();
        assertNull(ApplicationOtel.sdkProvider(getClass().getClassLoader()));
        try (var host = SdkTracerProvider.builder().setSpanLimits(SpanLimits.builder().setMaxNumberOfAttributes(32).build()).build();
             var local = SdkTracerProvider.builder().setSpanLimits(SpanLimits.builder().setMaxNumberOfAttributes(16).build()).build()) {
            io.opentelemetry.sdk.OpenTelemetrySdk.builder().setTracerProvider(host).buildAndRegisterGlobal();
            assertEquals(32, OtelBridge.snapshot().attributeLimit());
            var span = local.get("local").spanBuilder("local").startSpan();
            try (var scope = span.makeCurrent()) { assertEquals(16, OtelBridge.snapshot().attributeLimit()); }
            finally { span.end(); }
            assertEquals(32, host.getSpanLimits().getMaxNumberOfAttributes());
        } finally { io.opentelemetry.api.GlobalOpenTelemetry.resetForTest(); }
    }
    @ParameterizedTest
    @org.junit.jupiter.params.provider.CsvSource({"false,true", "true,true", "false,false", "true,false"})
    void samplingMetadataCountsAgainstProviderAndContextBudgets(boolean contextLimit, boolean fractionalRootRate) {
        var config = AutoConfig.resolve(Map.of(), Map.of("FI_SAMPLE_RATE", fractionalRootRate ? "0.25" : "1"));
        var memory = InMemorySpanExporter.create();
        try (var provider = SdkTracerProvider.builder().setSampler(TraceAIAuto.ownedSampler(config))
                .setSpanLimits(SpanLimits.builder().setMaxNumberOfAttributes(contextLimit ? 10 : 3).build())
                .addSpanProcessor(SimpleSpanProcessor.create(memory)).build()) {
            var runtime = new TraceAIAuto.RuntimeState(config, provider, provider.get("ratio-budget"), new OtlpJsonExporter(config));
            var parent = io.opentelemetry.api.trace.SpanContext.createFromRemoteParent(
                "00000000000000000000000000000001", "0000000000000001",
                io.opentelemetry.api.trace.TraceFlags.getSampled(),
                io.opentelemetry.api.trace.TraceState.builder().put("fi", "r:0.25").build());
            var context = Context.root().with(io.opentelemetry.api.trace.Span.wrap(parent));
            var request = AdversarialTest.request("http://127.0.0.1/v1/chat/completions", Map.of("model", "m"));
            var match = WireCore.match(request.method, request.url);
            var snapshot = new OtelBridge.Snapshot(context, Map.of(), false, 1, 1, contextLimit ? 3 : null, null);
            var capture = new WireCapture(request, request.headers, match, snapshot, 4194304, runtime);
            capture.endEpochNanos = 2;
            TraceAIAuto.emit(capture, new WireCore.Result("ratio-budget", Map.of(
                "gen_ai.span.kind", "LLM", "output.mime_type", "text/plain", "output.value", "output"), true, null));
            assertEquals(1, memory.getFinishedSpanItems().size());
            var attrs = memory.getFinishedSpanItems().get(0).getAttributes();
            assertEquals(0.25, attrs.get(AttributeKey.doubleKey("fi.sampling.ratio")), "full-rate local sampling must still budget copied metadata");
            assertTrue(attrs.size() <= 3);
            assertNull(attrs.get(AttributeKey.stringKey("output.mime_type")));
            assertNull(attrs.get(AttributeKey.stringKey("output.value")));
            assertEquals(2L, attrs.get(AttributeKey.longKey(AttributeProjection.DROPPED)));
        }
    }

    @ParameterizedTest
    @org.junit.jupiter.params.provider.CsvSource({"0,default", "1,default", "3,default", "0,1", "1,1", "3,1", "0,0.5", "1,0.5", "3,0.5"})
    void inheritedApplicationBudgetBoundsSamplerAttributesAtExport(int limit, String rate) {
        var config = AutoConfig.resolve(Map.of(), rate.equals("default") ? Map.of() : Map.of("FI_SAMPLE_RATE", rate));
        var memory = InMemorySpanExporter.create();
        var hostMemory = InMemorySpanExporter.create();
        try (var host = SdkTracerProvider.builder()
                .setSpanLimits(ContextApp.limits(limit))
                .addSpanProcessor(SimpleSpanProcessor.create(hostMemory)).build();
             var provider = SdkTracerProvider.builder().setSampler(TraceAIAuto.ownedSampler(config))
                .setSpanLimits(SpanLimits.builder().setMaxNumberOfAttributes(10).build())
                .addSpanProcessor(new WireResourceProcessor(SimpleSpanProcessor.create(memory))).build()) {
            var remote = io.opentelemetry.api.trace.SpanContext.createFromRemoteParent(
                "00000000000000000000000000000001", "0000000000000001",
                io.opentelemetry.api.trace.TraceFlags.getSampled(),
                io.opentelemetry.api.trace.TraceState.builder().put("vendor", "value").put("fi", "r:0.25").build());
            var parent = host.get("application").spanBuilder("application-parent")
                .setParent(Context.root().with(io.opentelemetry.api.trace.Span.wrap(remote))).startSpan();
            parent.setAttribute("application.attribute", "unchanged");
            var runtime = new TraceAIAuto.RuntimeState(config, provider, provider.get(TraceAIAuto.SCOPE), new OtlpJsonExporter(config));
            try (var scope = parent.makeCurrent()) {
                var snapshot = OtelBridge.snapshot();
                assertEquals(limit, snapshot.attributeLimit());
                var request = AdversarialTest.request("http://127.0.0.1/v1/chat/completions", Map.of("model", "m"));
                var capture = new WireCapture(request, request.headers, WireCore.match(request.method, request.url), snapshot, 4194304, runtime);
                capture.endEpochNanos = capture.startEpochNanos + 1;
                TraceAIAuto.emit(capture, new WireCore.Result("ratio-budget", Map.of(
                    "gen_ai.span.kind", "LLM", "output.mime_type", "text/plain", "output.value", "output"), true, null));
            } finally { parent.end(); }
            assertEquals(1, memory.getFinishedSpanItems().size(), "sampled parent must still keep the wire span");
            var span = memory.getFinishedSpanItems().get(0);
            assertTrue(span.getSpanContext().isSampled());
            assertEquals(parent.getSpanContext().getTraceId(), span.getTraceId());
            assertEquals(parent.getSpanContext().getSpanId(), span.getParentSpanId());
            assertEquals(remote.getTraceState(), span.getSpanContext().getTraceState());
            var resource = runtime.exporter().serialize(span);
            var exported = WireCore.map(WireCore.list(WireCore.get(WireCore.list(resource.get("scopeSpans")).get(0), "spans")).get(0));
            assertTrue(WireCore.list(exported.get("attributes")).size() <= limit, "export must respect the inherited budget, including sampler attributes");
            assertEquals("fi=r:0.25,vendor=value", exported.get("traceState"));
            assertTrue(span.getAttributes().size() <= limit);
            assertEquals(limit == 0 ? null : 0.25, span.getAttributes().get(AttributeKey.doubleKey("fi.sampling.ratio")));
            assertEquals(1, hostMemory.getFinishedSpanItems().size());
            var hostSpan = hostMemory.getFinishedSpanItems().get(0);
            assertNull(hostSpan.getAttributes().get(AttributeKey.doubleKey("fi.sampling.ratio")), "application provider must not be stamped");
            assertEquals(limit == 0 ? null : "unchanged", hostSpan.getAttributes().get(AttributeKey.stringKey("application.attribute")));
            assertEquals(remote.getTraceState(), hostSpan.getSpanContext().getTraceState());
            assertEquals(limit, host.getSpanLimits().getMaxNumberOfAttributes());
            assertEquals(10, provider.getSpanLimits().getMaxNumberOfAttributes());
        }
    }

    @ParameterizedTest @ValueSource(ints = {32, 128}) void emissionReservesOutcomeUsageAndNewestContentAtSdkLimit(int limit) {
        var memory = InMemorySpanExporter.create();
        try (var provider = SdkTracerProvider.builder().setSpanLimits(SpanLimits.builder().setMaxNumberOfAttributes(limit).build())
            .addSpanProcessor(SimpleSpanProcessor.create(memory)).build()) {
            var config = AutoConfig.resolve(Map.of(), Map.of());
            var runtime = new TraceAIAuto.RuntimeState(config, provider, provider.get("projection"), new OtlpJsonExporter(config));
            List<Object> messages = new ArrayList<>(); for (int i = 0; i < 80; i++) messages.add(Map.of("role", "user", "content", "message " + i));
            var request = AdversarialTest.request("http://127.0.0.1/v1/chat/completions", Map.of("model", "m", "messages", messages));
            var response = AdversarialTest.response("application/json", "{\"choices\":[{\"message\":{\"role\":\"assistant\",\"content\":\"output\"}}],\"usage\":{\"prompt_tokens\":3,\"completion_tokens\":2}}");
            response.error = new java.io.IOException("transport failure"); response.incomplete = true;
            var match = WireCore.match(request.method, request.url);
            var capture = new WireCapture(request, request.headers, match, new OtelBridge.Snapshot(Context.root(), Map.of(), false, 1, 1), 4194304, runtime);
            capture.endEpochNanos = 2;
            TraceAIAuto.emit(capture, WireCore.build(match, request, response));
            var span = memory.getFinishedSpanItems().get(0); var attrs = span.getAttributes();
            assertEquals("IOException", attrs.get(AttributeKey.stringKey("error.type")));
            assertEquals(true, attrs.get(AttributeKey.booleanKey("traceai.wire.incomplete")));
            assertEquals(5L, attrs.get(AttributeKey.longKey("gen_ai.usage.total_tokens")));
            assertEquals("output", attrs.get(AttributeKey.stringKey("output.value")));
            assertEquals("text/plain", attrs.get(AttributeKey.stringKey("output.mime_type")));
            assertEquals("message 79", attrs.get(AttributeKey.stringKey("gen_ai.input.messages.79.message.content")));
            assertNull(attrs.get(AttributeKey.stringKey("gen_ai.input.messages.0.message.content")));
            assertNull(attrs.get(AttributeKey.stringKey("input.value")));
            assertNotNull(attrs.get(AttributeKey.longKey("traceai.capture.dropped_content_count")));
            assertTrue(attrs.size() <= limit);
            String exported = WireCore.dump(new OtlpJsonExporter(config).serialize(span));
            assertTrue(exported.contains("traceai.capture.dropped_content_count")); assertTrue(exported.contains("gen_ai.usage.total_tokens"));
        }
    }
}
